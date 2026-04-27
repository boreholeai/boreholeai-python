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
from boreholeai._batch import BatchResult, run_batch
from boreholeai._files import collect_files
from boreholeai._merge import merge_results
from boreholeai._version import __version__, __version_date__
from boreholeai._types import FileResult, JobResult

_DEFAULT_OUTPUT_DIR = "./results"
_DEFAULT_CONCURRENCY = 6


class BoreholeAI:
    """Client for the BoreholeAI API.

    Usage::

        from boreholeai import BoreholeAI

        client = BoreholeAI(api_key="bhai_xxx")

        # Single file
        result = client.process_documents("borehole.pdf")

        # Folder — fans out to N concurrent server jobs, merges results
        result = client.process_documents("./logs/", output_dir="./results", concurrency=6)

    For a folder of N files, this submits N concurrent jobs (bounded by
    `concurrency`), polls each, downloads all outputs, then produces one
    merged ground_profile / test_data / AGS file.

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
        concurrency: int = _DEFAULT_CONCURRENCY,
    ) -> JobResult:
        """Submit, process, and merge a single file or a folder of files.

        Args:
            input_path: Single file or directory.
                Supported: PDF, PNG, JPG, JPEG, TIF, TIFF, WebP.
            output_dir: Where merged results land. Created if missing.
                If `.boreholeai_manifest.json` is present from a prior run,
                that work is resumed; already-completed files are skipped.
            concurrency: Max in-flight POSTs and downloads (default 6).

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

        _log(f"Starting {len(files)} file(s) (concurrency={concurrency})")
        start = time.monotonic()

        batch = asyncio.run(self._run(files, out, concurrency))

        elapsed = time.monotonic() - start
        return self._finalise(batch, files, out, elapsed)

    async def _run(
        self, files: list[Path], output_dir: Path, concurrency: int,
    ) -> BatchResult:
        async with APIClientAsync(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            _transport=self._transport,
        ) as client:
            return await run_batch(
                client, files, output_dir, concurrency=concurrency,
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

        merged_files: list[FileResult] = []
        if success_dirs:
            mr = merge_results(success_dirs, output_dir)
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
            _log(f"Saved {len(merged_files)} file(s) to {output_dir}")
            for f in merged_files:
                _log(f"  {f.filename}")

        # Clean up the per-job workdir only when everything succeeded.
        # On partial/failed runs we keep it so the user can inspect or resume.
        if status == "completed" and batch.workdir is not None and batch.workdir.exists():
            shutil.rmtree(batch.workdir, ignore_errors=True)

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
