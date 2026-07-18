"""End-to-end tests for the public `BoreholeAI.process_documents` API.

Exercises the full Phase 5 wiring: collect_files → run_batch → merge_results →
JobResult, with a fake stateful server via the `_transport` test seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boreholeai import BoreholeAI
from boreholeai import _batch
from boreholeai import client as _client_mod
from boreholeai._manifest import JobEntry, Manifest, STATUS_COMPLETED
from tests.test_batch import FakeServer  # reuse the fake server


# Minimal AGS bytes the fake "download" returns so merge_results can parse it.
_FAKE_AGS = (
    '"GROUP","PROJ"\r\n'
    '"HEADING","PROJ_ID"\r\n'
    '"UNIT",""\r\n'
    '"TYPE","X"\r\n'
    '"DATA","P1"\r\n'
)


class FakeServerWithAgs(FakeServer):
    """FakeServer variant whose 'download' returns valid AGS bytes."""

    def _handle(self, request):
        if request.url.host.startswith("dl.fake"):
            import httpx
            return httpx.Response(200, content=_FAKE_AGS.encode("utf-8"))
        return super()._handle(request)


@pytest.fixture
def fast_polls(monkeypatch):
    monkeypatch.setattr(_batch, "_POLL_INITIAL_INTERVAL", 0.001)
    monkeypatch.setattr(_batch, "_POLL_MAX_INTERVAL", 0.01)
    monkeypatch.setattr(_batch, "_SUBMIT_RETRY_BASE", 0.001)
    monkeypatch.setattr(_batch, "_SUBMIT_RETRY_MAX", 0.01)
    monkeypatch.setattr(_batch, "_RATE_LIMIT_RETRY_BASE", 0.001)
    monkeypatch.setattr(_batch, "_RATE_LIMIT_RETRY_MAX", 0.01)


def _input_dir(tmp_path: Path, names: list[str]) -> Path:
    d = tmp_path / "in"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"%PDF-fake\n")
    return d


def test_single_file_completed(tmp_path, fast_polls):
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    result = client.process_documents(indir, output_dir=tmp_path / "out")

    assert result.status == "completed"
    assert result.successes == ["a.pdf"]
    assert result.failures == {}
    assert len(result.job_ids) == 1
    # Single-job flow copies original file names (not _merged)
    assert any(f.filename == "Borehole_ags4.ags" for f in result.files)


def test_batch_completed_produces_merged_outputs(tmp_path, fast_polls):
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf", "c.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    result = client.process_documents(
        indir, output_dir=tmp_path / "out", finalise_and_cleanup=True,
    )

    assert result.status == "completed"
    assert sorted(result.successes) == ["a.pdf", "b.pdf", "c.pdf"]
    assert len(result.job_ids) == 3
    # Multi-job: AGS gets _merged suffix
    filenames = [f.filename for f in result.files]
    assert "Borehole_ags4_merged.ags" in filenames
    # Workdir cleaned up after a fully successful run with explicit True
    workdir = (tmp_path / "out") / ".boreholeai_workdir"
    assert not workdir.exists()


def test_partial_failure_returns_partial_status(tmp_path, fast_polls):
    server = FakeServerWithAgs(complete_after_polls=1, fail_jobs={"b.pdf"})
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf", "c.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    result = client.process_documents(indir, output_dir=tmp_path / "out")

    assert result.status == "partial"
    assert sorted(result.successes) == ["a.pdf", "c.pdf"]
    assert "b.pdf" in result.failures
    # Workdir KEPT on partial — user may want to inspect or resume
    workdir = (tmp_path / "out") / ".boreholeai_workdir"
    assert workdir.exists()


def test_total_failure_returns_failed_status(tmp_path, fast_polls):
    server = FakeServerWithAgs(auth_fails=True)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    result = client.process_documents(indir, output_dir=tmp_path / "out")

    assert result.status == "failed"
    assert result.successes == []
    assert set(result.failures) == {"a.pdf", "b.pdf"}


def test_explicit_cleanup_deletes_manifest_and_workdir(tmp_path, fast_polls):
    """With finalise_and_cleanup=True, a fully successful run removes both
    the manifest and workdir so output_dir contains only user-facing results."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    out = tmp_path / "out"
    client.process_documents(indir, output_dir=out, finalise_and_cleanup=True)

    assert not (out / ".boreholeai_manifest.json").exists()
    assert not (out / ".boreholeai_workdir").exists()


def test_default_keeps_state_when_not_interactive(tmp_path, fast_polls):
    """Default (None) in a non-TTY context: no prompt, state kept."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    out = tmp_path / "out"
    result = client.process_documents(indir, output_dir=out)

    assert result.status == "completed"
    assert (out / ".boreholeai_manifest.json").exists()
    assert (out / ".boreholeai_workdir").exists()


def test_default_cleans_up_when_prompt_answered_yes(tmp_path, fast_polls, monkeypatch):
    """Default (None) with an interactive 'y' answer: state deleted."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf"])
    monkeypatch.setattr(_client_mod, "_prompt_cleanup", lambda n: True)

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    out = tmp_path / "out"
    result = client.process_documents(indir, output_dir=out)

    assert result.status == "completed"
    assert not (out / ".boreholeai_manifest.json").exists()
    assert not (out / ".boreholeai_workdir").exists()


def test_prompt_cleanup_skips_when_not_a_tty(monkeypatch):
    """No terminal on stdin → never blocks, returns keep."""
    monkeypatch.setattr(_client_mod.sys, "stdin", _FakeTty(tty=False))
    called = []
    monkeypatch.setattr("builtins.input", lambda *a: called.append(1) or "y")

    assert _client_mod._prompt_cleanup(3) is False
    assert called == []  # input() never reached


def test_prompt_cleanup_answers(monkeypatch):
    """y/yes → delete; empty, n, garbage, EOF, Ctrl-C → keep."""
    monkeypatch.setattr(_client_mod.sys, "stdin", _FakeTty(tty=True))
    monkeypatch.setattr(_client_mod.sys, "stderr", _FakeTty(tty=True))

    for answer, expected in [
        ("y", True), ("YES", True), ("", False), ("n", False), ("what", False),
    ]:
        monkeypatch.setattr("builtins.input", lambda *a, _ans=answer: _ans)
        assert _client_mod._prompt_cleanup(1) is expected

    def _raise_eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise_eof)
    assert _client_mod._prompt_cleanup(1) is False

    def _raise_interrupt(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", _raise_interrupt)
    assert _client_mod._prompt_cleanup(1) is False


class _FakeTty:
    """Minimal stand-in for sys.stdin/sys.stderr in prompt tests."""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, s: str) -> int:
        return len(s)

    def flush(self) -> None:
        pass


def test_per_file_progress_shows_one_based_index(monkeypatch):
    """Every progress row identifies its input position, starting from one."""
    stderr = _RecordingTty()
    monkeypatch.setattr(_client_mod.sys, "stderr", stderr)
    renderer = _client_mod._PerFileProgress(started_at=0.0)
    manifest = Manifest(jobs={
        "ARU-K-AH21.pdf": JobEntry(
            status=STATUS_COMPLETED,
            downloaded=True,
            job_id="job-21-uuid",
        ),
        "ARU-K-AH22.pdf": JobEntry(
            status=STATUS_COMPLETED,
            downloaded=True,
            job_id="job-22-uuid",
        ),
    })

    renderer.update(manifest)

    assert "1. ARU-K-AH21.pdf  ✓ done" in stderr.text
    assert "2. ARU-K-AH22.pdf  ✓ done" in stderr.text
    assert "job_id" not in stderr.text


def test_reprocess_progress_waits_for_new_job_frame(monkeypatch):
    """A completed entry marked for redo must not latch the old final frame."""
    stderr = _RecordingTty()
    monkeypatch.setattr(_client_mod.sys, "stderr", stderr)
    renderer = _client_mod._PerFileProgress(started_at=0.0)
    entry = JobEntry(
        status=STATUS_COMPLETED,
        downloaded=True,
        reprocess=True,
        job_id="old-job",
    )
    manifest = Manifest(jobs={"ARU-K-AH21.pdf": entry})

    renderer.update(manifest)
    entry.reprocess = False
    entry.job_id = "new-job"
    renderer.update(manifest)

    assert "ARU-K-AH21.pdf  queued for reprocessing" in stderr.text
    assert "ARU-K-AH21.pdf  ✓ done" in stderr.text


class _RecordingTty(_FakeTty):
    def __init__(self):
        super().__init__(tty=True)
        self.parts: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def write(self, s: str) -> int:
        self.parts.append(s)
        return len(s)


def test_partial_failure_keeps_manifest_for_resume(tmp_path, fast_polls):
    """On partial failures, manifest + workdir are kept so the user can
    re-run and resume (only the failed files get retried)."""
    server = FakeServerWithAgs(complete_after_polls=1, fail_jobs={"b.pdf"})
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    out = tmp_path / "out"
    result = client.process_documents(indir, output_dir=out)

    assert result.status == "partial"
    assert (out / ".boreholeai_manifest.json").exists()
    assert (out / ".boreholeai_workdir").exists()


def test_keep_state_preserves_manifest_and_workdir(tmp_path, fast_polls):
    """finalise_and_cleanup=False keeps resume state even on full success."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    out = tmp_path / "out"
    result = client.process_documents(
        indir, output_dir=out, finalise_and_cleanup=False,
    )

    assert result.status == "completed"
    assert (out / ".boreholeai_manifest.json").exists()
    assert (out / ".boreholeai_workdir").exists()


def test_incremental_rerun_processes_only_new_files(tmp_path, fast_polls):
    """With kept state, files added to the folder later are processed on
    re-run while completed files are skipped, and the merge covers all."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])
    out = tmp_path / "out"

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    r1 = client.process_documents(indir, output_dir=out, finalise_and_cleanup=False)
    assert r1.status == "completed"
    posts_after_first = server.post_count

    (indir / "c.pdf").write_bytes(b"%PDF-fake\n")
    r2 = client.process_documents(indir, output_dir=out, finalise_and_cleanup=False)

    assert r2.status == "completed"
    assert sorted(r2.successes) == ["a.pdf", "b.pdf", "c.pdf"]
    assert server.post_count == posts_after_first + 1  # only c.pdf submitted


def test_final_cleanup_run_resubmits_nothing_and_cleans(tmp_path, fast_polls):
    """A finalise_and_cleanup=True re-run over fully-kept state submits
    nothing, re-merges, and removes manifest + workdir."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])
    out = tmp_path / "out"

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    client.process_documents(indir, output_dir=out, finalise_and_cleanup=False)
    posts_after_first = server.post_count

    result = client.process_documents(indir, output_dir=out, finalise_and_cleanup=True)

    assert result.status == "completed"
    assert sorted(result.successes) == ["a.pdf", "b.pdf"]
    assert server.post_count == posts_after_first  # zero new submits
    assert not (out / ".boreholeai_manifest.json").exists()
    assert not (out / ".boreholeai_workdir").exists()


def test_empty_api_key_rejected():
    with pytest.raises(ValueError, match="api_key is required"):
        BoreholeAI(api_key="")
