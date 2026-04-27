"""Unit tests for Processing Info aggregation."""

from __future__ import annotations

from openpyxl import Workbook

from boreholeai._merge._processing_info import (
    _format_duration,
    _parse_duration,
    _parse_num,
    build_merged_processing_info,
    read_processing_info,
)


def test_parse_num_integers():
    assert _parse_num("5") == 5
    assert _parse_num("123") == 123


def test_parse_num_handles_garbage():
    assert _parse_num(None) == 0
    assert _parse_num("") == 0
    assert _parse_num("N/A") == 0
    assert _parse_num("Total: 5") == 5  # strips letters


def test_parse_duration():
    assert _parse_duration("5 min") == 300.0
    assert _parse_duration("30 sec") == 30.0
    assert _parse_duration("5 min 30 sec") == 330.0
    assert _parse_duration("N/A") == 0.0
    assert _parse_duration(None) == 0.0


def test_format_duration_roundtrip():
    for sec in [0, 30, 60, 330, 600, 3600]:
        if sec == 0:
            assert _format_duration(sec) == "N/A"
            continue
        formatted = _format_duration(sec)
        assert _parse_duration(formatted) == sec


def test_format_duration_under_a_minute():
    assert _format_duration(45) == "45 sec"


def test_format_duration_minutes_and_seconds():
    assert _format_duration(330) == "5 min 30 sec"


def test_read_processing_info_skips_blank_rows():
    wb = Workbook()
    ws = wb.active
    ws.append(["Metric", "Value"])
    ws.append([None, None])  # blank — should be skipped
    ws.append(["Total Boreholes", "3"])
    ws.append(["Total Pages Processed", "10"])

    info = read_processing_info(ws)
    assert info["Total Boreholes"] == "3"
    assert info["Total Pages Processed"] == "10"


def test_build_merged_aggregates_totals():
    info_a = {"Total Boreholes": "3", "Total Pages Processed": "10",
              "Total Processing Time": "5 min", "Material Records": "12"}
    info_b = {"Total Boreholes": "2", "Total Pages Processed": "5",
              "Total Processing Time": "3 min 30 sec", "Material Records": "8"}

    wb = Workbook(); dest = wb.active
    build_merged_processing_info([info_a, info_b], dest)

    rows = {r[0]: r[1] for r in dest.iter_rows(values_only=True) if r[0]}
    assert rows["Total Boreholes"] == "5"
    assert rows["Total Pages Processed"] == "15"
    assert rows["Total Processing Time"] == "8 min 30 sec"
    assert rows["Material Records"] == "20"


def test_build_merged_collects_failed_boreholes():
    """Failed entries are indented (start with two spaces) and listed under their header."""
    info = {"Total Boreholes": "2", "Boreholes Failed": "1",
            "  BH99": ""}  # the failed-entry convention

    wb = Workbook(); dest = wb.active
    build_merged_processing_info([info], dest)

    metrics = [r[0] for r in dest.iter_rows(values_only=True) if r[0]]
    assert "  BH99" in metrics


def test_build_merged_handles_zero_pages():
    """Avg Time per Page must be 'N/A' when pages == 0 (no division by zero)."""
    info = {"Total Boreholes": "0", "Total Pages Processed": "0",
            "Total Processing Time": "N/A"}

    wb = Workbook(); dest = wb.active
    build_merged_processing_info([info], dest)

    rows = {r[0]: r[1] for r in dest.iter_rows(values_only=True) if r[0]}
    assert rows["Average Time per Page"] == "N/A"
