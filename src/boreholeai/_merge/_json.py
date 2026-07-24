"""Borehole_data.json merging.

Mirrors the TypeScript implementation `mergeJsonBuffers` in
`frontend/src/app/api/jobs/download/route.ts`. Any rule change here must
also land in the TS source.

The merge concatenates the record arrays under each section key
(ground_profile.*, test_data.*) across all per-job files. The
``processing_info`` section is the exception: it is aggregated the same clean
way as the Excel merge (via :func:`aggregate_processing_info`) rather than
concatenated.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Mapping

from ._processing_info import aggregate_processing_info

logger = logging.getLogger(__name__)

# JSON section key whose rows are an aggregated summary, not appendable records.
_PROCESSING_INFO_KEY = "processing_info"

# "page" is per-source-file metadata (1-based page within THAT pdf); in a
# cross-file merge it is ambiguous, so test_data records drop it — mirrors
# the Excel merge dropping the "page" column from Borehole_test_data_merged.
_TEST_DATA_GROUP = "test_data"
_DROPPED_TEST_DATA_FIELD = "page"


def merge_json_files(
    paths: Iterable[Path],
    source_files: Mapping[Path, str] | None = None,
) -> str:
    """Merge Borehole_data.json files into one JSON string (indent=2).

    An unparseable per-job file is skipped (logged) so the merge still
    produces output for the remaining jobs — mirrors the TS
    `mergeJsonBuffers` skip-and-continue behaviour and the AGS/Excel
    mergers, which degrade rather than abort the whole batch.
    """
    merged: dict[str, dict[str, list[object]]] = {}
    resolved_source_files = {
        Path(path).resolve(): stem
        for path, stem in (source_files or {}).items()
    }
    # group -> list of per-file metric→value dicts. processing_info is a summary,
    # so it is aggregated the same clean way as the Excel merge after all files
    # are read — not naively concatenated like the record sections.
    pi_infos: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        source_file = resolved_source_files.get(Path(path).resolve())
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("skipping malformed data JSON: %s", path)
            continue
        # Valid-but-wrong-shape (null, list, scalar) is skipped too — without
        # this guard `doc.items()` raises and aborts the whole merge.
        if not isinstance(doc, dict):
            logger.warning("skipping non-object data JSON: %s", path)
            continue
        for group, section in doc.items():
            if not isinstance(section, dict):
                continue
            dest = merged.setdefault(group, {})
            for key, rows in section.items():
                if not isinstance(rows, list):
                    continue
                if key == _PROCESSING_INFO_KEY:
                    pi_infos.setdefault(group, []).append(_records_to_info(rows))
                else:
                    rows = [
                        _with_source_file(
                            {
                                k: v for k, v in record.items()
                                if not (
                                    group == _TEST_DATA_GROUP
                                    and k == _DROPPED_TEST_DATA_FIELD
                                )
                            },
                            source_file,
                        )
                        if isinstance(record, dict) else record
                        for record in rows
                    ]
                    dest.setdefault(key, []).extend(rows)

    # Aggregate processing_info per group, mirroring the Excel merge. The
    # leading ("Metric", "Value") header tuple is a worksheet header, not a
    # JSON record, so it is dropped.
    for group, infos in pi_infos.items():
        merged.setdefault(group, {})[_PROCESSING_INFO_KEY] = [
            {"Metric": metric, "Value": value}
            for metric, value in aggregate_processing_info(infos)
            if (metric, value) != ("Metric", "Value")
        ]

    return json.dumps(merged, indent=2)




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _with_source_file(
    record: dict[str, object],
    source_file: str | None,
) -> dict[str, object]:
    """Put Source_File first, backfilling legacy records when possible."""
    existing = record.get("Source_File")
    value = (
        source_file
        if existing is None or str(existing).strip() == ""
        else existing
    )
    if value is None:
        return record
    return {
        "Source_File": value,
        **{key: item for key, item in record.items() if key != "Source_File"},
    }


def _records_to_info(records: list[object]) -> dict[str, str]:
    """Convert a processing_info record list to a metric→value dict.

    Mirrors `read_processing_info` (Excel path): preserve leading whitespace on
    the metric (marks failed-borehole rows), strip the value.
    """
    info: dict[str, str] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        metric_raw = rec.get("Metric")
        value_raw = rec.get("Value")
        metric = "" if metric_raw is None else str(metric_raw).rstrip()
        value = "" if value_raw is None else str(value_raw).strip()
        if metric:
            info[metric] = value
    return info
