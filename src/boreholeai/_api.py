"""Low-level HTTP calls to the BoreholeAI worker API."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

from boreholeai._version import __version__
from boreholeai.exceptions import (
    AuthenticationError,
    BoreholeAIError,
    InsufficientCreditsError,
    RateLimitError,
    ServerError,
)

DEFAULT_BASE_URL = "https://api1.boreholeai.com"
DEFAULT_URLS = [
    "https://api1.boreholeai.com",
    "https://api2.boreholeai.com",
    "https://api3.boreholeai.com",
]
DEFAULT_TIMEOUT = 120.0
CONNECT_TIMEOUT = 5.0


class APIClientAsync:
    """Async sibling of `APIClient` — same surface, asyncio-native.

    Owns auth headers, base-URL failover, and the underlying httpx clients.
    Used by `_batch.py` for concurrent submit / poll / download. Sync code
    paths keep using `APIClient`.

    The optional `_transport` kwarg is a test seam (`httpx.MockTransport`).
    Pass `None` in production.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: float,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._auth_headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
        tz = _detect_local_timezone()
        if tz:
            self._auth_headers["X-Timezone"] = tz
        self._timeout = timeout
        self._urls = [base_url] if base_url not in DEFAULT_URLS else DEFAULT_URLS
        self._transport = _transport
        self._api_client: httpx.AsyncClient | None = None
        self._dl_client: httpx.AsyncClient | None = None
        self.server_tag: str = ""

    async def _connect(self) -> httpx.AsyncClient:
        """Try each URL in order until one responds."""
        if self._api_client is not None:
            return self._api_client

        for i, url in enumerate(self._urls):
            try:
                client = httpx.AsyncClient(
                    base_url=url,
                    headers=self._auth_headers,
                    timeout=httpx.Timeout(self._timeout, connect=CONNECT_TIMEOUT),
                    transport=self._transport,
                )
                health = await client.get("/health")
                if health.status_code >= 500:
                    await client.aclose()
                    raise httpx.ConnectError(f"{url} returned {health.status_code}")
                self._api_client = client
                self.server_tag = url.split("//")[1].split(".")[0]
                return self._api_client
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if i < len(self._urls) - 1:
                    continue
                raise

    async def close(self) -> None:
        if self._api_client is not None:
            await self._api_client.aclose()
            self._api_client = None
        if self._dl_client is not None:
            await self._dl_client.aclose()
            self._dl_client = None

    async def __aenter__(self) -> APIClientAsync:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def create_job(self, file_paths: list[Path]) -> dict[str, Any]:
        """POST /v1/jobs — upload files and create a job."""
        client = await self._connect()
        # Read files concurrently in threads so the event loop isn't blocked.
        contents = await asyncio.gather(
            *(asyncio.to_thread(p.read_bytes) for p in file_paths),
        )
        files = [
            ("files", (p.name, content))
            for p, content in zip(file_paths, contents)
        ]
        resp = await client.post("/v1/jobs", files=files)
        _raise_for_status(resp)
        return resp.json()

    async def get_job(self, job_id: str) -> dict[str, Any]:
        client = await self._connect()
        resp = await client.get(f"/v1/jobs/{job_id}")
        _raise_for_status(resp)
        return resp.json()

    async def get_results(self, job_id: str) -> dict[str, Any]:
        client = await self._connect()
        resp = await client.get(f"/v1/jobs/{job_id}/results")
        _raise_for_status(resp)
        return resp.json()

    async def get_me(self) -> dict[str, Any]:
        """GET /v1/me — per-user account info, including the server's
        concurrency cap. The SDK fetches this once at batch start so it
        can size its own submit semaphore and never send POSTs that
        would be 429-rejected by the cap.
        """
        client = await self._connect()
        resp = await client.get("/v1/me")
        _raise_for_status(resp)
        return resp.json()

    async def download_file(self, url: str) -> bytes:
        """Download a signed URL.

        Uses a separate httpx.AsyncClient (no auth headers, no base URL) since
        signed URLs go to Supabase Storage, not the API host.
        """
        if self._dl_client is None:
            self._dl_client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport,
            )
        resp = await self._dl_client.get(url)
        resp.raise_for_status()
        return resp.content

    async def delete_job(self, job_id: str) -> dict[str, Any]:
        """DELETE /v1/jobs/{id} — purge stored files for this job (idempotent)."""
        client = await self._connect()
        resp = await client.delete(f"/v1/jobs/{job_id}")
        _raise_for_status(resp)
        return resp.json()




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _detect_local_timezone() -> str:
    """Best-effort detection of the local IANA timezone name (e.g. 'Australia/Brisbane').

    Reads the /etc/localtime symlink on macOS/Linux. Falls back to TZ env var.
    Returns empty string if detection fails — server will use its configured default.
    """
    import os
    # 1. Explicit TZ env var (works on all platforms)
    tz = os.environ.get("TZ", "")
    if tz and "/" in tz:
        return tz
    # 2. /etc/localtime symlink (macOS and most Linux)
    try:
        link = os.readlink("/etc/localtime")
        for marker in ("/zoneinfo/", "/zoneinfo-icu/"):
            if marker in link:
                return link.split(marker, 1)[1]
    except (OSError, ValueError):
        pass
    return ""


_update_warned = False


def _check_sdk_version(resp: httpx.Response) -> None:
    """Warn once if the server reports a newer SDK version."""
    global _update_warned
    if _update_warned:
        return
    try:
        latest = resp.headers.get("X-SDK-Latest-Version")
        if latest and latest != __version__:
            _update_warned = True
            print(
                f"  Update available: boreholeai {latest} (you have {__version__}). "
                f"Run: pip install -U boreholeai",
                file=sys.stderr,
            )
    except Exception:
        pass


def _raise_for_status(resp: httpx.Response) -> None:
    """Convert HTTP errors to SDK exceptions."""
    _check_sdk_version(resp)
    if resp.is_success:
        return

    detail = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        pass

    if resp.status_code == 401:
        raise AuthenticationError("Invalid API key", 401)
    if resp.status_code == 402:
        raise InsufficientCreditsError(detail or "Insufficient credits", 402)
    if resp.status_code == 429:
        raise RateLimitError("Rate limited — please retry shortly", 429)
    if resp.status_code >= 500:
        raise ServerError("Server error — please retry or contact support", resp.status_code)
    raise BoreholeAIError(f"Request failed (HTTP {resp.status_code})", resp.status_code)
