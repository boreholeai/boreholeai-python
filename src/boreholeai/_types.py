"""Response types for the BoreholeAI SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileResult:
    """A single downloaded result file."""

    filename: str
    path: Path


@dataclass
class JobResult:
    """Result of processing a batch (one or more input files).

    From 0.4.0 onward, every input file becomes its own server-side job,
    so a single `JobResult` summarises N jobs.
    """

    job_id: str                                       # primary (first) job id
    status: str                                       # "completed" | "partial" | "failed"
    num_pages: int                                    # sum across all jobs
    credits_used: int                                 # sum across all jobs (= num_pages)
    files: list[FileResult] = field(default_factory=list)         # merged outputs

    # New in 0.4.0 — per-file detail for fan-out batches.
    job_ids: list[str] = field(default_factory=list)
    successes: list[str] = field(default_factory=list)            # input filenames
    failures: dict[str, str] = field(default_factory=dict)        # filename → error
