"""Tests for `_batch.run_batch` — fan-out + poll + download orchestration.

Uses httpx.MockTransport via the APIClientAsync `_transport` test seam
to simulate a fake server with controllable per-job behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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
      submit_neterr_count: first N POSTs raise a transport error.
      poll_neterr_count: first N status GETs raise a transport error.
      download_neterr_count: first N signed-URL GETs raise a transport error.
      poll_http_status: if set, every status GET returns this HTTP code.
      poll_bad_body_count: first N status GETs return 200 with an HTML body.
      poll_unknown_status_count: first N status GETs return 200 with an
        unrecognised status value.
      submit_no_jobid_count: first N POSTs return 202 without a job_id.
      progress_junk: status GETs report non-numeric progress fields.
      results_file_entry: if set, get_results returns exactly this one entry.
      results_files_raw: if not None, used verbatim as the results "files"
        value (for malformed / empty list tests).
    """

    def __init__(
        self, *,
        complete_after_polls: int = 1,
        fail_jobs: set[str] = frozenset(),
        submit_429_count: int = 0,
        auth_fails: bool = False,
        purge_jobs: set[str] = frozenset(),
        max_concurrent_jobs: Optional[int] = None,
        submit_neterr_count: int = 0,
        poll_neterr_count: int = 0,
        download_neterr_count: int = 0,
        poll_http_status: Optional[int] = None,
        poll_bad_body_count: int = 0,
        poll_unknown_status_count: int = 0,
        submit_no_jobid_count: int = 0,
        progress_junk: bool = False,
        results_file_entry: Optional[dict] = None,
        results_files_raw: object = None,
        stuck_processing: bool = False,
        progress_ticks: bool = False,
        download_bytes: Optional[bytes] = None,
    ):
        self._complete_after = complete_after_polls
        self._fail_jobs = set(fail_jobs)
        self._submit_429_remaining = submit_429_count
        self._auth_fails = auth_fails
        self._purge_jobs = set(purge_jobs)
        self._max_concurrent_jobs = max_concurrent_jobs
        self._submit_neterr_remaining = submit_neterr_count
        self._poll_neterr_remaining = poll_neterr_count
        self._download_neterr_remaining = download_neterr_count
        self._poll_http_status = poll_http_status
        self._poll_bad_body_remaining = poll_bad_body_count
        self._poll_unknown_status_remaining = poll_unknown_status_count
        self._submit_no_jobid_remaining = submit_no_jobid_count
        self._progress_junk = progress_junk
        self._results_file_entry = results_file_entry
        self._results_files_raw = results_files_raw
        self._stuck_processing = stuck_processing
        self._progress_ticks = progress_ticks
        self._download_bytes = download_bytes
        # state
        self.jobs: dict[str, dict] = {}    # job_id -> {filename, polls, status}
        self.next_id = 0
        self.post_count = 0
        self.delete_count = 0
        self.me_count = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/health":
            return httpx.Response(200, json={"ok": True})

        if path == "/v1/me":
            self.me_count += 1
            if self._max_concurrent_jobs is None:
                return httpx.Response(404)  # simulate endpoint missing
            return httpx.Response(200, json={
                "user_id": "fake-user",
                "max_concurrent_jobs": self._max_concurrent_jobs,
                "purge_on_download": False,
            })

        # Signed-URL downloads come through the same transport
        if req.url.host.startswith("dl.fake"):
            if self._download_neterr_remaining > 0:
                self._download_neterr_remaining -= 1
                raise httpx.ReadTimeout("simulated network drop", request=req)
            if self._download_bytes is not None:
                return httpx.Response(200, content=self._download_bytes)
            return httpx.Response(200, content=b"merged-bytes")

        if req.method == "POST" and path == "/v1/jobs":
            self.post_count += 1
            if self._auth_fails:
                return httpx.Response(401, json={"detail": "bad key"})
            if self._submit_neterr_remaining > 0:
                self._submit_neterr_remaining -= 1
                raise httpx.ConnectError("simulated network drop", request=req)
            if self._submit_429_remaining > 0:
                self._submit_429_remaining -= 1
                return httpx.Response(429, json={"detail": "slow down"})
            if self._submit_no_jobid_remaining > 0:
                self._submit_no_jobid_remaining -= 1
                return httpx.Response(202, json={"credits_remaining": 100})

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
            file_entry = self._results_file_entry
            if file_entry is None:
                file_entry = {"filename": "Borehole_ags4.ags", "url": f"https://dl.fake/{jid}"}
            files_value = [file_entry]
            if self._results_files_raw is not None:
                files_value = self._results_files_raw
            return httpx.Response(200, json={
                "files": files_value,
                "purge_on_download": (
                    self.jobs[jid]["filename"] in self._purge_jobs
                ),
            })

        if req.method == "DELETE" and path.startswith("/v1/jobs/"):
            self.delete_count += 1
            return httpx.Response(200, json={"files_deleted": 1, "already_purged": False})

        if req.method == "GET" and path.startswith("/v1/jobs/"):
            if self._poll_neterr_remaining > 0:
                self._poll_neterr_remaining -= 1
                raise httpx.ReadTimeout("simulated network drop", request=req)
            if self._poll_http_status is not None:
                return httpx.Response(
                    self._poll_http_status, json={"detail": "poll rejected"},
                )
            if self._poll_bad_body_remaining > 0:
                self._poll_bad_body_remaining -= 1
                return httpx.Response(200, content=b"<html>502 Bad Gateway</html>")
            if self._poll_unknown_status_remaining > 0:
                self._poll_unknown_status_remaining -= 1
                return httpx.Response(200, json={"status": "reticulating"})
            if self._stuck_processing:
                # Dead-worker simulation: forever "processing", frozen progress
                return httpx.Response(200, json={
                    "status": "processing",
                    "progress": {"pages_done": 1, "pages_total": 3},
                })
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
            progress = {"pages_done": 3 if j["status"] == "completed" else 1,
                        "pages_total": 3}
            if self._progress_ticks:
                progress["pages_done"] = j["polls"]  # changes every poll
            if self._progress_junk:
                progress = {"page": "N/A", "pages_total": "many",
                            "pages_done": None, "completed_subgraphs": "nope"}
            return httpx.Response(200, json={
                "status": j["status"],
                "num_pages": 3,
                "progress": progress,
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
    monkeypatch.setattr(_batch, "_DOWNLOAD_RETRY_BASE", 0.001)
    monkeypatch.setattr(_batch, "_DOWNLOAD_RETRY_MAX", 0.01)
    monkeypatch.setattr(_batch, "_RATE_LIMIT_RETRY_BASE", 0.001)
    monkeypatch.setattr(_batch, "_RATE_LIMIT_RETRY_MAX", 0.01)


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
    for name, jid in result.job_ids.items():
        assert (
            result.workdir / f"{name} {jid}" / "Borehole_ags4.ags"
        ).exists()


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


async def test_rate_limit_waits_beyond_any_attempt_budget(files, tmp_path, fast_polls):
    """429 is backpressure — a file waits out a long cap-full stretch (here
    8 rejections, beyond the old 5-attempt budget) and still succeeds."""
    server = FakeServer(complete_after_polls=1, submit_429_count=8)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == ["a.pdf"]
    assert server.post_count == 9


async def test_rate_limit_gives_up_only_when_batch_stalled(
    files, tmp_path, fast_polls, monkeypatch,
):
    """If submits are refused AND nothing in the batch progresses for the
    stall budget, the cap is stuck (zombie jobs) — fail with a pointed
    message instead of waiting forever."""
    monkeypatch.setattr(_batch, "_RATE_LIMIT_STALL_BUDGET", 0.05)
    server = FakeServer(submit_429_count=10**6)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == []
    assert "concurrency cap appears stuck" in result.failures["a.pdf"]


# --- transient network errors (httpx.TransportError) ---

async def test_submit_retries_on_transport_error(files, tmp_path, fast_polls):
    # First 2 POSTs drop at the transport level, then success
    server = FakeServer(complete_after_polls=1, submit_neterr_count=2)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == ["a.pdf"]
    assert server.post_count == 3


async def test_submit_exhausted_transport_retries_marks_failed_not_crash(
    files, tmp_path, fast_polls,
):
    # Every POST drops — file must end up in failures, batch must not raise
    server = FakeServer(submit_neterr_count=99)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == []
    assert "a.pdf" in result.failures


async def test_missing_source_file_fails_that_file_only(files, tmp_path, fast_polls):
    """A file deleted after collection (mid-run folder edit) must not kill
    the batch: it's marked submit_failed and the other files proceed."""
    out = tmp_path / "out"
    files[1].unlink()  # b.pdf vanishes before its submit slot

    server = FakeServer(complete_after_polls=1)
    async with _client(server) as client:
        result = await run_batch(client, files, out)

    assert sorted(result.successes) == ["a.pdf", "c.pdf"]
    assert "b.pdf" in result.failures
    assert "missing or unreadable" in result.failures["b.pdf"]


async def test_poll_survives_transport_errors(files, tmp_path, fast_polls):
    # First 3 status GETs drop mid-poll; polling must ride through
    server = FakeServer(complete_after_polls=2, poll_neterr_count=3)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, files, out)

    assert sorted(result.successes) == ["a.pdf", "b.pdf", "c.pdf"]
    assert result.failures == {}


async def test_download_retries_on_transport_error(files, tmp_path, fast_polls):
    # First 2 signed-URL GETs drop, third attempt succeeds
    server = FakeServer(complete_after_polls=1, download_neterr_count=2)
    out = tmp_path / "out"

    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == ["a.pdf"]
    assert result.manifest.jobs["a.pdf"].downloaded


# --- poll error taxonomy (permanent verdicts, error streak, contract drift) ---

async def test_poll_permanent_http_fails_file_fast_no_hang(files, tmp_path, fast_polls):
    """401/402/404 on poll are final answers — fail the file and release the
    slot instead of retrying forever (the silent-hang failure mode)."""
    for code in (401, 402, 404):
        server = FakeServer(poll_http_status=code)
        out = tmp_path / f"out{code}"
        async with _client(server) as client:
            result = await run_batch(client, [files[0]], out)

        assert result.successes == []
        assert f"poll rejected (HTTP {code})" in result.failures["a.pdf"]


async def test_poll_error_streak_gives_up(files, tmp_path, fast_polls, monkeypatch):
    """Unbroken transient poll errors eventually fail the file instead of
    spinning forever while holding the concurrency slot."""
    monkeypatch.setattr(_batch, "_POLL_MAX_ERROR_STREAK", 3)
    server = FakeServer(poll_neterr_count=99)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert "gave up after 3 consecutive errors" in result.failures["a.pdf"]


async def test_poll_html_body_retries_then_completes(files, tmp_path, fast_polls):
    """A proxy answering 200 with an HTML page is transient — ride it out."""
    server = FakeServer(complete_after_polls=1, poll_bad_body_count=2)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]


async def test_poll_unknown_status_retries_then_completes(files, tmp_path, fast_polls):
    """An unrecognised status value is contract drift: never written to the
    manifest, retried like a server fault, recovered when sanity returns."""
    server = FakeServer(complete_after_polls=1, poll_unknown_status_count=2)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]
    assert result.manifest.jobs["a.pdf"].status == "completed"


async def test_progress_junk_is_ignored(files, tmp_path, fast_polls):
    """Progress fields are cosmetic — junk values must never fail a file."""
    server = FakeServer(complete_after_polls=2, progress_junk=True)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]


async def test_stuck_job_fails_after_stagnation_budget(
    files, tmp_path, fast_polls, monkeypatch,
):
    """A job whose worker died answers 'processing' with frozen progress
    forever — the guard fails it client-side instead of polling for days."""
    monkeypatch.setattr(_batch, "_STUCK_PROCESSING_BUDGET", 0.03)
    monkeypatch.setattr(_batch, "_STUCK_QUEUED_BUDGET", 0.03)
    server = FakeServer(stuck_processing=True)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == []
    assert "appears stuck" in result.failures["a.pdf"]


async def test_slow_but_progressing_job_not_marked_stuck(
    files, tmp_path, fast_polls, monkeypatch,
):
    """A long job whose progress keeps ticking resets the stagnation clock
    on every observable change — it must never be declared stuck."""
    monkeypatch.setattr(_batch, "_STUCK_PROCESSING_BUDGET", 0.05)
    monkeypatch.setattr(_batch, "_STUCK_QUEUED_BUDGET", 0.05)
    server = FakeServer(complete_after_polls=30, progress_ticks=True)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]


# --- malformed server responses (submit / results contract) ---

async def test_submit_missing_job_id_retries_then_succeeds(files, tmp_path, fast_polls):
    """A 202 without job_id is treated like a 5xx: retried, not fatal."""
    server = FakeServer(complete_after_polls=1, submit_no_jobid_count=2)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]
    assert server.post_count == 3


async def test_download_missing_filename_fails_file_not_batch(files, tmp_path, fast_polls):
    server = FakeServer(
        complete_after_polls=1,
        results_file_entry={"url": "https://dl.fake/x"},  # no filename
    )
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert "a.pdf" in result.failures  # failed cleanly, batch survived


async def test_results_files_not_a_list_fails_file(files, tmp_path, fast_polls):
    """A malformed `files` value must fail the file, not count as a
    zero-artifact success that silently drops the borehole from the merge."""
    server = FakeServer(complete_after_polls=1, results_files_raw={"oops": 1})
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == []
    assert "a.pdf" in result.failures
    assert result.manifest.jobs["a.pdf"].downloaded is False


async def test_results_empty_file_list_fails_file(files, tmp_path, fast_polls):
    """A completed job with zero result files is a broken contract."""
    server = FakeServer(complete_after_polls=1, results_files_raw=[])
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == []
    assert "a.pdf" in result.failures
    assert result.manifest.jobs["a.pdf"].downloaded is False


async def test_download_traversal_filename_stays_in_workdir(files, tmp_path, fast_polls):
    """A ../-laden filename from the server is flattened to its basename."""
    server = FakeServer(
        complete_after_polls=1,
        results_file_entry={"filename": "../../evil.ags", "url": "https://dl.fake/x"},
    )
    out = tmp_path / "out"
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert result.successes == ["a.pdf"]
    job_id = result.manifest.jobs["a.pdf"].job_id
    assert (
        out / ".boreholeai_workdir" / f"a.pdf {job_id}" / "evil.ags"
    ).exists()
    assert not (out / "evil.ags").exists()  # escape attempt contained


def _gp_xlsx_with_processing_info(failed: int, total: int = 2, names=()) -> bytes:
    """Build a minimal Borehole_ground_profile.xlsx with a Processing Info
    sheet, mirroring the backend's stats layout."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Ground Profile"
    ws.append(["Hole_ID", "material"])
    pi = wb.create_sheet("Processing Info")
    pi.append(["Metric", "Value"])
    pi.append(["Total Boreholes", str(total)])
    pi.append(["Boreholes Digitalised", str(total - failed)])
    pi.append(["Boreholes Failed", str(failed)])
    if names:
        pi.append(["— Failed Boreholes —", ""])
        for n in names:
            pi.append([f"  {n}", "page failed"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_page_failure_warning_recorded_on_completed_job(
    files, tmp_path, fast_polls,
):
    """A completed job whose Processing Info sheet reports failed pages gets
    an informational warning — still a success, never a failure."""
    payload = _gp_xlsx_with_processing_info(failed=1, names=["BH1_page_001"])
    server = FakeServer(
        complete_after_polls=1,
        results_file_entry={
            "filename": "Borehole_ground_profile.xlsx", "url": "https://dl.fake/x",
        },
        download_bytes=payload,
    )
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]
    entry = result.manifest.jobs["a.pdf"]
    assert entry.warning is not None
    assert "1 of 2" in entry.warning
    assert "BH1_page_001" in entry.warning


async def test_no_warning_when_all_pages_digitised(files, tmp_path, fast_polls):
    payload = _gp_xlsx_with_processing_info(failed=0)
    server = FakeServer(
        complete_after_polls=1,
        results_file_entry={
            "filename": "Borehole_ground_profile.xlsx", "url": "https://dl.fake/x",
        },
        download_bytes=payload,
    )
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], tmp_path / "out")

    assert result.successes == ["a.pdf"]
    assert result.manifest.jobs["a.pdf"].warning is None


# --- last-resort firewall ---

async def test_unexpected_error_fails_one_file_not_batch(
    files, tmp_path, fast_polls, monkeypatch,
):
    """Anything unanticipated fails one file; the other files still finish."""
    real_submit = _batch._submit_one

    async def poisoned(name, *args, **kwargs):
        if name == "b.pdf":
            raise RuntimeError("boom")
        return await real_submit(name, *args, **kwargs)

    monkeypatch.setattr(_batch, "_submit_one", poisoned)
    server = FakeServer(complete_after_polls=1)
    async with _client(server) as client:
        result = await run_batch(client, files, tmp_path / "out")

    assert sorted(result.successes) == ["a.pdf", "c.pdf"]
    assert "unexpected error" in result.failures["b.pdf"]
    assert "boom" in result.failures["b.pdf"]


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


async def test_resume_retries_server_side_failed_job(files, tmp_path, fast_polls):
    """Manifest with status=failed should re-POST on resume (not stay sticky)."""
    out = tmp_path / "out"
    out.mkdir()

    pre = _manifest.load_or_init(
        out, input_root=tmp_path, concurrency=6, files=[files[0]],
    )
    pre.jobs["a.pdf"].job_id = "old-failed-job"
    pre.jobs["a.pdf"].status = _manifest.STATUS_FAILED
    pre.jobs["a.pdf"].error = "The write operation timed out"
    _manifest.save(pre, out)

    server = FakeServer(complete_after_polls=1)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    # Re-POSTed once and now succeeds; old job_id is replaced.
    assert server.post_count == 1
    assert result.successes == ["a.pdf"]
    assert result.manifest.jobs["a.pdf"].job_id != "old-failed-job"
    assert result.manifest.jobs["a.pdf"].error is None


async def test_reprocess_flag_forces_rerun_and_redownload(
    files, tmp_path, fast_polls,
):
    """Setting `reprocess: true` on a completed entry (the documented manual
    redo switch) re-runs just that file. Stale downloaded/purged flags from
    the old job must not survive the resubmit, or the new results would never
    be pulled; the reprocess flag itself is consumed by the resubmit."""
    out = tmp_path / "out"
    out.mkdir()

    pre = _manifest.load_or_init(
        out, input_root=tmp_path, concurrency=6, files=[files[0]],
    )
    pre.jobs["a.pdf"].job_id = "old-completed-job"
    pre.jobs["a.pdf"].status = _manifest.STATUS_COMPLETED
    pre.jobs["a.pdf"].downloaded = True
    pre.jobs["a.pdf"].purged = True
    pre.jobs["a.pdf"].completed_at = "2026-01-01T00:00:00+00:00"
    pre.jobs["a.pdf"].pages_done = 3
    pre.jobs["a.pdf"].current_page = 3
    pre.jobs["a.pdf"].pages_total = 3
    pre.jobs["a.pdf"].completed_subgraphs = ["SG01", "SG02"]
    pre.jobs["a.pdf"].reprocess = True   # the manual edit
    _manifest.save(pre, out)
    labelled_old_cache = (
        out / ".boreholeai_workdir" / "a.pdf old-completed-job"
    )
    legacy_old_cache = out / ".boreholeai_workdir" / "old-completed-job"
    labelled_old_cache.mkdir(parents=True)
    legacy_old_cache.mkdir()
    (labelled_old_cache / "stale-result.ags").write_text("stale")
    (legacy_old_cache / "stale-result.ags").write_text("stale")

    server = FakeServer(complete_after_polls=1)
    async with _client(server) as client:
        result = await run_batch(client, [files[0]], out)

    assert server.post_count == 1
    assert result.successes == ["a.pdf"]
    entry = result.manifest.jobs["a.pdf"]
    assert entry.job_id != "old-completed-job"
    assert entry.downloaded  # new job's results actually downloaded
    assert entry.reprocess is False  # flag consumed — won't loop forever
    assert entry.completed_at != "2026-01-01T00:00:00+00:00"
    assert entry.completed_subgraphs == []
    assert not labelled_old_cache.exists()
    assert not legacy_old_cache.exists()
    assert (Path(result.workdir) / f"a.pdf {entry.job_id}").exists()


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


# --- cap-aware pacing (resolve_effective_concurrency) ---

async def test_resolve_effective_concurrency_uses_server_cap_when_lower(tmp_path):
    from boreholeai._batch import resolve_effective_concurrency
    server = FakeServer(max_concurrent_jobs=2)
    async with _client(server) as client:
        effective, server_cap = await resolve_effective_concurrency(client, user_concurrency=10)
    assert effective == 2
    assert server_cap == 2
    assert server.me_count == 1


async def test_resolve_effective_concurrency_keeps_user_when_lower(tmp_path):
    from boreholeai._batch import resolve_effective_concurrency
    server = FakeServer(max_concurrent_jobs=10)
    async with _client(server) as client:
        effective, server_cap = await resolve_effective_concurrency(client, user_concurrency=3)
    assert effective == 3
    assert server_cap == 10


async def test_resolve_effective_concurrency_falls_back_when_endpoint_missing(tmp_path):
    """If /v1/me returns 404 (older backend), don't break — use user value."""
    from boreholeai._batch import resolve_effective_concurrency
    server = FakeServer(max_concurrent_jobs=None)  # endpoint returns 404
    async with _client(server) as client:
        effective, server_cap = await resolve_effective_concurrency(client, user_concurrency=6)
    assert effective == 6
    assert server_cap is None


async def test_resolve_effective_concurrency_zero_cap_raises(tmp_path):
    """A cap of 0 means the account is locked out — fail clearly."""
    from boreholeai._batch import resolve_effective_concurrency
    from boreholeai.exceptions import BoreholeAIError
    server = FakeServer(max_concurrent_jobs=0)
    async with _client(server) as client:
        with pytest.raises(BoreholeAIError, match="no concurrency budget"):
            await resolve_effective_concurrency(client, user_concurrency=6)
