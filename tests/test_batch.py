"""Tests for `_batch.run_batch` — fan-out + poll + download orchestration.

Uses httpx.MockTransport via the APIClientAsync `_transport` test seam
to simulate a fake server with controllable per-job behavior.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from boreholeai import _batch, _manifest
from boreholeai._api import APIClientAsync, DEFAULT_BASE_URL
from boreholeai._batch import run_batch


class FakeServer:
    """Stateful mock server. Tracks job state and returns realistic responses.

    Args:
      complete_after_polls: each job becomes "completed" after this many GETs.
      fail_jobs: set of input filenames that should be reported as 'failed'.
      submit_429_count: first N POSTs return 429 (test rate-limit retry).
      auth_fails: if True, all POSTs return 401.
      purge_jobs: jobs whose `get_results` should set purge_on_download=True.
    """

    def __init__(
        self, *,
        complete_after_polls: int = 1,
        fail_jobs: set[str] = frozenset(),
        submit_429_count: int = 0,
        auth_fails: bool = False,
        purge_jobs: set[str] = frozenset(),
    ):
        self._complete_after = complete_after_polls
        self._fail_jobs = set(fail_jobs)
        self._submit_429_remaining = submit_429_count
        self._auth_fails = auth_fails
        self._purge_jobs = set(purge_jobs)
        # state
        self.jobs: dict[str, dict] = {}    # job_id -> {filename, polls, status}
        self.next_id = 0
        self.post_count = 0
        self.delete_count = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/health":
            return httpx.Response(200, json={"ok": True})

        # Signed-URL downloads come through the same transport
        if req.url.host.startswith("dl.fake"):
            return httpx.Response(200, content=b"merged-bytes")

        if req.method == "POST" and path == "/v1/jobs":
            self.post_count += 1
            if self._auth_fails:
                return httpx.Response(401, json={"detail": "bad key"})
            if self._submit_429_remaining > 0:
                self._submit_429_remaining -= 1
                return httpx.Response(429, json={"detail": "slow down"})

            # Read filename from multipart body for state tracking
            filename = self._extract_filename(req)
            self.next_id += 1
            jid = f"j{self.next_id}"
            self.jobs[jid] = {"filename": filename, "polls": 0, "status": "queued"}
            return httpx.Response(202, json={
                "job_id": jid, "num_pages": 3, "credits_remaining": 100,
            })

        if req.method == "GET" and path.startswith("/v1/jobs/") and path.endswith("/results"):
            jid = path.split("/")[-2]
            return httpx.Response(200, json={
                "files": [{"filename": "Borehole_ags4.ags", "url": f"https://dl.fake/{jid}"}],
                "purge_on_download": (
                    self.jobs[jid]["filename"] in self._purge_jobs
                ),
            })

        if req.method == "DELETE" and path.startswith("/v1/jobs/"):
            self.delete_count += 1
            return httpx.Response(200, json={"files_deleted": 1, "already_purged": False})

        if req.method == "GET" and path.startswith("/v1/jobs/"):
            jid = path.split("/")[-1]
            j = self.jobs[jid]
            j["polls"] += 1
            if j["filename"] in self._fail_jobs:
                j["status"] = "failed"
                return httpx.Response(200, json={
                    "status": "failed",
                    "error_message": "fake pipeline error",
                })
            if j["polls"] >= self._complete_after:
                j["status"] = "completed"
            return httpx.Response(200, json={
                "status": j["status"],
                "num_pages": 3,
                "progress": {"pages_done": 3 if j["status"] == "completed" else 1,
                             "pages_total": 3},
            })

        return httpx.Response(404)

    @staticmethod
    def _extract_filename(req: httpx.Request) -> str:
        """Find filename in multipart body (good enough for tests)."""
        body = req.content.decode("utf-8", errors="ignore")
        for line in body.splitlines():
            if 'filename="' in line:
                return line.split('filename="', 1)[1].split('"', 1)[0]
        return "unknown"


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    out = []
    for n in ["a.pdf", "b.pdf", "c.pdf"]:
        p = tmp_path / n
        p.write_bytes(b"%PDF-fake\n")
        out.append(p)
    return out


@pytest.fixture
def fast_polls(monkeypatch):
    """Make polling near-instant so tests don't hang."""
    monkeypatch.setattr(_batch, "_POLL_INITIAL_INTERVAL", 0.001)
    monkeypatch.setattr(_batch, "_POLL_MAX_INTERVAL", 0.01)
    monkeypatch.setattr(_batch, "_SUBMIT_RETRY_BASE", 0.001)
    monkeypatch.setattr(_batch, "_SUBMIT_RETRY_MAX", 0.01)


def _client(server: FakeServer) -> APIClientAsync:
    return APIClientAsync(
        api_key="bhai_test", base_url=DEFAULT_BASE_URL, timeout=5.0,
        _transport=server.transport(),
    )


# --- happy path ---

async def test_all_files_complete(files, tmp_path, fast_polls):
    server = FakeServer(complete_after_polls=2)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, files, out, concurrency=2)

    assert sorted(result.successes) == ["a.pdf", "b.pdf", "c.pdf"]
    assert result.failures == {}
    assert len(result.job_ids) == 3
    # Each job's workdir contains the downloaded file
    for jid in result.job_ids.values():
        assert (result.workdir / jid / "Borehole_ags4.ags").exists()


async def test_manifest_persisted_after_run(files, tmp_path, fast_polls):
    server = FakeServer(complete_after_polls=1)
    out = tmp_path / "out"

    async with _client(server) as client:
        await run_batch(client, files, out)

    manifest_path = out / _manifest.MANIFEST_FILENAME
    assert manifest_path.exists()
    m = _manifest.load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    for f in files:
        assert m.is_done(f.name)


# --- partial failure ---

async def test_one_file_fails_others_succeed(files, tmp_path, fast_polls):
    server = FakeServer(complete_after_polls=1, fail_jobs={"b.pdf"})
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, files, out)

    assert sorted(result.successes) == ["a.pdf", "c.pdf"]
    assert "b.pdf" in result.failures
    assert "fake pipeline error" in result.failures["b.pdf"]


# --- rate limit retry ---

async def test_rate_limit_retries_and_succeeds(files, tmp_path, fast_polls):
    # First 2 POSTs return 429, then success
    server = FakeServer(complete_after_polls=1, submit_429_count=2)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == ["a.pdf"]
    # 1 success + 2 429s = 3 POSTs total
    assert server.post_count == 3


# --- fatal auth failure ---

async def test_auth_failure_marks_all_submit_failed(files, tmp_path, fast_polls):
    server = FakeServer(auth_fails=True)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, files, out)

    assert result.successes == []
    assert set(result.failures) == {"a.pdf", "b.pdf", "c.pdf"}
    for msg in result.failures.values():
        assert "Invalid" in msg or "401" in msg


# --- enterprise purge ---

async def test_purge_on_download_calls_delete(files, tmp_path, fast_polls):
    server = FakeServer(complete_after_polls=1, purge_jobs={"a.pdf"})
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == ["a.pdf"]
    assert server.delete_count == 1
    assert result.manifest.jobs["a.pdf"].purged is True


# --- resume ---

async def test_resume_skips_already_done(files, tmp_path, fast_polls):
    """If manifest says one file is already done, no new POST for it."""
    out = tmp_path / "out"
    out.mkdir()

    # Pre-seed manifest as if a.pdf was already completed in a prior run
    pre = _manifest.load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    pre.jobs["a.pdf"].job_id = "preexisting-job"
    pre.jobs["a.pdf"].status = _manifest.STATUS_COMPLETED
    pre.jobs["a.pdf"].downloaded = True
    _manifest.save(pre, out)

    server = FakeServer(complete_after_polls=1)
    async with _client(server) as client:
        result = await run_batch(client, files, out)

    # Only 2 POSTs (b, c) — a was skipped
    assert server.post_count == 2
    assert "a.pdf" in result.successes


async def test_resume_continues_polling_existing_job(files, tmp_path, fast_polls):
    """Manifest with status=submitted + job_id should resume polling, not POST again."""
    out = tmp_path / "out"
    out.mkdir()

    server = FakeServer(complete_after_polls=1)
    # Seed a fake job in the server's state matching the manifest
    server.jobs["existing-job-1"] = {
        "filename": "a.pdf", "polls": 0, "status": "queued",
    }

    pre = _manifest.load_or_init(out, input_root=tmp_path, concurrency=6, files=[files[0]])
    pre.jobs["a.pdf"].job_id = "existing-job-1"
    pre.jobs["a.pdf"].status = _manifest.STATUS_SUBMITTED
    _manifest.save(pre, out)

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    # No new POST — the existing job_id was reused
    assert server.post_count == 0
    assert result.successes == ["a.pdf"]


# --- bad input ---

async def test_empty_input_raises(tmp_path):
    server = FakeServer()
    async with _client(server) as client:
        with pytest.raises(ValueError):
            await run_batch(client, [], tmp_path / "out")
