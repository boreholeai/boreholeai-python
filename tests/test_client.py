"""End-to-end tests for the public `BoreholeAI.process_documents` API.

Exercises the full Phase 5 wiring: collect_files → run_batch → merge_results →
JobResult, with a fake stateful server via the `_transport` test seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boreholeai import BoreholeAI
from boreholeai import _batch
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
    result = client.process_documents(indir, output_dir=tmp_path / "out", concurrency=2)

    assert result.status == "completed"
    assert sorted(result.successes) == ["a.pdf", "b.pdf", "c.pdf"]
    assert len(result.job_ids) == 3
    # Multi-job: AGS gets _merged suffix
    filenames = [f.filename for f in result.files]
    assert "Borehole_ags4_merged.ags" in filenames
    # Workdir cleaned up after a fully successful run
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


def test_successful_run_deletes_manifest_and_workdir(tmp_path, fast_polls):
    """On a fully successful run, both the manifest and workdir are removed
    so output_dir contains only the user-facing results."""
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf", "b.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    out = tmp_path / "out"
    client.process_documents(indir, output_dir=out)

    assert not (out / ".boreholeai_manifest.json").exists()
    assert not (out / ".boreholeai_workdir").exists()


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


def test_concurrency_parameter_accepted(tmp_path, fast_polls):
    server = FakeServerWithAgs(complete_after_polls=1)
    indir = _input_dir(tmp_path, ["a.pdf"])

    client = BoreholeAI(api_key="bhai_test", base_url="https://api1.boreholeai.com",
                         _transport=server.transport())
    result = client.process_documents(indir, output_dir=tmp_path / "out", concurrency=10)

    assert result.status == "completed"


def test_empty_api_key_rejected():
    with pytest.raises(ValueError, match="api_key is required"):
        BoreholeAI(api_key="")
