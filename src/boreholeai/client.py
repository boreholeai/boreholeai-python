"""BoreholeAI Python SDK client — fan-out + client-side merge."""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from boreholeai._api import APIClientAsync, DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from boreholeai._batch import (
    _DEFAULT_CONCURRENCY,
    _job_workdir,
    BatchResult,
    resolve_effective_concurrency,
    run_batch,
)
from boreholeai import _manifest
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

_BAR_WIDTH = 20
_BAR_FILL = "█"
_BAR_EMPTY = "░"

# Cap renderer redraws to ~4 fps so a chatty manifest (subgraph events
# firing several times per second × N files) doesn't peg stderr or
# stutter over slow terminals (SSH, recorders).
_RENDER_MIN_INTERVAL = 0.25

# Leave a little vertical breathing room below the live frame.  More
# importantly, this guarantees that every cursor-up command refers only to
# rows that are still on the visible screen; terminals cannot move the cursor
# into scrollback history.
_LIVE_VERTICAL_MARGIN = 2
_LIVE_INDENT = "        "

# Strip C0/C1 control characters from filenames before printing them, so
# a malicious filename can't inject ANSI escape sequences into the user's
# terminal (clear screen, hide cursor, OSC commands, etc.).
_CONTROL_CHARS = "".join(chr(c) for c in range(0x00, 0x20)) + "\x7f"
_CONTROL_TRANS = str.maketrans({c: "?" for c in _CONTROL_CHARS})


def _safe_filename(name: str) -> str:
    """Replace control characters in a filename for safe terminal display."""
    return name.translate(_CONTROL_TRANS)


def _safe_terminal_text(text: str) -> str:
    """Replace control characters in arbitrary server-provided display text."""
    return text.translate(_CONTROL_TRANS)


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
        finalise_and_cleanup: Optional[bool] = None,
        retry_failed_pages: Optional[bool] = None,
    ) -> JobResult:
        """Submit, process, and merge a single file or a folder of files.

        Args:
            input_path: Single file or directory.
                Supported: PDF, PNG, JPG, JPEG, TIF, TIFF, WebP.
            output_dir: Where merged results land. Created if missing.
                If `.boreholeai_manifest.json` is present from a prior run,
                that work is resumed; already-completed files are skipped.
            finalise_and_cleanup: What to do with the resume state (manifest
                + per-job workdir) after a fully-successful run.
                True — delete it, leaving only the merged outputs.
                False — keep it, so files added to `input_path` later are
                processed incrementally on re-run (completed files are
                skipped, the merge re-covers everything).
                None (default) — keep it, and when running at an interactive
                terminal ask "clean up? [y/N]" once everything has completed.
                Any non-answer (Enter, EOF, Ctrl-C, closed window) keeps the
                state; a later re-run asks again. Cleanup never happens on a
                partial/failed run regardless of this setting.
            retry_failed_pages: What to do when a previous run left completed
                files with failed pages (the 🟡 warnings in the run summary).
                A re-processed file is submitted as a new job and charged
                again, like a new file.
                True — re-process them all, without asking.
                False — leave them completed; only new/failed files run.
                None (default) — when running at an interactive terminal,
                ask once before the run starts. Any non-answer (Enter, EOF,
                Ctrl-C) leaves them completed; under cron/CI/pipes nothing
                is asked and nothing is re-charged.

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

        _maybe_flag_failed_page_retries(
            out, {f.name for f in files}, retry_failed_pages,
        )

        start = time.monotonic()

        renderer = _PerFileProgress(start) if sys.stderr.isatty() else None
        try:
            batch = asyncio.run(self._run(files, out, _DEFAULT_CONCURRENCY, renderer))
        finally:
            if renderer is not None:
                renderer.finalise()

        elapsed = time.monotonic() - start
        return self._finalise(batch, files, out, elapsed, finalise_and_cleanup)

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
            _log(f"🟢 Starting {len(files)} file(s) (workers={effective})")
            return await run_batch(
                client, files, output_dir, concurrency=effective,
                on_progress=renderer.update if renderer else None,
            )

    def _finalise(
        self, batch: BatchResult, files: list[Path],
        output_dir: Path, elapsed: float,
        finalise_and_cleanup: Optional[bool] = None,
    ) -> JobResult:
        """Merge successful jobs, log summary, build JobResult."""
        success_dirs = [
            _job_workdir(batch.workdir, name, batch.job_ids[name])
            for name in batch.successes
            if name in batch.job_ids and batch.workdir is not None
        ]
        # Map each per-job dir back to its original input filename so any
        # merge warning shows the user-facing name, not the job UUID.
        dir_labels = {
            _job_workdir(batch.workdir, name, batch.job_ids[name]): name
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

        # Successful jobs can still contain pages that failed extraction —
        # the job-level status can't see those, but each job's Processing
        # Info sheet records them and the download step lifts them into
        # the manifest.
        page_warnings = {
            name: entry.warning
            for name, entry in (batch.manifest.jobs.items() if batch.manifest else [])
            if entry.warning and name in batch.successes
        }
        if page_warnings:
            _log(f"🟡 {len(page_warnings)} completed file(s) have failed pages:")
            for name, w in page_warnings.items():
                _log(f"  {name}: {w}")
            _log(
                "  Re-run the same call to be offered a retry of these "
                "files (or pass retry_failed_pages=True)"
            )
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
        # inspect or resume the run. With finalise_and_cleanup=False they are
        # kept even on success, so a later run over a grown input folder
        # processes only the new files and re-merges the full set. With the
        # default None, an interactive user is asked; keeping is the safe
        # default everywhere else. The merged outputs are already written by
        # this point, so no answer (or a killed process) loses nothing.
        if status == "completed":
            cleanup = finalise_and_cleanup
            if cleanup is None:
                cleanup = _prompt_cleanup(n_total)
            if cleanup:
                if batch.workdir is not None and batch.workdir.exists():
                    shutil.rmtree(batch.workdir, ignore_errors=True)
                manifest_path = output_dir / ".boreholeai_manifest.json"
                if manifest_path.exists():
                    manifest_path.unlink()
            else:
                _log(
                    "🟡 Kept resume state — re-run to add files incrementally, "
                    "or pass finalise_and_cleanup=True to clean up"
                )

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
            warnings=page_warnings,
        )




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _log(message: str) -> None:
    """Print a status line to stderr."""
    print(f"  {message}", file=sys.stderr, flush=True)


def _prompt_cleanup(n_files: int) -> bool:
    """Ask an interactive user whether to delete the resume state.

    Only asks when both stdin and stderr are terminals — under cron/CI/
    pipes this returns False without blocking. Any non-answer (empty,
    EOF, Ctrl-C, unrecognised input) also means keep: keeping state is
    always recoverable by a later run, deleting is not.
    """
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    print(
        f"  All {n_files} file(s) completed — clean up the manifest and "
        f"working file cache? [y/N] ",
        end="", file=sys.stderr, flush=True,
    )
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _maybe_flag_failed_page_retries(
    output_dir: Path, input_names: set[str], decision: Optional[bool],
) -> None:
    """Flag completed-with-failed-pages entries for re-processing.

    Runs before the batch starts so the resume logic sees the flags and
    resubmits those files through the normal `reprocess` path. `decision`
    mirrors `finalise_and_cleanup`: True flags without asking, False
    touches nothing, None asks at an interactive terminal and touches
    nothing everywhere else — an unattended run never re-charges files
    without an explicit True. Only files present in the current input
    (`input_names`) are counted or flagged; anything else in the manifest
    couldn't be resubmitted this run anyway.
    """
    if decision is False:
        return
    manifest = _manifest.load(output_dir)
    if manifest is None:
        return
    candidates = {
        name: entry
        for name, entry in manifest.jobs.items()
        if name in input_names
        and entry.status == STATUS_COMPLETED
        and entry.warning
        and not entry.reprocess
    }
    if not candidates:
        return
    if decision is None and not _prompt_retry_failed_pages(candidates):
        return
    for entry in candidates.values():
        entry.reprocess = True
    _manifest.save(manifest, output_dir)
    _log(
        f"🟡 {len(candidates)} file(s) with failed pages queued for "
        f"reprocessing"
    )


def _prompt_retry_failed_pages(
    candidates: dict[str, _manifest.JobEntry],
) -> bool:
    """Ask an interactive user whether to re-process completed files whose
    pages failed extraction.

    Mirrors `_prompt_cleanup`: only asks when both stdin and stderr are
    terminals — under cron/CI/pipes this returns False without blocking.
    Any non-answer (empty, EOF, Ctrl-C, unrecognised input) also means no:
    saying yes re-submits every listed file as a fresh job, which deducts
    credits again, so the conservative default is to leave them alone.
    """
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    est_credits = sum((e.num_pages or 1) for e in candidates.values())
    print(
        f"  🟡 {len(candidates)} previously completed file(s) have failed "
        f"pages — re-process them? Uses ~{est_credits} credit(s) [y/N] ",
        end="", file=sys.stderr, flush=True,
    )
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


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
    """Terminal-aware per-file progress display.

    Small batches keep the familiar one-row-per-file live display.  When the
    batch is taller than the terminal, the live frame contains a summary and
    only non-terminal files, capped to the visible height.  Once everything
    finishes, the live frame is cleared and every file is printed once as an
    append-only final report.  The final report may scroll, but is never used
    as the target of an ANSI cursor-up redraw.

    Non-TTY callers should not construct this — `client.py` only instantiates
    it when `sys.stderr.isatty()`.
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
        """Update the bounded live frame or print the final report once."""
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

        terminal = shutil.get_terminal_size(fallback=(80, 24))
        max_live_lines = max(1, terminal.lines - _LIVE_VERTICAL_MARGIN)

        if all_terminal:
            self._clear_live_frame(max_live_lines)
            self._write_final_report(manifest, now)
            return

        lines = self._live_lines(manifest, now, max_live_lines)
        self._redraw_live_frame(lines, terminal.columns, max_live_lines)

    def _live_lines(
        self, manifest: Manifest, now: float, max_live_lines: int,
    ) -> list[str]:
        """Build a live frame that never exceeds the terminal height."""
        existing = [name for name in self._order if name in manifest.jobs]
        if len(existing) <= max_live_lines:
            names = existing
            summary = None
        else:
            active = [
                name for name in existing
                if manifest.jobs[name].status != STATUS_PENDING
                and not _file_is_terminal(manifest.jobs[name])
            ]
            active_capacity = max(0, max_live_lines - 1)
            names = active[:active_capacity]
            summary = self._summary_line(manifest, len(names), len(active))

        max_name_len = min(40, max((len(n) for n in existing), default=10))
        index_width = len(str(len(self._order)))
        positions = {name: i for i, name in enumerate(self._order, start=1)}
        lines = [summary] if summary is not None else []
        for name in names:
            entry = manifest.jobs[name]
            elapsed = self._elapsed(name, entry, now)
            elapsed_in_sg = self._elapsed_in_current_sg(name, entry, now)
            lines.append(self._format_line(
                positions[name], name, entry, index_width, max_name_len,
                elapsed, elapsed_in_sg,
            ))
        return lines

    def _summary_line(
        self, manifest: Manifest, shown_active: int, active_count: int,
    ) -> str:
        entries = [manifest.jobs[n] for n in self._order if n in manifest.jobs]
        succeeded = sum(
            e.status == STATUS_COMPLETED and e.downloaded and not e.reprocess
            for e in entries
        )
        failed = sum(
            e.status in (STATUS_FAILED, STATUS_SUBMIT_FAILED) for e in entries
        )
        waiting = sum(e.status == STATUS_PENDING for e in entries)
        summary = (
            f"Files: {succeeded}/{len(entries)} succeeded · {failed} failed · "
            f"{active_count} active · {waiting} waiting"
        )
        if shown_active < active_count:
            summary += f" · showing {shown_active}/{active_count} active"
        return summary

    def _redraw_live_frame(
        self, lines: list[str], terminal_width: int, max_live_lines: int,
    ) -> None:
        """Replace the prior bounded frame without addressing scrollback."""
        # A terminal resize can make the old frame taller than the new screen.
        # It is no longer safe to address those rows, so abandon that frame and
        # append the new bounded one.  This can leave one stale frame after a
        # resize, but prevents the repeating/scrollback corruption entirely.
        if self._lines_drawn > max_live_lines:
            self._lines_drawn = 0

        previous = self._lines_drawn
        if previous:
            sys.stderr.write(f"\033[{previous}A")

        content_width = max(1, terminal_width - len(_LIVE_INDENT))
        for line in lines:
            fitted = _fit_terminal_width(line, content_width)
            sys.stderr.write(f"\r\033[K{_LIVE_INDENT}{fitted}\n")

        # If the new frame is shorter, erase the old tail and restore the
        # cursor to immediately below the new frame.
        surplus = previous - len(lines)
        for _ in range(max(0, surplus)):
            sys.stderr.write("\r\033[K\n")
        if surplus > 0:
            sys.stderr.write(f"\033[{surplus}A")

        sys.stderr.flush()
        self._lines_drawn = len(lines)

    def _clear_live_frame(self, max_live_lines: int) -> None:
        """Erase the bounded live frame before append-only final output."""
        if not self._lines_drawn:
            return
        if self._lines_drawn > max_live_lines:
            # The terminal was resized and some rows may now be in scrollback.
            # Never issue an unsafe cursor-up command for them.
            self._lines_drawn = 0
            return
        sys.stderr.write(f"\033[{self._lines_drawn}A")
        for _ in range(self._lines_drawn):
            sys.stderr.write("\r\033[K\n")
        self._lines_drawn = 0

    def _write_final_report(self, manifest: Manifest, now: float) -> None:
        """Print every final row once; scrolling is safe because it is static."""
        existing = [name for name in self._order if name in manifest.jobs]
        max_name_len = min(40, max((len(n) for n in existing), default=10))
        index_width = len(str(len(self._order)))
        for index, name in enumerate(self._order, start=1):
            entry = manifest.jobs.get(name)
            if entry is None:
                continue
            elapsed = self._elapsed(name, entry, now)
            elapsed_in_sg = self._elapsed_in_current_sg(name, entry, now)
            line = self._format_line(
                index, name, entry, index_width, max_name_len,
                elapsed, elapsed_in_sg,
            )
            sys.stderr.write(f"{_LIVE_INDENT}{line}\n")
        sys.stderr.flush()

    def _elapsed(self, name: str, entry, now: float) -> float:
        """Elapsed time, preferring persisted timestamps for resume accuracy."""
        submitted = _parse_manifest_time(entry.submitted_at)
        if submitted is not None:
            completed = (
                _parse_manifest_time(entry.completed_at)
                if _file_is_terminal(entry) else None
            )
            end = completed or datetime.now(timezone.utc)
            return max(0.0, (end - submitted).total_seconds())

        end_time = self._file_ended.get(name, now)
        return max(0.0, end_time - self._file_started.get(name, now))

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
        self, index: int, name: str, entry,
        index_width: int, name_width: int,
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
        return f"{index:>{index_width}}. {padded}  {body}  [{time_str}]"

    def _status_body(self, entry, elapsed_in_sg: float) -> str:
        s = entry.status
        if s == STATUS_COMPLETED and entry.reprocess:
            return "queued for reprocessing"
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
                if entry.warning:
                    # e.g. "✓ done (1 of 2 borehole page(s) failed extraction)"
                    short = entry.warning.split(" — ")[0].split(":")[0]
                    return f"✓ done ({short})"
                return "✓ done"
            return _bar_full() + "  downloading…"
        if s == STATUS_FAILED:
            error = _safe_terminal_text(entry.error or "unknown error")
            return f"✗ failed ({error})"
        if s == STATUS_SUBMIT_FAILED:
            error = _safe_terminal_text(entry.error or "unknown error")
            return f"✗ submit failed ({error})"
        return s


def _parse_manifest_time(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 manifest timestamp, tolerating legacy variants."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fit_terminal_width(text: str, width: int) -> str:
    """Keep a live row on one terminal line (best effort for Unicode)."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


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
    if entry.status == STATUS_COMPLETED and entry.reprocess:
        return False
    if entry.status in (STATUS_FAILED, STATUS_SUBMIT_FAILED):
        return True
    return entry.status == STATUS_COMPLETED and entry.downloaded
