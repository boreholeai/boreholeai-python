# Changelog

All notable changes to the BoreholeAI Python SDK are documented here.
The project adheres to [Semantic Versioning](https://semver.org/).

## 0.4.2 — 2026-04-28

### Added

- **Visible enterprise purge confirmation.** When an API key has `purge_on_download` enabled, the SDK now prints a final summary line `🔴 Server files deleted for N job(s) (enterprise purge-on-download)` after the run, so users can see at a glance that the server-side `DELETE /v1/jobs/{id}` calls succeeded. Previously the purge happened silently — only visible at `INFO` log level.

### Changed

- The `Saved N file(s) to ...` line is now prefixed with `🟢` to make the success summary scan more easily.

## 0.4.1 — 2026-04-28

### Added

- **Live per-file progress display.** When stderr is a TTY, the SDK now renders one progress line per input file and updates them in place as each job moves through submit → queue → processing → download. Each line shows a filled bar with `pages_done/pages_total` plus elapsed time. Non-TTY callers (piped output, CI without a terminal) see no rendered progress — the SDK falls back to standard logging.

### Fixed

- **`merge_warnings.txt` now shows input filenames instead of job UUIDs.** Previously a merge warning looked like `1baf6345-…: no AGS file found`, which made it hard to know which input PDF was missing outputs. It now reads `AU-BH401.pdf: no AGS file found`.

### Internal

- `JobEntry` gained a `pages_done: int = 0` field, populated from poll responses to drive the new progress display.
- `run_batch()` accepts an optional `on_progress: Callable[[Manifest], None]` callback fired after every state change.
- `merge_results()` accepts an optional `dir_labels: dict[Path, str]` parameter so the caller can provide friendly names for warning messages.

## 0.4.0 — 2026-04-28

### Changed (architecture)

- **Folder uploads now fan out to N concurrent server-side jobs.** Previously the SDK sent every file in one batched POST that ran on a single worker. Now each file becomes its own job, processed in parallel across the worker pool — typically 3× faster on a 3-worker pool, scales linearly with worker count.
- **Per-job results are merged client-side in Python.** A new `boreholeai._merge` module ports the format-aware AGS / Excel merge from the frontend into the SDK. Run-to-run output is structurally equivalent to the frontend's "Download Selected" feature.
- **Same public API.** `client.process_documents(input_path, output_dir=...)` continues to work unchanged. Single-file uploads behave exactly as before.

### Added

- `concurrency=` parameter on `process_documents()` — bounds in-flight POSTs and downloads. Default `6`.
- **Resume on interrupt.** A manifest file (`output_dir/.boreholeai_manifest.json`) tracks per-file state. Re-running with the same `output_dir` skips already-completed work — survives Ctrl-C, network drops, and laptop sleep.
- **Partial-failure tolerance.** If some files fail processing, the rest are merged normally and surfaced via `JobResult.failures`. The batch only raises if every file fails.
- New fields on `JobResult`:
  - `job_ids: list[str]` — every server-side job ID in this batch
  - `successes: list[str]` — input filenames that completed
  - `failures: dict[str, str]` — filename → error message for failed files
- `merge_warnings.txt` — written to `output_dir` only when warnings occur during merge (e.g. a job missing one of its result files). No file when everything is clean.

### Behavior change to flag

- **Multi-file output filenames now carry a `_merged` suffix.** Single-file uploads still produce `Borehole_ground_profile.xlsx`; multi-file uploads now produce `Borehole_ground_profile_merged.xlsx`, `Borehole_test_data_merged.xlsx`, `Borehole_ags4_merged.ags`. Annotated PDFs are always per-file (unchanged).
- **GEOL / Material row counts may increase slightly.** The backend pipeline's folder-mode consolidation step (which fused contiguous "grades to" continuation layers) doesn't run on per-file jobs. No data is lost — same depths, descriptions, and geology — just expressed as separate rows where the old path produced a single fused row. Customers diffing outputs across SDK versions will see this drift; downstream tooling that counted GEOL rows may need adjusting.

### Removed

- The internal sync `APIClient` class is gone (the SDK was already a thin async wrapper). If you were reaching into `boreholeai._api.APIClient` directly (private path, not in `__all__`), use `boreholeai._api.APIClientAsync` instead.

### Dependencies

- Added: `openpyxl >=3.1, <4` for client-side Excel read/write/styling during merge.

## 0.3.0 and earlier

See git history.
