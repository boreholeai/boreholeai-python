"""Public merge entry for combining per-job results into one output dir.

After SDK fan-out completes, each completed job has its own directory
holding `Borehole_ground_profile*.xlsx`, `Borehole_test_data*.xlsx`,
`Borehole_ags4*.ags`, `Borehole_data.json`, and `*_annotated.pdf`.
`merge_results` reads those N directories and writes:

    output_dir/
        Borehole_ground_profile_merged.xlsx     (when N > 1; .xlsm when
                                                 macro_button=True and the
                                                 macro template applies)
        Borehole_test_data_merged.xlsx          (when N > 1)
        Borehole_ags4_merged.ags                (when N > 1)
        Borehole_data_merged.json               (when N > 1)
        annotated_pdf/<file>_annotated.pdf      (one per source file)
        merge_warnings.txt                      (only if warnings emitted)

For N == 1, files keep their original names (no `_merged` suffix),
matching the frontend "Download Selected" single-job behavior — except
the ground profile, which still becomes `.xlsm` when the macro button
applies.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from boreholeai._merge._ags import AgsMergeConflict, AgsSource, merge_ags_files
from boreholeai._merge._excel import merge_excel_files
from boreholeai._merge._json import merge_json_files

logger = logging.getLogger(__name__)

_WARNINGS_FILENAME = "merge_warnings.txt"

# Authored once in Excel from the .bas + base workbook in templates/ —
# see templates/README.md. Required only when macro_button=True.
_MACRO_TEMPLATE_PATH = (
    Path(__file__).parent / "templates" / "ground_profile_template.xlsm"
)


@dataclass
class MergeResult:
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def merge_results(
    input_dirs: list[Path],
    output_dir: Path,
    *,
    dir_labels: Optional[dict[Path, str]] = None,
    dir_job_ids: Optional[dict[Path, str]] = None,
    macro_button: bool = False,
) -> MergeResult:
    """Merge per-job result directories into `output_dir`.

    `dir_labels` (optional): map each input dir → display name used in
    warnings (e.g. the original PDF filename). If not provided, the dir's
    own basename (typically a UUID) is used.

    `dir_job_ids` optionally supplies the actual source job IDs for AGS merge
    provenance. Directory names are not interpreted as job IDs.

    `macro_button` (default False): when True, build the ground profile
    workbook on the packaged macro template so it ships with a
    "Regenerate derived tabs" VBA button on its Processing Info sheet,
    saved as `.xlsm` instead of `.xlsx`. The test-data workbook is
    unaffected; the default produces a plain macro-free `.xlsx`. If the
    template is missing or fails to apply, the ground profile falls back
    to the standard macro-free `.xlsx` and a warning is recorded — the
    macro is never allowed to fail the merge.

    Raises ValueError on empty input. Missing per-job files are recorded
    as warnings, not errors — the merge proceeds for whichever categories
    do have files.
    """
    if not input_dirs:
        raise ValueError("merge_results requires at least one input directory")

    input_dirs = [Path(d) for d in input_dirs]
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve label keys to absolute Paths so caller can pass either.
    resolved_labels: dict[Path, str] = {
        Path(k).resolve(): v for k, v in (dir_labels or {}).items()
    }
    resolved_job_ids = {
        Path(k).resolve(): v for k, v in (dir_job_ids or {}).items()
    }

    def _label(d: Path) -> str:
        return resolved_labels.get(d.resolve(), d.name)

    def _source_file(d: Path) -> str | None:
        label = resolved_labels.get(d.resolve())
        return Path(label).stem if label else None

    result = MergeResult()

    macro_template: Optional[Path] = None
    if macro_button:
        macro_template = _MACRO_TEMPLATE_PATH
        if not macro_template.is_file():
            result.warnings.append(
                f"macro button unavailable (template missing at "
                f"{macro_template}); ground profile written without it"
            )
            macro_template = None

    if len(input_dirs) == 1:
        source_file = _source_file(input_dirs[0])
        for src in _glob_results(input_dirs[0]):
            if src.name.endswith("_annotated.pdf"):
                dest_dir = output_dir / "annotated_pdf"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / src.name
            else:
                dest = output_dir / src.name
            source_map = {src: source_file} if source_file else None
            is_ground_profile = (
                src.suffix.lower() == ".xlsx"
                and src.name.startswith("Borehole_ground_profile")
            )
            macro_blob: bytes | None = None
            if is_ground_profile and macro_template is not None:
                # User-requested fallback: the macro is cosmetic — a broken
                # template must never fail delivery of the results.
                try:
                    macro_blob = merge_excel_files(
                        [src],
                        source_files=source_map,
                        macro_template=macro_template,
                    )
                except Exception as exc:
                    result.warnings.append(
                        f"macro button unavailable ({exc!r}); "
                        f"{src.name} written without it"
                    )
            if macro_blob is not None:
                dest = output_dir / (src.stem + ".xlsm")
                dest.write_bytes(macro_blob)
            elif src.suffix.lower() == ".xlsx" and source_map:
                dest.write_bytes(merge_excel_files([src], source_files=source_map))
            elif src.name == "Borehole_data.json" and source_map:
                dest.write_text(
                    merge_json_files([src], source_files=source_map),
                    encoding="utf-8",
                )
            else:
                shutil.copy2(src, dest)
            result.files.append(dest)
        return result

    ground_profile_paths: list[Path] = []
    test_data_paths: list[Path] = []
    ags_paths: list[Path] = []
    ags_sources: list[AgsSource] = []
    json_paths: list[Path] = []
    annotated_pdfs: list[Path] = []
    excel_source_files: dict[Path, str] = {}
    json_source_files: dict[Path, str] = {}
    json_ags_sources: dict[Path, AgsSource] = {}
    location_mapping: list[dict] = []
    ags_created = False

    for d in input_dirs:
        gp = _find_one(d, "Borehole_ground_profile*.xlsx")
        td = _find_one(d, "Borehole_test_data*.xlsx")
        ags = _find_one(d, "Borehole_ags4*.ags")
        js = _find_one(d, "Borehole_data.json")
        label = _label(d)
        source_file = _source_file(d)

        if gp is None:
            result.warnings.append(f"{label}: no ground_profile xlsx found")
        else:
            ground_profile_paths.append(gp)
            if source_file:
                excel_source_files[gp] = source_file

        if td is None:
            result.warnings.append(f"{label}: no test_data xlsx found")
        else:
            test_data_paths.append(td)
            if source_file:
                excel_source_files[td] = source_file

        if ags is None:
            result.warnings.append(f"{label}: no AGS file found")
        else:
            ags_paths.append(ags)
            # Without labels the directory name is the identity: unique per job, never the shared AGS filename.
            ags_sources.append(AgsSource(
                source_file=label, job_id=resolved_job_ids.get(d.resolve(), ""),
            ))

        if js is None:
            result.warnings.append(f"{label}: no Borehole_data JSON found")
        else:
            json_paths.append(js)
            if source_file:
                json_source_files[js] = source_file
            json_ags_sources[js] = AgsSource(
                source_file=label, job_id=resolved_job_ids.get(d.resolve(), ""),
            )

        annotated_pdfs.extend(sorted(d.glob("*_annotated.pdf")))

    if ground_profile_paths:
        macro_blob: bytes | None = None
        if macro_template is not None:
            # User-requested fallback: the macro is cosmetic — a broken
            # template must never fail delivery of the results.
            try:
                macro_blob = merge_excel_files(
                    ground_profile_paths,
                    source_files=excel_source_files,
                    macro_template=macro_template,
                )
            except Exception as exc:
                result.warnings.append(
                    f"macro button unavailable ({exc!r}); "
                    f"ground profile written without it"
                )
        if macro_blob is not None:
            out = output_dir / "Borehole_ground_profile_merged.xlsm"
            out.write_bytes(macro_blob)
        else:
            out = output_dir / "Borehole_ground_profile_merged.xlsx"
            out.write_bytes(merge_excel_files(
                ground_profile_paths,
                source_files=excel_source_files,
            ))
        result.files.append(out)
        logger.info(
            "merged ground_profile from %d file(s) → %s",
            len(ground_profile_paths), out,
        )

    if test_data_paths:
        out = output_dir / "Borehole_test_data_merged.xlsx"
        # "page" is per-source-file metadata (1-based page within THAT pdf);
        # in a cross-file merge it is ambiguous, so the merged output drops it.
        out.write_bytes(merge_excel_files(
            test_data_paths,
            drop_columns=frozenset({"page"}),
            source_files=excel_source_files,
        ))
        result.files.append(out)
        logger.info(
            "merged test_data from %d file(s) → %s",
            len(test_data_paths), out,
        )

    if ags_paths:
        out = output_dir / "Borehole_ags4_merged.ags"
        try:
            merged_ags = merge_ags_files(
                ags_paths, sources=ags_sources, location_mapping=location_mapping
            )
        except AgsMergeConflict as exc:
            # A prior run must not leave a stale merged AGS beside the conflict report.
            out.unlink(missing_ok=True)
            result.warnings.append(f"{exc}. No merged AGS produced; originals retained in original_ags/.")
            original_dir = output_dir / "original_ags"
            original_dir.mkdir(parents=True, exist_ok=True)
            for index, source in enumerate(ags_paths, 1):
                destination = original_dir / f"{index}_{source.name}"
                shutil.copy2(source, destination)
                result.files.append(destination)
        else:
            out.write_text(merged_ags, encoding="utf-8")
            ags_created = True
            result.files.append(out)
            logger.info("merged AGS from %d file(s) → %s", len(ags_paths), out)

    if json_paths:
        out = output_dir / "Borehole_data_merged.json"
        merged_json = json.loads(merge_json_files(
            json_paths, source_files=json_source_files, ags_sources=json_ags_sources,
        ))
        merged_json.setdefault("ags_export", {}).update({
            "reconciliation_scope": "source_ags",
            "merge_status": "created" if ags_created else "not_created" if ags_paths else "no_ags",
            "merged_ags_file": "Borehole_ags4_merged.ags" if ags_created else None,
            "location_mapping": location_mapping if ags_created else [],
        })
        out.write_text(json.dumps(merged_json, indent=2), encoding="utf-8")
        result.files.append(out)
        logger.info(
            "merged data JSON from %d file(s) → %s",
            len(json_paths), out,
        )

    if annotated_pdfs:
        annotated_dir = output_dir / "annotated_pdf"
        annotated_dir.mkdir(parents=True, exist_ok=True)
        for pdf in annotated_pdfs:
            dest = annotated_dir / pdf.name
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
    "Borehole_data.json",
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
