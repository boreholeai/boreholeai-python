"""BoreholeAI Python SDK client — fan-out + client-side merge."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from boreholeai._api import APIClientAsync, DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from boreholeai._batch import BatchResult, resolve_effective_concurrency, run_batch
from boreholeai._files import collect_files
from boreholeai._manifest import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_QUEUED,
    STATUS_SUBMITTED,
    STATUS_SUBMIT_FAILED,
    Manifest,
)
from boreholeai._merge import merge_results
from boreholeai._progress import compute_progress
from boreholeai._version import __version__, __version_date__
from boreholeai._types import FileResult, JobResult

_DEFAULT_OUTPUT_DIR = "./results"
_DEFAULT_CONCURRENCY = 10

_BAR_WIDTH = 20
_BAR_FILL = "█"
_BAR_EMPTY = "░"

# Cap renderer redraws to ~4 fps so a chatty manifest (subgraph events
# firing several times per second × N files) doesn't peg stderr or
# stutter over slow terminals (SSH, recorders).
_RENDER_MIN_INTERVAL = 0.25

# Strip C0/C1 control characters from filenames before printing them, so
# a malicious filename can't inject ANSI escape sequences into the user's
# terminal (clear screen, hide cursor, OSC commands, etc.).
_CONTROL_CHARS = "".join(chr(c) for c in range(0x00, 0x20)) + "\x7f"
_CONTROL_TRANS = str.maketrans({c: "?" for c in _CONTROL_CHARS})


def _safe_filename(name: str) -> str:
    """Replace control characters in a filename for safe terminal display."""
    return name.translate(_CONTROL_TRANS)


class BoreholeAI:
    """Client for the BoreholeAI API.

    Usage::

        from boreholeai import BoreholeAI

        client = BoreholeAI(api_key="bhai_xxx")

        # Single file
        result = client.process_documents("borehole.pdf")

        # Folder — fans out to N concurrent server jobs, merges results
        result = client.process_documents("./logs/", output_dir="./results")

    For a folder of N files, this processes them in parallel, downloads
    all outputs, then produces one merged ground_profile / test_data / AGS
    file.

    Resumes automatically if interrupted: re-running with the same
    `output_dir` skips already-completed work.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        _transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._transport = _transport
        date_suffix = f" ({__version_date__})" if __version_date__ != "dev" else ""
        _log(f"boreholeai v{__version__}{date_suffix}")
        _log("To check for updates: pip install --upgrade boreholeai")

    def close(self) -> None:
        """No-op — async clients are scoped per call. Kept for API symmetry."""
        pass

    def __enter__(self) -> BoreholeAI:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def process_documents(
        self,
        input_path: str | Path,
        *,
        output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
    ) -> JobResult:
        """Submit, process, and merge a single file or a folder of files.

        Args:
            input_path: Single file or directory.
                Supported: PDF, PNG, JPG, JPEG, TIF, TIFF, WebP.
            output_dir: Where merged results land. Created if missing.
                If `.boreholeai_manifest.json` is present from a prior run,
                that work is resumed; already-completed files are skipped.

        Returns:
            JobResult — `status` is "completed", "partial", or "failed";
            `failures` lists per-file errors when partial.

        Raises:
            FileNotFoundError, ValueError on bad input. Otherwise per-file
            failures are reported via `result.failures`, not raised.
        """
        files = collect_files(input_path)
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)

        start = time.monotonic()

        renderer = _PerFileProgress(start) if sys.stderr.isatty() else None
        try:
            batch = asyncio.run(self._run(files, out, _DEFAULT_CONCURRENCY, renderer))
        finally:
            if renderer is not None:
                renderer.finalise()

        elapsed = time.monotonic() - start
        return self._finalise(batch, files, out, elapsed)

    async def _run(
        self, files: list[Path], output_dir: Path, concurrency: int,
        renderer: Optional["_PerFileProgress"] = None,
    ) -> BatchResult:
        async with APIClientAsync(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            _transport=self._transport,
        ) as client:
            # Pace ourselves to the server's per-user cap so we never
            # send POSTs that would just be 429-rejected.
            effective, _ = await resolve_effective_concurrency(client, concurrency)
            _log(f"🟢 Starting {len(files)} file(s) (concurrency={effective})")
            return await run_batch(
                client, files, output_dir, concurrency=effective,
                on_progress=renderer.update if renderer else None,
            )

    def _finalise(
        self, batch: BatchResult, files: list[Path],
        output_dir: Path, elapsed: float,
    ) -> JobResult:
        """Merge successful jobs, log summary, build JobResult."""
        success_dirs = [
            batch.workdir / batch.job_ids[name]
            for name in batch.successes
            if name in batch.job_ids and batch.workdir is not None
        ]
        # Map each per-job dir back to its original input filename so any
        # merge warning shows the user-facing name, not the job UUID.
        dir_labels = {
            batch.workdir / batch.job_ids[name]: name
            for name in batch.successes
            if name in batch.job_ids and batch.workdir is not None
        }

        merged_files: list[FileResult] = []
        if success_dirs:
            mr = merge_results(success_dirs, output_dir, dir_labels=dir_labels)
            merged_files = [FileResult(filename=p.name, path=p) for p in mr.files]
            if mr.warnings:
                _log(f"{len(mr.warnings)} merge warning(s) — see merge_warnings.txt")

        n_total = len(files)
        n_ok = len(batch.successes)
        n_fail = len(batch.failures)
        if n_ok == n_total:
            status = "completed"
        elif n_ok == 0:
            status = "failed"
        else:
            status = "partial"

        _log(
            f"Done {n_ok}/{n_total} file(s) in {_fmt_time(elapsed)}"
            + (f" — {n_fail} failure(s)" if n_fail else ""),
        )
        if batch.failures:
            _log("Failures:")
            for name, err in batch.failures.items():
                _log(f"  {name}: {err}")
        if merged_files:
            _log(f"🟢 Saved {len(merged_files)} file(s) to {output_dir}")
            for f in merged_files:
                _log(f"      {f.filename}")

        purged_count = sum(
            1 for e in (batch.manifest.jobs.values() if batch.manifest else [])
            if e.purged
        )
        if purged_count:
            _log(f"🔴 Server files deleted for {purged_count} job(s) (enterprise purge-on-download)")

        # Clean up the per-job workdir AND the manifest only when everything
        # succeeded. On partial/failed runs we keep both so the user can
        # inspect or resume the run.
        if status == "completed":
            if batch.workdir is not None and batch.workdir.exists():
                shutil.rmtree(batch.workdir, ignore_errors=True)
            manifest_path = output_dir / ".boreholeai_manifest.json"
            if manifest_path.exists():
                manifest_path.unlink()

        primary_job_id = next(iter(batch.job_ids.values()), "")
        total_pages = sum(
            (e.num_pages or 0)
            for e in (batch.manifest.jobs.values() if batch.manifest else [])
        )

        return JobResult(
            job_id=primary_job_id,
            status=status,
            num_pages=total_pages,
            credits_used=total_pages,
            files=merged_files,
            job_ids=list(batch.job_ids.values()),
            successes=list(batch.successes),
            failures=dict(batch.failures),
        )




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _log(message: str) -> None:
    """Print a status line to stderr."""
    print(f"  {message}", file=sys.stderr, flush=True)


def _fmt_time(seconds: float) -> str:
    """Human-readable elapsed time."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s"


def _fmt_progress(seconds: float) -> str:
    """Compact mm:ss for inline progress display."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class _PerFileProgress:
    """Live multi-line progress display, one line per input file.

    Re-renders the block in place using ANSI cursor movement after each
    state change. Non-TTY callers should not construct this — `client.py`
    only instantiates it when `sys.stderr.isatty()`.
    """

    def __init__(self, started_at: float):
        self._started_at = started_at
        self._lines_drawn = 0
        # Capture filename order from first update so lines stay stable
        self._order: list[str] = []
        self._file_started: dict[str, float] = {}
        # Frozen end-time for files that have reached a terminal state.
        # Without this, the displayed elapsed clock keeps ticking on
        # already-done files because `now - started` is recomputed each
        # frame. We capture `now` at the first terminal observation and
        # reuse it forever after.
        self._file_ended: dict[str, float] = {}
        # Per-file: number of completed_subgraphs at last observation, and
        # the timestamp at which that number first appeared. Used by the
        # progress bar to compute "elapsed since last subgraph change" so
        # `_progress.compute_progress` can interpolate inside the current
        # subgraph. Mirrors `sgStartTimes` in frontend's use-job-progress.ts.
        self._sg_state: dict[str, tuple[int, float]] = {}
        # Frame-rate cap state — see _RENDER_MIN_INTERVAL.
        self._last_render_at: float = 0.0
        self._final_frame_drawn: bool = False

    def update(self, manifest: Manifest) -> None:
        """Re-draw all per-file lines based on current manifest state."""
        if not self._order:
            self._order = list(manifest.jobs.keys())

        now = time.monotonic()
        for name in self._order:
            entry = manifest.jobs.get(name)
            if entry is None:
                continue
            # Track when each file's clock started (first non-pending state).
            if name not in self._file_started and entry.status != STATUS_PENDING:
                self._file_started[name] = now
            # Freeze each file's clock at its first terminal observation.
            if name not in self._file_ended and _file_is_terminal(entry):
                self._file_ended[name] = now

        # Frame-rate cap: ~4 fps unless every file has reached a terminal
        # state, in which case we always draw the FINAL frame so the user
        # sees the last update before the renderer goes silent. "Terminal"
        # for a COMPLETED file requires `downloaded=True` — otherwise we'd
        # latch the final frame mid-download on the last file and miss the
        # `downloading… → ✓ done` transition.
        all_terminal = all(
            (manifest.jobs.get(name) is None) or
            _file_is_terminal(manifest.jobs[name])
            for name in self._order
        )
        if not all_terminal and now - self._last_render_at < _RENDER_MIN_INTERVAL:
            return
        if all_terminal and self._final_frame_drawn:
            return
        self._last_render_at = now
        if all_terminal:
            self._final_frame_drawn = True

        # Move cursor up to the top of the previously drawn block, clear,
        # then re-draw every line. Each redraw is one frame.
        if self._lines_drawn > 0:
            sys.stderr.write(f"\033[{self._lines_drawn}A")

        max_name_len = min(40, max((len(n) for n in self._order), default=10))
        rendered = 0
        for name in self._order:
            entry = manifest.jobs.get(name)
            if entry is None:
                continue
            end_time = self._file_ended.get(name, now)
            elapsed = end_time - self._file_started.get(name, now)
            elapsed_in_sg = self._elapsed_in_current_sg(name, entry, now)
            line = self._format_line(name, entry, max_name_len, elapsed, elapsed_in_sg)
            sys.stderr.write(f"\r\033[K        {line}\n")
            rendered += 1

        sys.stderr.flush()
        self._lines_drawn = rendered

    def _elapsed_in_current_sg(self, name: str, entry, now: float) -> float:
        """Seconds since the last time `entry.completed_subgraphs` grew.

        Resets the timer whenever a new subgraph completes, so the bar's
        within-subgraph interpolation in `_progress.compute_progress`
        starts fresh on each transition.
        """
        count = len(entry.completed_subgraphs or [])
        last = self._sg_state.get(name)
        if last is None or last[0] != count:
            self._sg_state[name] = (count, now)
            return 0.0
        return now - last[1]

    def finalise(self) -> None:
        """Leave the last frame in place; subsequent _log calls print below it."""
        # Nothing to do — the trailing newline on the last line means the
        # cursor is already on a fresh line, and lines we drew remain visible.
        self._lines_drawn = 0

    def _format_line(
        self, name: str, entry, name_width: int,
        elapsed: float, elapsed_in_sg: float,
    ) -> str:
        # Sanitise FIRST — defends against ANSI / OSC injection via
        # adversarial filenames (e.g. clear-screen, cursor-move, OSC
        # commands written by some terminals).
        safe = _safe_filename(name)
        truncated = safe if len(safe) <= name_width else safe[: name_width - 1] + "…"
        padded = truncated.ljust(name_width)
        time_str = _fmt_progress(elapsed) if entry.status != STATUS_PENDING else "—"
        body = self._status_body(entry, elapsed_in_sg)
        return f"{padded}  {body}  [{time_str}]"

    def _status_body(self, entry, elapsed_in_sg: float) -> str:
        s = entry.status
        if s == STATUS_PENDING:
            # With cap-aware pacing, files in the PENDING state are held by
            # the SDK's submit semaphore — waiting for a server-side slot to
            # free up before being POSTed. Make that explicit so users don't
            # think the SDK is stuck.
            return "queued (waiting for slot)"
        if s == STATUS_SUBMITTED:
            return "submitted, waiting for worker"
        if s == STATUS_QUEUED:
            return "queued, waiting for worker"
        if s == STATUS_PROCESSING:
            return _bar_processing(entry, elapsed_in_sg)
        if s == STATUS_COMPLETED:
            if entry.downloaded:
                return "✓ done"
            return _bar_full() + "  downloading…"
        if s == STATUS_FAILED:
            return f"✗ failed ({entry.error or 'unknown error'})"
        if s == STATUS_SUBMIT_FAILED:
            return f"✗ submit failed ({entry.error or 'unknown error'})"
        return s


def _bar_processing(entry, elapsed_in_sg: float) -> str:
    """Render the bar using the weighted-subgraph algorithm shared with
    the frontend (see `_progress.compute_progress`).

    Bar fills smoothly across subgraphs; never reaches 100% until status
    flips to "completed" (`_bar_full` handles that).
    """
    pages_total = entry.pages_total or entry.num_pages or 1
    pct = compute_progress(
        page=entry.current_page or 1,
        pages_total=pages_total,
        completed_subgraphs=entry.completed_subgraphs or [],
        elapsed_in_current_sg=elapsed_in_sg,
    )
    pct_clamped = max(0.0, min(99.0, pct))
    filled = int((pct_clamped / 100.0) * _BAR_WIDTH)
    bar = _BAR_FILL * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)
    return f"[{bar}] {int(pct_clamped)}%"


def _bar_full() -> str:
    """Bar at 100% — only shown when status is `completed`."""
    return f"[{_BAR_FILL * _BAR_WIDTH}] 100%"


def _file_is_terminal(entry) -> bool:
    """A file is fully terminal once it can't change visually anymore.

    Failed files are terminal as soon as status flips. Completed files
    only count as terminal once `downloaded=True` — without this, the
    renderer would latch its final frame the moment the last file's
    poll ends, swallowing the `downloading… → ✓ done` transition.
    """
    if entry.status in (STATUS_FAILED, STATUS_SUBMIT_FAILED):
        return True
    return entry.status == STATUS_COMPLETED and entry.downloaded
