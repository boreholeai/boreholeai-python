"""Low-level HTTP calls to the BoreholeAI worker API."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx

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
        self._auth_headers = {"Authorization": f"Bearer {api_key}"}
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
                client.get("/health")
                self._client = client
                if i > 0:
                    print(f"  Connected to fallback server", file=sys.stderr, flush=True)
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

def _raise_for_status(resp: httpx.Response) -> None:
    """Convert HTTP errors to SDK exceptions."""
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
