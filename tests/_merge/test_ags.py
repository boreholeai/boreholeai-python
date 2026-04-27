"""Unit tests for the AGS4 parser/writer/merge."""

from __future__ import annotations

import pytest

from boreholeai._merge._ags import (
    AgsGroup,
    OUTPUT_ORDER,
    _align_headings,
    _pad_row,
    _parse_ags,
    _parse_ags_line,
    _write_ags,
    merge_ags_files,
)


# --- _parse_ags_line ---

def test_parse_simple():
    assert _parse_ags_line('"DATA","BH1","0.0","1.5"') == ["DATA", "BH1", "0.0", "1.5"]


def test_parse_comma_inside_quoted_field():
    """Regression for the TS naive split-on-comma bug — fields with commas."""
    line = '"DATA","BH1","0","1","BASALT: weak, brown, friable","Y"'
    assert _parse_ags_line(line) == [
        "DATA", "BH1", "0", "1", "BASALT: weak, brown, friable", "Y",
    ]


def test_parse_escaped_quote():
    """`""` inside a quoted field is one literal `"`."""
    line = '"DATA","BH1","She said ""hello"""'
    assert _parse_ags_line(line) == ["DATA", "BH1", 'She said "hello"']


def test_parse_empty_fields():
    assert _parse_ags_line('"","",""') == ["", "", ""]


def test_parse_unquoted_passes_through():
    assert _parse_ags_line('DATA,BH1,0.0') == ["DATA", "BH1", "0.0"]


# --- _pad_row / _align_headings ---

def test_pad_row_extends():
    assert _pad_row(["a"], 3) == ["a", "", ""]


def test_pad_row_no_truncate():
    assert _pad_row(["a", "b", "c"], 2) == ["a", "b", "c"]


def test_align_headings_extends_existing():
    existing = AgsGroup(name="X", headings=["A"], unit=["m"], type=["X"], data=[["1"]])
    incoming = AgsGroup(name="X", headings=["A", "B"], unit=["m", "kg"], type=["X", "X"])
    _align_headings(existing, incoming)
    assert existing.headings == ["A", "B"]
    assert existing.unit == ["m", "kg"]
    assert existing.data == [["1", ""]]  # padded


# --- merge_ags_files: the format-specific rules ---

def _ags(blocks: dict) -> str:
    """Build a minimal AGS4 text from {group: (headings, units, types, [data...])}."""
    out = []
    for name, (h, u, t, d) in blocks.items():
        lines = [f'"GROUP","{name}"']
        lines.append(",".join(f'"{x}"' for x in ["HEADING"] + h))
        lines.append(",".join(f'"{x}"' for x in ["UNIT"] + u))
        lines.append(",".join(f'"{x}"' for x in ["TYPE"] + t))
        for row in d:
            lines.append(",".join(f'"{x}"' for x in ["DATA"] + row))
        out.append("\r\n".join(lines))
    return "\r\n\r\n".join(out) + "\r\n"


def test_singleton_keeps_latest(tmp_path):
    a = tmp_path / "a.ags"
    b = tmp_path / "b.ags"
    a.write_text(_ags({"PROJ": (["PROJ_ID"], [""], ["X"], [["A"]])}))
    b.write_text(_ags({"PROJ": (["PROJ_ID"], [""], ["X"], [["B"]])}))

    merged = _parse_ags(merge_ags_files([a, b]))
    proj = next(g for g in merged if g.name == "PROJ")
    assert proj.data == [["B"]]  # last file wins


def test_loca_id_dedup_last_wins(tmp_path):
    a = tmp_path / "a.ags"
    b = tmp_path / "b.ags"
    rows_a = [["BH1", "0", "1"], ["BH2", "0", "2"]]
    rows_b = [["BH1", "0", "9"]]  # BH1 repeats — should overwrite
    headings = ["LOCA_ID", "TOP", "BASE"]
    a.write_text(_ags({"GEOL": (headings, ["", "m", "m"], ["ID", "2DP", "2DP"], rows_a)}))
    b.write_text(_ags({"GEOL": (headings, ["", "m", "m"], ["ID", "2DP", "2DP"], rows_b)}))

    merged = _parse_ags(merge_ags_files([a, b]))
    geol = next(g for g in merged if g.name == "GEOL")
    bh1_rows = [r for r in geol.data if r[0] == "BH1"]
    bh2_rows = [r for r in geol.data if r[0] == "BH2"]
    assert bh1_rows == [["BH1", "0", "9"]]
    assert bh2_rows == [["BH2", "0", "2"]]


def test_meta_group_dedup_by_full_row(tmp_path):
    a = tmp_path / "a.ags"
    b = tmp_path / "b.ags"
    headings = ["UNIT_UNIT", "UNIT_DESC"]
    a.write_text(_ags({"UNIT": (headings, ["", ""], ["X", "X"],
                                [["m", "metres"], ["kg", "kilograms"]])}))
    b.write_text(_ags({"UNIT": (headings, ["", ""], ["X", "X"],
                                [["m", "metres"], ["s", "seconds"]])}))

    merged = _parse_ags(merge_ags_files([a, b]))
    unit = next(g for g in merged if g.name == "UNIT")
    assert sorted(unit.data) == [["kg", "kilograms"], ["m", "metres"], ["s", "seconds"]]


def test_no_loca_id_appends(tmp_path):
    a = tmp_path / "a.ags"
    b = tmp_path / "b.ags"
    headings = ["A", "B"]
    a.write_text(_ags({"X": (headings, ["", ""], ["X", "X"], [["1", "2"]])}))
    b.write_text(_ags({"X": (headings, ["", ""], ["X", "X"], [["3", "4"]])}))

    merged = _parse_ags(merge_ags_files([a, b]))
    x = next(g for g in merged if g.name == "X")
    assert sorted(x.data) == [["1", "2"], ["3", "4"]]


def test_align_headings_when_incoming_has_extra_column(tmp_path):
    a = tmp_path / "a.ags"
    b = tmp_path / "b.ags"
    a.write_text(_ags({"GEOL": (["LOCA_ID", "A"], ["", ""], ["ID", "X"], [["BH1", "x"]])}))
    b.write_text(_ags({"GEOL": (["LOCA_ID", "A", "B"], ["", "", ""], ["ID", "X", "X"],
                                [["BH2", "y", "z"]])}))

    merged = _parse_ags(merge_ags_files([a, b]))
    geol = next(g for g in merged if g.name == "GEOL")
    assert geol.headings == ["LOCA_ID", "A", "B"]
    bh1 = next(r for r in geol.data if r[0] == "BH1")
    assert bh1 == ["BH1", "x", ""]  # padded


# --- _write_ags ---

def test_write_uses_output_order(tmp_path):
    a = tmp_path / "a.ags"
    a.write_text(_ags({
        "GEOL": (["LOCA_ID"], [""], ["ID"], [["BH1"]]),
        "PROJ": (["PROJ_ID"], [""], ["X"], [["P"]]),
        "TRAN": (["TRAN_DATE"], [""], ["DT"], [["2026-01-01"]]),
    }))
    text = merge_ags_files([a])
    proj_pos = text.index('"GROUP","PROJ"')
    tran_pos = text.index('"GROUP","TRAN"')
    geol_pos = text.index('"GROUP","GEOL"')
    assert proj_pos < tran_pos < geol_pos


def test_write_escapes_embedded_quote():
    groups = {"X": AgsGroup(name="X", headings=["A"], unit=[""], type=["X"],
                            data=[['He said "hi"']])}
    text = _write_ags(groups)
    assert '""hi""' in text  # embedded quotes doubled


def test_empty_input_raises(tmp_path):
    with pytest.raises(ValueError):
        merge_ags_files([])


# --- Roundtrip / idempotence ---

def test_merge_one_file_idempotent(tmp_path):
    a = tmp_path / "a.ags"
    a.write_text(_ags({"GEOL": (["LOCA_ID", "A"], ["", ""], ["ID", "X"],
                                 [["BH1", "x"], ["BH2", "y"]])}))
    once = merge_ags_files([a])
    twice = merge_ags_files([a, a])  # dedup means second copy is no-op
    assert _parse_ags(once)[0].data == _parse_ags(twice)[0].data
