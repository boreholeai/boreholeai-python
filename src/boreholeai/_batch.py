"""Fan-out + poll + download orchestration for SDK batch processing.

Reads/writes the manifest, calls APIClientAsync for HTTP. Three phases
run sequentially, but within each phase all files are processed
concurrently:

   SUBMIT    bounded by Semaphore(concurrency)
   POLL      unbounded — polling is idle wait, not real load
   DOWNLOAD  bounded by Semaphore(concurrency)

State is checkpointed to the manifest after every transition, so a
Ctrl-C or network drop can be resumed by re-invoking with the same
output_dir.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from boreholeai import _manifest
from boreholeai._api import APIClientAsync
from boreholeai._manifest import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_SUBMITTED,
    STATUS_SUBMIT_FAILED,
    Manifest,
)
from boreholeai.exceptions import (
    AuthenticationError,
    BoreholeAIError,
    InsufficientCreditsError,
    RateLimitError,
    ServerError,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONCURRENCY = 6

_POLL_INITIAL_INTERVAL = 2.0
_POLL_MAX_INTERVAL = 10.0
_POLL_BACKOFF_FACTOR = 1.5

_SUBMIT_MAX_RETRIES = 5
_SUBMIT_RETRY_BASE = 2.0
_SUBMIT_RETRY_MAX = 60.0

_WORKDIR_NAME = ".boreholeai_workdir"


@dataclass
class BatchResult:
    """Outcome of `run_batch`. Filenames are input file basenames."""

    successes: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    job_ids: dict[str, str] = field(default_factory=dict)
    workdir: Optional[Path] = None
    manifest: Optional[Manifest] = None


async def run_batch(
    client: APIClientAsync,
    files: list[Path],
    output_dir: Path,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    on_progress: Optional[Callable[[Manifest], None]] = None,
) -> BatchResult:
    """Submit, poll, and download N files concurrently.

    Persists state to `output_dir/.boreholeai_manifest.json` so an
    interrupted run can resume by re-invoking with the same arguments.
    Does NOT call merge — caller decides when to merge across the
    workdir.

    `on_progress` (optional): called with the current Manifest after every
    state change. Used by `client.py` to render a per-file progress display.
    Must not raise; exceptions are swallowed to avoid breaking the batch.
    """
    files = list(files)
    if not files:
        raise ValueError("run_batch requires at least one file")

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    workdir = output_dir / _WORKDIR_NAME
    workdir.mkdir(parents=True, exist_ok=True)

    input_root = files[0].parent.resolve()
    manifest = _manifest.load_or_init(
        output_dir, input_root=input_root, concurrency=concurrency, files=files,
    )
    save_lock = asyncio.Lock()
    sem = asyncio.Semaphore(concurrency)

    files_by_name = {f.name: f for f in files}

    # Initial render so users see all files in their starting state
    _emit_progress(on_progress, manifest)

    # PHASE 1 — SUBMIT
    await asyncio.gather(*(
        _submit_one(name, files_by_name[name], client, sem, manifest, save_lock, output_dir, on_progress)
        for name in files_by_name
        if manifest.needs_submit(name)
    ))

    # PHASE 2 — POLL
    await asyncio.gather(*(
        _poll_one(name, client, manifest, save_lock, output_dir, on_progress)
        for name in files_by_name
        if manifest.needs_poll(name)
    ))

    # PHASE 3 — DOWNLOAD (and optional enterprise purge)
    await asyncio.gather(*(
        _download_one(name, client, sem, manifest, save_lock, output_dir, workdir, on_progress)
        for name in files_by_name
        if manifest.needs_download(name)
    ))

    return _build_result(manifest, files_by_name, workdir)




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

async def _submit_one(
    name: str, file_path: Path, client: APIClientAsync,
    sem: asyncio.Semaphore, manifest: Manifest,
    save_lock: asyncio.Lock, output_dir: Path,
    on_progress: Optional[Callable[[Manifest], None]] = None,
) -> None:
    """Submit one file with exponential-backoff retry on transient errors."""
    delay = _SUBMIT_RETRY_BASE
    for attempt in range(1, _SUBMIT_MAX_RETRIES + 1):
        try:
            async with sem:
                data = await client.create_job([file_path])
            async with save_lock:
                e = manifest.jobs[name]
                e.job_id = data["job_id"]
                e.num_pages = data.get("num_pages")
                e.status = STATUS_SUBMITTED
                e.submitted_at = _manifest._now()
                e.error = None
                _manifest.save(manifest, output_dir)
            _emit_progress(on_progress, manifest)
            logger.info("submitted %s → job %s", name, data["job_id"][:8])
            return
        except (RateLimitError, ServerError) as exc:
            if attempt >= _SUBMIT_MAX_RETRIES:
                await _mark_submit_failed(name, str(exc), manifest, save_lock, output_dir, on_progress)
                return
            logger.warning(
                "transient submit error on %s (%s) — retry %d/%d in %.1fs",
                name, exc, attempt, _SUBMIT_MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _SUBMIT_RETRY_MAX)
        except (AuthenticationError, InsufficientCreditsError) as exc:
            # Fatal — no point retrying this file or any other (caller will see)
            await _mark_submit_failed(name, str(exc), manifest, save_lock, output_dir, on_progress)
            return
        except BoreholeAIError as exc:
            await _mark_submit_failed(name, str(exc), manifest, save_lock, output_dir, on_progress)
            return


async def _mark_submit_failed(
    name: str, error: str, manifest: Manifest,
    save_lock: asyncio.Lock, output_dir: Path,
    on_progress: Optional[Callable[[Manifest], None]] = None,
) -> None:
    async with save_lock:
        e = manifest.jobs[name]
        e.status = STATUS_SUBMIT_FAILED
        e.error = error
        _manifest.save(manifest, output_dir)
    _emit_progress(on_progress, manifest)
    logger.error("submit failed for %s: %s", name, error)


async def _poll_one(
    name: str, client: APIClientAsync, manifest: Manifest,
    save_lock: asyncio.Lock, output_dir: Path,
    on_progress: Optional[Callable[[Manifest], None]] = None,
) -> None:
    """Poll one job until it reaches a terminal state. Tolerates transient
    polling errors with backoff — only terminal job statuses end the loop."""
    job_id = manifest.jobs[name].job_id
    if job_id is None:
        return

    interval = _POLL_INITIAL_INTERVAL
    while True:
        try:
            data = await client.get_job(job_id)
        except BoreholeAIError as exc:
            logger.warning("poll error on %s (%s) — retry in %.1fs", name, exc, interval)
            await asyncio.sleep(interval)
            interval = min(interval * _POLL_BACKOFF_FACTOR, _POLL_MAX_INTERVAL)
            continue

        status = data.get("status", "")
        progress = data.get("progress") or {}
        async with save_lock:
            e = manifest.jobs[name]
            e.status = status
            # Live page-progress for the renderer
            pages_done = progress.get("pages_done")
            pages_total = progress.get("pages_total")
            if pages_done is not None:
                e.pages_done = int(pages_done)
            if pages_total and e.num_pages is None:
                e.num_pages = int(pages_total)
            if status == STATUS_FAILED:
                e.error = data.get("error_message", "unknown error")
                e.completed_at = _manifest._now()
            elif status == STATUS_COMPLETED:
                e.completed_at = _manifest._now()
                if e.num_pages is None:
                    e.num_pages = data.get("num_pages")
                if e.num_pages:
                    e.pages_done = e.num_pages
            _manifest.save(manifest, output_dir)
        _emit_progress(on_progress, manifest)

        if status in (STATUS_COMPLETED, STATUS_FAILED):
            return

        await asyncio.sleep(interval)
        interval = min(interval * _POLL_BACKOFF_FACTOR, _POLL_MAX_INTERVAL)


async def _download_one(
    name: str, client: APIClientAsync, sem: asyncio.Semaphore,
    manifest: Manifest, save_lock: asyncio.Lock,
    output_dir: Path, workdir: Path,
    on_progress: Optional[Callable[[Manifest], None]] = None,
) -> None:
    """Fetch signed URLs, download every file into workdir/{job_id}/, and
    optionally call DELETE for the enterprise purge-on-download flow."""
    job_id = manifest.jobs[name].job_id
    if job_id is None:
        return

    job_workdir = workdir / job_id
    job_workdir.mkdir(parents=True, exist_ok=True)

    async with sem:
        try:
            results = await client.get_results(job_id)
        except BoreholeAIError as exc:
            async with save_lock:
                manifest.jobs[name].error = f"results fetch failed: {exc}"
                _manifest.save(manifest, output_dir)
            _emit_progress(on_progress, manifest)
            logger.error("results fetch failed for %s: %s", name, exc)
            return

        files_to_dl = results.get("files", [])
        for f in files_to_dl:
            try:
                content = await client.download_file(f["url"])
            except Exception as exc:
                async with save_lock:
                    manifest.jobs[name].error = f"download failed: {exc}"
                    _manifest.save(manifest, output_dir)
                _emit_progress(on_progress, manifest)
                logger.error("download failed for %s: %s", name, exc)
                return
            (job_workdir / f["filename"]).write_bytes(content)

        async with save_lock:
            manifest.jobs[name].downloaded = True
            _manifest.save(manifest, output_dir)
        _emit_progress(on_progress, manifest)
        logger.info("downloaded %d file(s) for %s", len(files_to_dl), name)

        if results.get("purge_on_download"):
            try:
                purge = await client.delete_job(job_id)
                async with save_lock:
                    manifest.jobs[name].purged = True
                    _manifest.save(manifest, output_dir)
                _emit_progress(on_progress, manifest)
                logger.info(
                    "purged %d server file(s) for %s",
                    purge.get("files_deleted", 0), name,
                )
            except BoreholeAIError as exc:
                logger.warning("purge failed for %s: %s", name, exc)


def _emit_progress(
    cb: Optional[Callable[[Manifest], None]], manifest: Manifest,
) -> None:
    """Fire the progress callback if set, swallowing any exceptions so the
    batch never fails because the renderer crashed."""
    if cb is None:
        return
    try:
        cb(manifest)
    except Exception:
        logger.debug("progress callback raised", exc_info=True)


def _build_result(
    manifest: Manifest, files_by_name: dict[str, Path], workdir: Path,
) -> BatchResult:
    res = BatchResult(workdir=workdir, manifest=manifest)
    for name in files_by_name:
        e = manifest.jobs.get(name)
        if e is None:
            continue
        if e.job_id:
            res.job_ids[name] = e.job_id
        if e.status == STATUS_COMPLETED and e.downloaded:
            res.successes.append(name)
        elif e.status in (STATUS_FAILED, STATUS_SUBMIT_FAILED):
            res.failures[name] = e.error or "unknown"
        else:
            res.failures[name] = f"incomplete: status={e.status}"
    return res
