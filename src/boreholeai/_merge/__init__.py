"""Public merge entry for combining per-job results into one output dir.

After SDK fan-out completes, each completed job has its own directory
holding `Borehole_ground_profile*.xlsx`, `Borehole_test_data*.xlsx`,
`Borehole_ags4*.ags`, and `*_annotated.pdf`. `merge_results` reads those
N directories and writes:

    output_dir/
        Borehole_ground_profile_merged.xlsx     (when N > 1)
        Borehole_test_data_merged.xlsx          (when N > 1)
        Borehole_ags4_merged.ags                (when N > 1)
        <file>_annotated.pdf                    (one per source file)
        merge_warnings.txt                      (only if warnings emitted)

For N == 1, files are copied with their original names (no `_merged`
suffix), matching the frontend "Download Selected" single-job behavior.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from boreholeai._merge._ags import merge_ags_files
from boreholeai._merge._excel import merge_excel_files

logger = logging.getLogger(__name__)

_WARNINGS_FILENAME = "merge_warnings.txt"


@dataclass
class MergeResult:
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def merge_results(
    input_dirs: list[Path],
    output_dir: Path,
) -> MergeResult:
    """Merge per-job result directories into `output_dir`.

    Raises ValueError on empty input. Missing per-job files are recorded
    as warnings, not errors — the merge proceeds for whichever categories
    do have files.
    """
    if not input_dirs:
        raise ValueError("merge_results requires at least one input directory")

    input_dirs = [Path(d) for d in input_dirs]
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    result = MergeResult()

    if len(input_dirs) == 1:
        for src in _glob_results(input_dirs[0]):
            dest = output_dir / src.name
            shutil.copy2(src, dest)
            result.files.append(dest)
        return result

    ground_profile_paths: list[Path] = []
    test_data_paths: list[Path] = []
    ags_paths: list[Path] = []
    annotated_pdfs: list[Path] = []

    for d in input_dirs:
        gp = _find_one(d, "Borehole_ground_profile*.xlsx")
        td = _find_one(d, "Borehole_test_data*.xlsx")
        ags = _find_one(d, "Borehole_ags4*.ags")

        if gp is None:
            result.warnings.append(f"{d.name}: no ground_profile xlsx found")
        else:
            ground_profile_paths.append(gp)

        if td is None:
            result.warnings.append(f"{d.name}: no test_data xlsx found")
        else:
            test_data_paths.append(td)

        if ags is None:
            result.warnings.append(f"{d.name}: no AGS file found")
        else:
            ags_paths.append(ags)

        annotated_pdfs.extend(sorted(d.glob("*_annotated.pdf")))

    if ground_profile_paths:
        out = output_dir / "Borehole_ground_profile_merged.xlsx"
        out.write_bytes(merge_excel_files(ground_profile_paths))
        result.files.append(out)
        logger.info(
            "merged ground_profile from %d file(s) → %s",
            len(ground_profile_paths), out,
        )

    if test_data_paths:
        out = output_dir / "Borehole_test_data_merged.xlsx"
        out.write_bytes(merge_excel_files(test_data_paths))
        result.files.append(out)
        logger.info(
            "merged test_data from %d file(s) → %s",
            len(test_data_paths), out,
        )

    if ags_paths:
        out = output_dir / "Borehole_ags4_merged.ags"
        out.write_text(merge_ags_files(ags_paths), encoding="utf-8")
        result.files.append(out)
        logger.info(
            "merged AGS from %d file(s) → %s",
            len(ags_paths), out,
        )

    for pdf in annotated_pdfs:
        dest = output_dir / pdf.name
        shutil.copy2(pdf, dest)
        result.files.append(dest)

    for w in result.warnings:
        logger.warning(w)

    if result.warnings:
        warnings_path = _write_warnings_file(output_dir, result.warnings)
        result.files.append(warnings_path)

    return result




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

_RESULT_PATTERNS = (
    "Borehole_ground_profile*.xlsx",
    "Borehole_test_data*.xlsx",
    "Borehole_ags4*.ags",
    "*_annotated.pdf",
)


def _glob_results(d: Path) -> list[Path]:
    """All result files in a job dir, in deterministic order."""
    files: list[Path] = []
    for pattern in _RESULT_PATTERNS:
        files.extend(sorted(d.glob(pattern)))
    return files


def _find_one(d: Path, pattern: str) -> Path | None:
    """First match for a glob pattern in `d`, or None if missing."""
    matches = sorted(d.glob(pattern))
    return matches[0] if matches else None


def _write_warnings_file(output_dir: Path, warnings: list[str]) -> Path:
    """Write merge_warnings.txt only when warnings are present."""
    path = output_dir / _WARNINGS_FILENAME
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"BoreholeAI merge warnings — {timestamp}",
        "=" * 50,
        "",
    ]
    lines.extend(f"  - {w}" for w in warnings)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
