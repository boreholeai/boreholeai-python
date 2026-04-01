"""BoreholeAI Python SDK client."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from boreholeai._api import APIClient, DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from boreholeai._files import collect_files
from boreholeai._version import __version__, __version_date__
from boreholeai._types import FileResult, JobResult
from boreholeai.exceptions import JobFailedError

# Polling configuration
_POLL_INITIAL_INTERVAL = 2.0   # seconds
_POLL_MAX_INTERVAL = 10.0      # seconds
_POLL_BACKOFF_FACTOR = 1.5

# Default output directory
_DEFAULT_OUTPUT_DIR = "./results"

# Braille spinner frames
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class BoreholeAI:
    """Client for the BoreholeAI API.

    Usage::

        from boreholeai import BoreholeAI

        client = BoreholeAI(api_key="bhai_xxx")

        # Single file
        results = client.process_documents("borehole.pdf")

        # Folder — all PDFs + images processed together, merged output
        results = client.process_documents("./logs/", output_dir="./results")
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        date_suffix = f" ({__version_date__})" if __version_date__ != "dev" else ""
        _log(f"boreholeai v{__version__}{date_suffix}")
        _log("To check for updates: pip install --upgrade boreholeai")
        self._api = APIClient(api_key=api_key, base_url=base_url, timeout=timeout)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._api.close()

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
        """Upload files, process them, and download results.

        Args:
            input_path: Path to a single file or a directory of files.
                Supported formats: PDF, PNG, JPG, JPEG, TIF, TIFF, WebP.
            output_dir: Directory to save result files. Created if it doesn't exist.

        Returns:
            JobResult with job metadata and list of downloaded files.

        Raises:
            FileNotFoundError: If input_path doesn't exist.
            ValueError: If no supported files found.
            AuthenticationError: If API key is invalid.
            InsufficientCreditsError: If not enough credits.
            JobFailedError: If processing fails on the server.
        """
        files = collect_files(input_path)
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)

        filenames = ", ".join(f.name for f in files)
        _log(f"Starting {filenames}")

        job_data = self._api.create_job(files)
        job_id = job_data["job_id"]
        num_pages = job_data["num_pages"]
        short_id = job_id[:8]
        _log(f"Job {short_id} created — {num_pages} page(s)")

        # Poll
        start = time.monotonic()
        result = self._poll_until_done(job_id, num_pages, start)
        elapsed = time.monotonic() - start

        _log(f"Completed {num_pages} page(s) in {_fmt_time(elapsed)}")

        # Download results
        downloaded = self._download_results(job_id, out)

        return JobResult(
            job_id=job_id,
            status=result["status"],
            num_pages=num_pages,
            credits_used=num_pages,
            files=downloaded,
        )

    def _poll_until_done(self, job_id: str, num_pages: int, start: float) -> dict:
        """Poll GET /v1/jobs/{id} until status is completed or failed."""
        interval = _POLL_INITIAL_INTERVAL
        spin_idx = 0

        while True:
            data = self._api.get_job(job_id)
            status = data["status"]

            if status == "completed":
                # Clear the spinner line
                _clear_line()
                return data

            if status == "failed":
                _clear_line()
                error = data.get("error_message", "Unknown error")
                raise JobFailedError(f"Job {job_id} failed: {error}")

            progress = data.get("progress") or {}
            pages_done = progress.get("pages_done", 0)
            pages_total = progress.get("pages_total", num_pages)
            elapsed = time.monotonic() - start
            spinner = _SPINNER[spin_idx % len(_SPINNER)]
            spin_idx += 1

            _spinner_line(
                f"{spinner} Processing {pages_done}/{pages_total} pages "
                f"[{_fmt_time(elapsed)}]"
            )

            time.sleep(interval)
            interval = min(interval * _POLL_BACKOFF_FACTOR, _POLL_MAX_INTERVAL)

    def _download_results(self, job_id: str, output_dir: Path) -> list[FileResult]:
        """Download all result files to output_dir."""
        results_data = self._api.get_results(job_id)
        downloaded: list[FileResult] = []

        for file_info in results_data.get("files", []):
            filename = file_info["filename"]
            url = file_info["url"]

            content = self._api.download_file(url)
            dest = output_dir / filename
            dest.write_bytes(content)
            downloaded.append(FileResult(filename=filename, path=dest))

        _log(f"Saved {len(downloaded)} file(s) to {output_dir}")
        for f in downloaded:
            _log(f"  {f.filename}")
        return downloaded




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _log(message: str) -> None:
    """Print a status line to stderr."""
    print(f"  {message}", file=sys.stderr, flush=True)


def _spinner_line(message: str) -> None:
    """Overwrite the current line in-place (for spinner updates)."""
    sys.stderr.write(f"\r  {message}\033[K")
    sys.stderr.flush()


def _clear_line() -> None:
    """Clear the spinner line so the next _log prints cleanly."""
    sys.stderr.write("\r\033[K")
    sys.stderr.flush()


def _fmt_time(seconds: float) -> str:
    """Format elapsed seconds as a human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    return f"{m}m {s}s"
