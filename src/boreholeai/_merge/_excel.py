"""Excel data-sheet merging with backend-matching styles.

Mirrors `mergeExcelBuffers` and `applyStyles` in
`frontend/src/app/api/jobs/download/route.ts`.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from boreholeai._merge._processing_info import (
    build_merged_processing_info,
    read_processing_info,
)

logger = logging.getLogger(__name__)

# ARGB hex colors — match the backend openpyxl output and the TS constants.
_HEADER_FILL = PatternFill(fill_type="solid", fgColor="FF4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFFFF", size=11)
_HEADER_SIDE = Side(style="medium", color="FF4472C4")
_HEADER_BORDER = Border(
    top=_HEADER_SIDE, bottom=_HEADER_SIDE, left=_HEADER_SIDE, right=_HEADER_SIDE,
)
_DATA_SIDE = Side(style="thin", color="FFB8CCE4")
_DATA_BORDER = Border(
    top=_DATA_SIDE, bottom=_DATA_SIDE, left=_DATA_SIDE, right=_DATA_SIDE,
)
_ALT_ROW_FILL = PatternFill(fill_type="solid", fgColor="FFF8F9FA")
_FLAGGED_FILL = PatternFill(fill_type="solid", fgColor="FFFFEB9C")
_FLAGGED_FONT = Font(color="FF9C6500")
_FAILED_HEADER_FONT = Font(bold=True, color="FFCC0000", size=11)
_FAILED_NAME_FILL = PatternFill(fill_type="solid", fgColor="FFFFFF00")

_LEFT_ALIGN_COLS = frozenset({
    "Metric", "Value", "material", "material_type", "parent_rock",
    "material_description", "geology_type", "consistency_density_strength",
    "consistency_density", "project_name", "borehole_location",
    "extraction_notes", "grid_datum", "test_results", "other_observations",
    "raw_observations", "SPT_raw", "GWL_date", "Cu_method", "reason_for_review",
})

_WRAP_TEXT_COLS = frozenset({
    "material", "material_description", "reason_for_review",
})


def merge_excel_files(paths: Iterable[Path]) -> bytes:
    """Merge multiple Excel files into one workbook, returned as bytes.

    Sheet rules:
      • "Processing Info" is aggregated via `build_merged_processing_info`.
      • Every other sheet is merged by name: first file's sheet copied
        whole (incl. header); subsequent files' rows appended (skip header).
      • Styles are applied to every sheet at the end.
    """
    paths = list(paths)
    if not paths:
        raise ValueError("merge_excel_files requires at least one path")

    merged = Workbook()
    # Workbook starts with one default sheet — remove it.
    merged.remove(merged.active)

    # Pass 1: collect Processing Info sheets and build aggregated one.
    processing_infos: list[dict[str, str]] = []
    for p in paths:
        wb = load_workbook(p, data_only=True)
        try:
            if "Processing Info" in wb.sheetnames:
                processing_infos.append(read_processing_info(wb["Processing Info"]))
        finally:
            wb.close()

    if processing_infos:
        dest_pi = merged.create_sheet("Processing Info")
        build_merged_processing_info(processing_infos, dest_pi)

    # Pass 2: merge data sheets.
    for p in paths:
        wb = load_workbook(p, data_only=True)
        try:
            for name in wb.sheetnames:
                if name == "Processing Info":
                    continue
                src = wb[name]
                if name not in merged.sheetnames:
                    dest = merged.create_sheet(name)
                    for row in src.iter_rows(values_only=True):
                        dest.append(list(row))
                else:
                    dest = merged[name]
                    rows = list(src.iter_rows(values_only=True))
                    for row in rows[1:]:
                        dest.append(list(row))
        finally:
            wb.close()

    # Apply styling to every sheet (including Processing Info).
    for sheet in merged.worksheets:
        _apply_styles(sheet)
        if sheet.title == "Processing Info":
            _style_processing_info(sheet)

    buf = io.BytesIO()
    merged.save(buf)
    return buf.getvalue()




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _apply_styles(sheet: Worksheet) -> None:
    """Apply backend-matching styles in place.

    Header row → blue fill, white bold, borders, center+wrap.
    Data rows  → alt-row fill on even rows, thin borders, alignment by column name.
    Flagged   → cells where `require_human_check` is true get yellow fill.
    """
    col_count = sheet.max_column
    row_count = sheet.max_row
    if row_count == 0 or col_count == 0:
        return

    # Read header names for column-specific styling.
    col_names: list[str] = []
    for c in range(1, col_count + 1):
        v = sheet.cell(row=1, column=c).value
        col_names.append("" if v is None else str(v))

    # Header row.
    for c in range(1, col_count + 1):
        cell = sheet.cell(row=1, column=c)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.border = _HEADER_BORDER
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True,
        )

    max_header_len = max((len(n) for n in col_names), default=10)
    sheet.row_dimensions[1].height = max(30, 25 + (max_header_len // 20) * 15)

    # Data rows.
    for r in range(2, row_count + 1):
        is_even = (r % 2) == 0
        for c in range(1, col_count + 1):
            cell = sheet.cell(row=r, column=c)
            col_name = col_names[c - 1] if c - 1 < len(col_names) else ""
            cell.border = _DATA_BORDER
            if is_even:
                cell.fill = _ALT_ROW_FILL
            if col_name in _WRAP_TEXT_COLS:
                cell.alignment = Alignment(
                    horizontal="left", vertical="center", wrap_text=True,
                )
            elif col_name in _LEFT_ALIGN_COLS:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            if col_name == "require_human_check" and (
                cell.value is True or str(cell.value) == "True"
            ):
                cell.fill = _FLAGGED_FILL
                cell.font = _FLAGGED_FONT

    # Auto-fit column widths (cap at 60).
    for c in range(1, col_count + 1):
        max_len = 0
        for r in range(1, row_count + 1):
            v = sheet.cell(row=r, column=c).value
            if v is not None:
                max_len = max(max_len, len(str(v)))
        sheet.column_dimensions[get_column_letter(c)].width = min(max_len + 2, 60)

    # Freeze header row.
    sheet.freeze_panes = "A2"


def _style_processing_info(sheet: Worksheet) -> None:
    """Red+bold on '— Failed Boreholes —', yellow fill on failed-name rows.

    Mirrors backend `_style_processing_info` in
    `Agentic 06/.../data_processing_utils.py`. Runs after `_apply_styles`
    so the yellow fill overrides the alt-row gray.
    """
    in_failed_section = False
    for r in range(2, sheet.max_row + 1):
        cell_value = str(sheet.cell(row=r, column=1).value or "")
        if cell_value == "— Failed Boreholes —":
            sheet.cell(row=r, column=1).font = _FAILED_HEADER_FONT
            in_failed_section = True
            continue
        if in_failed_section:
            if cell_value.startswith("  ") and cell_value.strip():
                sheet.cell(row=r, column=1).fill = _FAILED_NAME_FILL
                sheet.cell(row=r, column=2).fill = _FAILED_NAME_FILL
            elif cell_value.strip() and not cell_value.startswith("  "):
                in_failed_section = False
