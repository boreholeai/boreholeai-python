"""Tests for `APIClientAsync` — uses httpx.MockTransport via the `_transport`
test seam to avoid real network calls.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from boreholeai._api import APIClientAsync, DEFAULT_BASE_URL
from boreholeai.exceptions import (
    AuthenticationError,
    BoreholeAIError,
    InsufficientCreditsError,
    RateLimitError,
    ServerError,
)


def _transport(routes: dict, default_status: int = 404):
    """Build a MockTransport from `{(method, path_suffix): handler_or_response}`."""
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        match = routes.get(key) or routes.get(("*", request.url.path))
        if match is None:
            return httpx.Response(default_status)
        if callable(match):
            return match(request)
        return match
    return httpx.MockTransport(handler)


def _ok_health() -> dict:
    return {("GET", "/health"): httpx.Response(200, json={"ok": True})}


def _client(routes, base_url=DEFAULT_BASE_URL, **kwargs) -> APIClientAsync:
    return APIClientAsync(
        api_key="bhai_test", base_url=base_url, timeout=5.0,
        _transport=_transport({**_ok_health(), **routes}),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_create_job_returns_payload():
    async with _client({
        ("POST", "/v1/jobs"): httpx.Response(
            202, json={"job_id": "abc", "num_pages": 5, "credits_remaining": 100},
        ),
    }) as c:
        result = await c.create_job([])  # empty file list still hits the endpoint
    assert result == {"job_id": "abc", "num_pages": 5, "credits_remaining": 100}


@pytest.mark.asyncio
async def test_get_job_returns_status():
    async with _client({
        ("GET", "/v1/jobs/abc"): httpx.Response(
            200, json={"status": "processing", "progress": {"pages_done": 2, "pages_total": 5}},
        ),
    }) as c:
        result = await c.get_job("abc")
    assert result["status"] == "processing"
    assert result["progress"]["pages_done"] == 2


@pytest.mark.asyncio
async def test_get_results_returns_signed_urls():
    async with _client({
        ("GET", "/v1/jobs/abc/results"): httpx.Response(
            200, json={"files": [{"filename": "out.xlsx", "url": "https://signed/x"}]},
        ),
    }) as c:
        result = await c.get_results("abc")
    assert result["files"][0]["filename"] == "out.xlsx"


@pytest.mark.asyncio
async def test_delete_job_is_idempotent():
    async with _client({
        ("DELETE", "/v1/jobs/abc"): httpx.Response(
            200, json={"files_deleted": 0, "already_purged": True},
        ),
    }) as c:
        result = await c.delete_job("abc")
    assert result["already_purged"] is True


@pytest.mark.asyncio
async def test_create_job_sends_data_plane_field_when_local(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-fake\n")
    captured = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(202, json={"job_id": "abc"})

    async with _client({("POST", "/v1/jobs"): capture}, local_data=True) as c:
        await c.create_job([src])

    # Multipart body: both the file part and the data_plane form field
    assert b'name="files"' in captured["body"]
    assert b'name="data_plane"' in captured["body"]
    assert b"local" in captured["body"]


@pytest.mark.asyncio
async def test_create_job_omits_data_plane_by_default():
    captured = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(202, json={"job_id": "abc"})

    async with _client({("POST", "/v1/jobs"): capture}) as c:
        await c.create_job([])

    assert b'name="data_plane"' not in captured["body"]


@pytest.mark.asyncio
async def test_download_file_reads_file_url_from_disk(tmp_path):
    src = tmp_path / "dir with spaces" / "out.xlsx"
    src.parent.mkdir()
    src.write_bytes(b"local bytes")

    async with _client({}) as c:
        content = await c.download_file(src.as_uri())

    assert content == b"local bytes"


@pytest.mark.asyncio
async def test_download_file_missing_file_url_raises(tmp_path):
    missing = tmp_path / "gone.xlsx"

    async with _client({}) as c:
        with pytest.raises(BoreholeAIError, match="Local-mode result file not found"):
            await c.download_file(missing.as_uri())


@pytest.mark.asyncio
async def test_auth_header_is_attached():
    captured = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"status": "completed"})

    async with _client({("GET", "/v1/jobs/x"): capture}) as c:
        await c.get_job("x")

    assert captured["auth"] == "Bearer bhai_test"


@pytest.mark.asyncio
async def test_failover_to_next_url_when_first_fails():
    """api1 raises ConnectError → api2 succeeds → server_tag reflects api2."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "api1" in str(request.url):
            raise httpx.ConnectError("simulated down")
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"status": "completed"})

    c = APIClientAsync(
        api_key="bhai_test",
        base_url=DEFAULT_BASE_URL,  # triggers DEFAULT_URLS list
        timeout=5.0,
        _transport=httpx.MockTransport(handler),
    )
    try:
        result = await c.get_job("abc")
        assert result == {"status": "completed"}
        assert c.server_tag == "api2"
    finally:
        await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,exc", [
    (401, AuthenticationError),
    (402, InsufficientCreditsError),
    (429, RateLimitError),
    (500, ServerError),
    (502, ServerError),
    (418, BoreholeAIError),
])
async def test_error_status_maps_to_exception(status, exc):
    async with _client({
        ("GET", "/v1/jobs/x"): httpx.Response(status, json={"detail": "nope"}),
    }) as c:
        with pytest.raises(exc):
            await c.get_job("x")


@pytest.mark.asyncio
async def test_close_is_idempotent():
    c = _client({})
    await c.close()
    await c.close()  # second close should not raise


@pytest.mark.asyncio
async def test_context_manager_closes():
    c = _client({})
    async with c:
        pass
    # After exit, internal clients should be cleared
    assert c._api_client is None
    assert c._dl_client is None
