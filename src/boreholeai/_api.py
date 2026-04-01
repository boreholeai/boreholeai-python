"""Low-level HTTP calls to the BoreholeAI worker API."""

from __future__ import annotations

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


class APIClient:
    """Thin wrapper around httpx for authenticated requests.

    Tries URLs in order: api1 → api2 → api3.
    On connection failure, falls back to the next URL. Once connected,
    stays on that server for the remainder of the session.
    """

    def __init__(self, api_key: str, base_url: str, timeout: float):
        self._auth_headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
        tz = _detect_local_timezone()
        if tz:
            self._auth_headers["X-Timezone"] = tz
        self._timeout = timeout
        self._urls = [base_url] if base_url not in DEFAULT_URLS else DEFAULT_URLS
        self._client: httpx.Client | None = None

    def _connect(self) -> httpx.Client:
        """Try each URL in order until one responds."""
        if self._client is not None:
            return self._client

        for i, url in enumerate(self._urls):
            try:
                client = httpx.Client(
                    base_url=url,
                    headers=self._auth_headers,
                    timeout=httpx.Timeout(self._timeout, connect=CONNECT_TIMEOUT),
                )
                health = client.get("/health")
                if health.status_code >= 500:
                    client.close()
                    raise httpx.ConnectError(f"{url} returned {health.status_code}")
                self._client = client
                server_tag = url.split("//")[1].split(".")[0]  # e.g. "api1"
                print(f"  [{server_tag}]", file=sys.stderr, flush=True)
                return self._client
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if i < len(self._urls) - 1:
                    continue
                raise

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def create_job(self, file_paths: list[Path]) -> dict[str, Any]:
        """POST /v1/jobs — upload files and create a job.

        Returns {"job_id": ..., "num_pages": ..., "credits_remaining": ...}.
        """
        client = self._connect()
        files = [
            ("files", (p.name, p.read_bytes()))
            for p in file_paths
        ]
        resp = client.post("/v1/jobs", files=files)
        _raise_for_status(resp)
        return resp.json()

    def get_job(self, job_id: str) -> dict[str, Any]:
        """GET /v1/jobs/{id} — poll job status."""
        client = self._connect()
        resp = client.get(f"/v1/jobs/{job_id}")
        _raise_for_status(resp)
        return resp.json()

    def get_results(self, job_id: str) -> dict[str, Any]:
        """GET /v1/jobs/{id}/results — signed download URLs."""
        client = self._connect()
        resp = client.get(f"/v1/jobs/{job_id}/results")
        _raise_for_status(resp)
        return resp.json()

    def download_file(self, url: str) -> bytes:
        """Download a file from a signed URL."""
        resp = httpx.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.content




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
        detail = resp.text

    if resp.status_code == 401:
        raise AuthenticationError(detail or "Invalid API key", 401)
    if resp.status_code == 402:
        raise InsufficientCreditsError(detail or "Insufficient credits", 402)
    if resp.status_code == 429:
        raise RateLimitError(detail or "Rate limited", 429)
    if resp.status_code >= 500:
        raise ServerError(detail or "Server error", resp.status_code)
    raise BoreholeAIError(detail or f"HTTP {resp.status_code}", resp.status_code)
