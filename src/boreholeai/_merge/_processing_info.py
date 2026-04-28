"""Processing Info sheet aggregation for merged Excel output.

Mirrors `readProcessingInfo` / `buildMergedProcessingInfo` in
`frontend/src/app/api/jobs/download/route.ts`.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from openpyxl.worksheet.worksheet import Worksheet

logger = logging.getLogger(__name__)

# Metric names that are summary lines or markers, NOT per-record-type counts.
# Anything outside this set (and not a failed-borehole indented entry) is
# treated as a record count.
_SUMMARY_METRICS = frozenset({
    "Report Generated", "Workflow Start Time", "Workflow Completion Time",
    "Total Boreholes", "Boreholes Digitalised", "Boreholes Failed",
    "Total Pages Processed", "Pages Skipped (Non-borehole)",
    "Total Processing Time", "Average Time per Page",
    "— Processing Summary —", "— Record Counts —", "— Failed Boreholes —",
    "Metric",
})


def read_processing_info(sheet: Worksheet) -> dict[str, str]:
    """Read metric/value pairs from a Processing Info sheet.

    Returns insertion-ordered dict so failed-borehole rows (which start
    with two leading spaces) keep their order under the "— Failed
    Boreholes —" header in the source file.
    """
    info: dict[str, str] = {}
    for row in sheet.iter_rows(values_only=True):
        if not row:
            continue
        metric_raw = row[0]
        value_raw = row[1] if len(row) > 1 else None
        # Preserve leading whitespace on metric (used to detect failed boreholes).
        metric = "" if metric_raw is None else str(metric_raw).rstrip()
        value = "" if value_raw is None else str(value_raw).strip()
        if metric:
            info[metric] = value
    return info


def build_merged_processing_info(
    infos: list[dict[str, str]], dest_sheet: Worksheet,
) -> None:
    """Aggregate Processing Info across files and write to `dest_sheet`."""
    total_boreholes = 0
    boreholes_done = 0
    boreholes_failed = 0
    total_pages = 0
    pages_skipped = 0
    total_time_sec = 0.0
    failed_list: list[str] = []
    record_counts: dict[str, int] = {}

    for info in infos:
        total_boreholes += _parse_num(info.get("Total Boreholes"))
        boreholes_done += _parse_num(info.get("Boreholes Digitalised"))
        boreholes_failed += _parse_num(info.get("Boreholes Failed"))
        total_pages += _parse_num(info.get("Total Pages Processed"))
        pages_skipped += _parse_num(info.get("Pages Skipped (Non-borehole)"))
        total_time_sec += _parse_duration(info.get("Total Processing Time"))

        for metric in info:
            if metric.startswith("  ") and metric.strip() not in _SUMMARY_METRICS:
                failed_list.append(metric.strip())

        for metric, value in info.items():
            trimmed = metric.strip()
            if (
                trimmed not in _SUMMARY_METRICS
                and not metric.startswith("  ")
                and trimmed != ""
            ):
                record_counts[trimmed] = record_counts.get(trimmed, 0) + _parse_num(value)

    avg_time_per_page = (
        f"{(total_time_sec / total_pages / 60):.1f} min"
        if total_pages > 0 else "N/A"
    )

    rows: list[tuple[str, str]] = [
        ("Metric", "Value"),
        ("", ""),
        ("— Processing Summary —", ""),
        ("Report Generated", date.today().isoformat()),
        ("Total Boreholes", str(total_boreholes)),
        ("Boreholes Digitalised", str(boreholes_done)),
        ("Boreholes Failed", str(boreholes_failed)),
    ]

    if failed_list:
        rows.append(("", ""))
        rows.append(("— Failed Boreholes —", ""))
        rows.append(("", ""))
        for name in failed_list:
            rows.append((f"  {name}", ""))
        rows.append(("", ""))

    rows.extend([
        ("Total Pages Processed", str(total_pages)),
        ("Pages Skipped (Non-borehole)", str(pages_skipped)),
        ("Total Processing Time", _format_duration(total_time_sec)),
        ("Average Time per Page", avg_time_per_page),
        ("", ""),
        ("— Record Counts —", ""),
    ])

    for metric, count in record_counts.items():
        rows.append((metric, str(count)))

    for metric, value in rows:
        dest_sheet.append([metric, value])

    dest_sheet.column_dimensions["A"].width = 35
    dest_sheet.column_dimensions["B"].width = 25




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _parse_num(val: Any) -> int:
    """Parse an integer-valued metric. Mirrors TS `parseNum` (returns 0 on noise)."""
    if val is None or val == "":
        return 0
    s = re.sub(r"[^0-9.]", "", str(val))
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _parse_duration(val: Any) -> float:
    """Parse a "X min Y sec" duration into seconds. "N/A"/empty → 0.0."""
    if val is None or val == "" or val == "N/A":
        return 0.0
    s = str(val)
    total_sec = 0.0
    min_match = re.search(r"([\d.]+)\s*min", s)
    sec_match = re.search(r"([\d.]+)\s*sec", s)
    if min_match:
        total_sec += float(min_match.group(1)) * 60
    if sec_match:
        total_sec += float(sec_match.group(1))
    return total_sec


def _format_duration(total_sec: float) -> str:
    """Inverse of `_parse_duration`."""
    if total_sec <= 0:
        return "N/A"
    mins = int(total_sec // 60)
    secs = round(total_sec % 60)
    if mins == 0:
        return f"{secs} sec"
    return f"{mins} min {secs} sec"
