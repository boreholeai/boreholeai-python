"""Persisted batch manifest for SDK fan-out resume.

Tracks per-input-file state in a JSON sidecar at
`output_dir/.boreholeai_manifest.json`. Survives Ctrl-C, network drops,
and process restarts so a re-run can skip already-completed work.

Pure persistence layer — no networking, no async, no concurrency. Used
by `_batch.py` (Phase 4) and `client.py` (Phase 5).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = ".boreholeai_manifest.json"
MANIFEST_VERSION = 1

# SDK-internal lifecycle states.
STATUS_PENDING = "pending"            # added to manifest, not yet POSTed
STATUS_SUBMITTED = "submitted"         # POST returned a job_id, not yet polled
STATUS_SUBMIT_FAILED = "submit_failed"  # POST itself errored; no job_id

# Server-reported states (passthrough from GET /v1/jobs/{id}).
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@dataclass
class JobEntry:
    """Per-input-file state. Filename is the dict key in `Manifest.jobs`."""

    job_id: Optional[str] = None
    num_pages: Optional[int] = None
    pages_done: int = 0                 # updated from poll responses; for live progress
    status: str = STATUS_PENDING
    downloaded: bool = False
    purged: bool = False                # only meaningful if API key has purge_on_download
    error: Optional[str] = None
    submitted_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class Manifest:
    version: int = MANIFEST_VERSION
    created_at: str = ""
    updated_at: str = ""
    concurrency: int = 6
    input_root: str = ""
    output_dir: str = ""
    jobs: dict[str, JobEntry] = field(default_factory=dict)

    def is_done(self, filename: str) -> bool:
        """A file is done iff completed and downloaded."""
        e = self.jobs.get(filename)
        return e is not None and e.status == STATUS_COMPLETED and e.downloaded

    def needs_submit(self, filename: str) -> bool:
        """True if this file should be POSTed (or re-POSTed)."""
        e = self.jobs.get(filename)
        if e is None:
            return True
        return e.status in (STATUS_PENDING, STATUS_SUBMIT_FAILED) or e.job_id is None

    def needs_poll(self, filename: str) -> bool:
        """True if this file has a job_id but isn't terminal yet."""
        e = self.jobs.get(filename)
        if e is None or e.job_id is None:
            return False
        return e.status in (STATUS_SUBMITTED, STATUS_QUEUED, STATUS_PROCESSING)

    def needs_download(self, filename: str) -> bool:
        """True if completed server-side but bytes not yet pulled."""
        e = self.jobs.get(filename)
        if e is None:
            return False
        return e.status == STATUS_COMPLETED and not e.downloaded


def load_or_init(
    output_dir: Path,
    *,
    input_root: Path,
    concurrency: int,
    files: list[Path],
) -> Manifest:
    """Load an existing manifest from `output_dir`, or create a fresh one.

    For an existing manifest:
      - Validates version. Mismatch raises.
      - Warns if `input_root` differs from saved (likely stale manifest).
      - Adds entries for any new files not yet tracked (state=pending).
      - Existing entries are preserved as-is so resume picks up where it left off.

    For a fresh manifest:
      - One pending entry per file in `files`.
      - Written to disk immediately (so Ctrl-C before first POST is recoverable).
    """
    output_dir = Path(output_dir).resolve()
    input_root = Path(input_root).resolve()
    path = output_dir / MANIFEST_FILENAME

    if path.exists():
        manifest = _read(path)
        if manifest.input_root and manifest.input_root != str(input_root):
            logger.warning(
                "manifest input_root differs (saved=%s, current=%s) — "
                "treating saved entries as authoritative",
                manifest.input_root, input_root,
            )
        # Add pending entries for newly-seen files.
        for f in files:
            if f.name not in manifest.jobs:
                manifest.jobs[f.name] = JobEntry()
        manifest.updated_at = _now()
        save(manifest, output_dir)
        return manifest

    output_dir.mkdir(parents=True, exist_ok=True)
    now = _now()
    manifest = Manifest(
        version=MANIFEST_VERSION,
        created_at=now,
        updated_at=now,
        concurrency=concurrency,
        input_root=str(input_root),
        output_dir=str(output_dir),
        jobs={f.name: JobEntry() for f in files},
    )
    save(manifest, output_dir)
    return manifest


def save(manifest: Manifest, output_dir: Path) -> None:
    """Write manifest atomically (tmp file + rename) so Ctrl-C never leaves
    a half-written JSON."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest.updated_at = _now()
    target = output_dir / MANIFEST_FILENAME
    _atomic_write_json(target, _to_dict(manifest))




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to a sibling temp file, then os.replace() — atomic on
    POSIX and Windows."""
    fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _read(path: Path) -> Manifest:
    """Load and validate a manifest from disk."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    version = data.get("version")
    if version != MANIFEST_VERSION:
        raise ValueError(
            f"unsupported manifest version {version!r} "
            f"(expected {MANIFEST_VERSION}) at {path}",
        )

    return _from_dict(data)


def _to_dict(m: Manifest) -> dict:
    return {
        "version": m.version,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
        "concurrency": m.concurrency,
        "input_root": m.input_root,
        "output_dir": m.output_dir,
        "jobs": {name: asdict(entry) for name, entry in m.jobs.items()},
    }


def _from_dict(data: dict) -> Manifest:
    jobs_raw = data.get("jobs", {})
    jobs = {}
    for name, entry_dict in jobs_raw.items():
        # Defensive: ignore unknown fields, allow missing fields to default.
        known = {
            k: v for k, v in entry_dict.items()
            if k in JobEntry.__dataclass_fields__
        }
        jobs[name] = JobEntry(**known)
    return Manifest(
        version=data.get("version", MANIFEST_VERSION),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        concurrency=data.get("concurrency", 6),
        input_root=data.get("input_root", ""),
        output_dir=data.get("output_dir", ""),
        jobs=jobs,
    )
