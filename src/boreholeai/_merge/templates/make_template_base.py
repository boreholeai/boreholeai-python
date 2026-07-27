"""Regenerate ground_profile_template_base.xlsx (the pre-VBA template base).

WARNING: the checked-in base was later edited in Excel and carries the
form-control button, already assigned to the macro. Re-running this script
OVERWRITES it with a buttonless workbook, after which the button must be
re-drawn by hand (see README.md). Only run it to rebuild from scratch.

Run from the repo root:  .venv/bin/python src/boreholeai/_merge/templates/make_template_base.py
"""

from pathlib import Path

from openpyxl import Workbook


def main() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Processing Info"
    # Same widths _style_processing_info applies at merge time.
    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 25
    out = Path(__file__).parent / "ground_profile_template_base.xlsx"
    wb.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
