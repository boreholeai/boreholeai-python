"""Tests for the persisted batch manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boreholeai._manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SUBMITTED,
    STATUS_SUBMIT_FAILED,
    JobEntry,
    Manifest,
    load,
    load_or_init,
    save,
)


def _files(tmp_path: Path, names: list[str]) -> list[Path]:
    """Create empty placeholder files in `tmp_path` and return their Paths."""
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"")
        out.append(p)
    return out


# --- init ---

def test_init_creates_manifest_file(tmp_path):
    files = _files(tmp_path, ["a.pdf", "b.pdf"])
    out = tmp_path / "out"

    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)

    assert (out / MANIFEST_FILENAME).exists()
    assert m.version == MANIFEST_VERSION
    assert m.concurrency == 6
    assert set(m.jobs) == {"a.pdf", "b.pdf"}
    assert all(e.status == STATUS_PENDING for e in m.jobs.values())


def test_init_records_paths_as_absolute(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"

    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)

    assert Path(m.input_root).is_absolute()
    assert Path(m.output_dir).is_absolute()


# --- load: roundtrip ---

def test_load_returns_saved_state(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"

    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    m.jobs["a.pdf"].job_id = "uuid-1"
    m.jobs["a.pdf"].status = STATUS_PROCESSING
    m.jobs["a.pdf"].num_pages = 5
    save(m, out)

    m2 = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    assert m2.jobs["a.pdf"].job_id == "uuid-1"
    assert m2.jobs["a.pdf"].status == STATUS_PROCESSING
    assert m2.jobs["a.pdf"].num_pages == 5


def test_load_adds_entries_for_new_files(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    m.jobs["a.pdf"].status = STATUS_COMPLETED
    save(m, out)

    # Add a new file (without touching a.pdf), re-load
    new_files = files + _files(tmp_path, ["b.pdf"])
    m2 = load_or_init(out, input_root=tmp_path, concurrency=6, files=new_files)

    assert m2.jobs["a.pdf"].status == STATUS_COMPLETED  # preserved
    assert m2.jobs["b.pdf"].status == STATUS_PENDING    # new


def test_load_rejects_unknown_version(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / MANIFEST_FILENAME).write_text(json.dumps({"version": 999, "jobs": {}}))

    with pytest.raises(ValueError, match="unsupported manifest version"):
        load_or_init(out, input_root=tmp_path, concurrency=6, files=[])


def test_load_raises_on_input_root_mismatch(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    load_or_init(out, input_root=tmp_path, concurrency=6, files=files)

    # Re-load with a different input_root — a kept manifest must never
    # silently skip same-named files from an unrelated folder.
    other_root = tmp_path / "other"
    other_root.mkdir()
    with pytest.raises(ValueError, match="different input folder"):
        load_or_init(out, input_root=other_root, concurrency=6, files=[])


# --- source-file fingerprint (incremental reruns) ---

def test_load_resets_entry_when_source_file_changed(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    m.jobs["a.pdf"].status = STATUS_COMPLETED
    m.jobs["a.pdf"].job_id = "old-job"
    m.jobs["a.pdf"].downloaded = True
    save(m, out)

    files[0].write_bytes(b"replacement scan")  # same name, new content

    m2 = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    assert m2.jobs["a.pdf"].status == STATUS_PENDING
    assert m2.jobs["a.pdf"].job_id is None
    assert m2.jobs["a.pdf"].downloaded is False


def test_load_keeps_entry_when_source_unchanged(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    m.jobs["a.pdf"].status = STATUS_COMPLETED
    m.jobs["a.pdf"].job_id = "job-1"
    m.jobs["a.pdf"].downloaded = True
    save(m, out)

    m2 = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    assert m2.jobs["a.pdf"].status == STATUS_COMPLETED
    assert m2.jobs["a.pdf"].job_id == "job-1"


def test_needs_submit_when_reprocess_flag_set(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    e = m.jobs["a.pdf"]
    e.status = STATUS_COMPLETED
    e.job_id = "job-1"
    e.downloaded = True

    assert not m.needs_submit("a.pdf")
    e.reprocess = True
    assert m.needs_submit("a.pdf")


def test_load_never_resets_legacy_entries_without_fingerprint(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    entry = m.jobs["a.pdf"]
    entry.status = STATUS_COMPLETED
    entry.downloaded = True
    entry.src_size = None       # simulate a manifest written pre-fingerprint
    entry.src_mtime = None
    save(m, out)

    files[0].write_bytes(b"changed after the fact")

    m2 = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)
    assert m2.jobs["a.pdf"].status == STATUS_COMPLETED  # not reset


# --- atomic write ---

def test_save_is_atomic_no_tmp_files_left(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)

    for _ in range(3):
        m.jobs["a.pdf"].num_pages = (m.jobs["a.pdf"].num_pages or 0) + 1
        save(m, out)

    leftovers = [p for p in out.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_writes_valid_json(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=files)

    raw = (out / MANIFEST_FILENAME).read_text()
    data = json.loads(raw)  # parses cleanly
    assert data["version"] == MANIFEST_VERSION
    assert "jobs" in data


# --- Manifest predicates ---

def test_is_done_only_when_completed_and_downloaded(tmp_path):
    m = Manifest(jobs={
        "a": JobEntry(status=STATUS_COMPLETED, downloaded=True),
        "b": JobEntry(status=STATUS_COMPLETED, downloaded=False),
        "c": JobEntry(status=STATUS_PROCESSING, downloaded=False),
    })
    assert m.is_done("a")
    assert not m.is_done("b")
    assert not m.is_done("c")
    assert not m.is_done("missing")


def test_needs_submit_for_pending_failed_post_and_server_failed(tmp_path):
    m = Manifest(jobs={
        "a": JobEntry(status=STATUS_PENDING),
        "b": JobEntry(status=STATUS_SUBMIT_FAILED, error="429"),
        "c": JobEntry(status=STATUS_PROCESSING, job_id="uuid"),
        "d": JobEntry(status=STATUS_FAILED, job_id="uuid", error="pipeline boom"),
    })
    assert m.needs_submit("a")
    assert m.needs_submit("b")
    assert not m.needs_submit("c")
    assert m.needs_submit("d")  # server-side failure → retry on resume
    assert m.needs_submit("missing")  # any new file needs submit


def test_needs_poll_only_for_in_flight_jobs(tmp_path):
    m = Manifest(jobs={
        "a": JobEntry(status=STATUS_SUBMITTED, job_id="x"),
        "b": JobEntry(status=STATUS_PROCESSING, job_id="y"),
        "c": JobEntry(status=STATUS_COMPLETED, job_id="z"),
        "d": JobEntry(status=STATUS_PENDING),  # no job_id yet
    })
    assert m.needs_poll("a")
    assert m.needs_poll("b")
    assert not m.needs_poll("c")
    assert not m.needs_poll("d")


def test_needs_download_only_after_completion_before_pull(tmp_path):
    m = Manifest(jobs={
        "a": JobEntry(status=STATUS_COMPLETED, downloaded=False),
        "b": JobEntry(status=STATUS_COMPLETED, downloaded=True),
        "c": JobEntry(status=STATUS_FAILED, downloaded=False),
    })
    assert m.needs_download("a")
    assert not m.needs_download("b")
    assert not m.needs_download("c")


# --- Defensive parsing ---

def test_load_ignores_unknown_fields_in_entry(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    payload = {
        "version": MANIFEST_VERSION,
        "jobs": {
            "a.pdf": {
                "job_id": "u",
                "status": "completed",
                "downloaded": True,
                "future_field_we_dont_know": "xyz",  # ignored
            },
        },
    }
    (out / MANIFEST_FILENAME).write_text(json.dumps(payload))

    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=[])
    assert m.jobs["a.pdf"].job_id == "u"
    assert m.jobs["a.pdf"].status == STATUS_COMPLETED


def test_local_data_round_trips_and_legacy_defaults_false(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    payload = {
        "version": MANIFEST_VERSION,
        "jobs": {
            "local.pdf": {"job_id": "u1", "status": "completed", "local_data": True},
            "legacy.pdf": {"job_id": "u2", "status": "completed"},  # pre-0.8.0 manifest
        },
    }
    (out / MANIFEST_FILENAME).write_text(json.dumps(payload))

    m = load_or_init(out, input_root=tmp_path, concurrency=6, files=[])
    assert m.jobs["local.pdf"].local_data is True
    assert m.jobs["legacy.pdf"].local_data is False


# --- load (read-only peek) ---

def test_load_returns_none_when_missing(tmp_path):
    assert load(tmp_path / "nowhere") is None


def test_load_reads_back_saved_manifest(tmp_path):
    files = _files(tmp_path, ["a.pdf"])
    out = tmp_path / "out"
    load_or_init(out, input_root=tmp_path, concurrency=6, files=files)

    m = load(out)

    assert m is not None
    assert set(m.jobs) == {"a.pdf"}


def test_load_returns_none_on_corrupt_or_wrong_version(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / MANIFEST_FILENAME).write_text("{not json")
    assert load(out) is None

    (out / MANIFEST_FILENAME).write_text(json.dumps({"version": 999}))
    assert load(out) is None
