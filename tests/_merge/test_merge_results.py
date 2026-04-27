"""Tests for the public `merge_results` orchestrator."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from openpyxl import Workbook

from boreholeai._merge import merge_results


def _make_job_dir(d: Path, *, gp=True, td=True, ags=True, pdf=True) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    if gp:
        wb = Workbook(); ws = wb.active
        ws.append(["Hole_ID", "material"]); ws.append(["BH1", "CLAY"])
        wb.save(d / "Borehole_ground_profile.xlsx")
    if td:
        wb = Workbook(); ws = wb.active
        ws.append(["Hole_ID", "test"]); ws.append(["BH1", "SPT"])
        wb.save(d / "Borehole_test_data.xlsx")
    if ags:
        (d / "Borehole_ags4.ags").write_text(
            '"GROUP","PROJ"\r\n"HEADING","PROJ_ID"\r\n"UNIT",""\r\n"TYPE","X"\r\n"DATA","P1"\r\n'
        )
    if pdf:
        (d / "BH1_annotated.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
    return d


def test_empty_input_raises(tmp_path):
    with pytest.raises(ValueError):
        merge_results([], tmp_path / "out")


def test_single_job_copies_originals(tmp_path):
    job = _make_job_dir(tmp_path / "j1")
    out = tmp_path / "out"

    result = merge_results([job], out)

    names = sorted(f.name for f in result.files)
    assert "Borehole_ground_profile.xlsx" in names
    assert "Borehole_test_data.xlsx" in names
    assert "Borehole_ags4.ags" in names
    assert "BH1_annotated.pdf" in names
    # No _merged suffix on single-job
    assert not any("_merged" in n for n in names)
    assert result.warnings == []
    assert not (out / "merge_warnings.txt").exists()


def test_multi_job_produces_merged_files_no_warnings(tmp_path):
    j1 = _make_job_dir(tmp_path / "j1")
    j2 = _make_job_dir(tmp_path / "j2")
    out = tmp_path / "out"

    result = merge_results([j1, j2], out)

    names = sorted(f.name for f in result.files)
    assert "Borehole_ground_profile_merged.xlsx" in names
    assert "Borehole_test_data_merged.xlsx" in names
    assert "Borehole_ags4_merged.ags" in names
    assert result.warnings == []
    assert not (out / "merge_warnings.txt").exists()


def test_missing_files_emit_warnings_and_log_file(tmp_path):
    j1 = _make_job_dir(tmp_path / "j1")
    j2 = _make_job_dir(tmp_path / "j2", ags=False, td=False)  # only gp
    out = tmp_path / "out"

    result = merge_results([j1, j2], out)

    # Warnings collected
    assert any("test_data" in w for w in result.warnings)
    assert any("AGS" in w for w in result.warnings)

    # merge_warnings.txt written and listed in result.files
    warn_path = out / "merge_warnings.txt"
    assert warn_path.exists()
    assert warn_path in result.files

    content = warn_path.read_text()
    assert "BoreholeAI merge warnings" in content
    assert "j2" in content


def test_partial_failure_still_merges_what_it_can(tmp_path):
    """One job missing AGS: merge still proceeds for the other one + Excel."""
    j1 = _make_job_dir(tmp_path / "j1")
    j2 = _make_job_dir(tmp_path / "j2", ags=False)
    out = tmp_path / "out"

    result = merge_results([j1, j2], out)

    names = {f.name for f in result.files}
    assert "Borehole_ground_profile_merged.xlsx" in names
    assert "Borehole_test_data_merged.xlsx" in names
    # AGS still produced — from j1's single AGS file
    assert "Borehole_ags4_merged.ags" in names
    assert any("AGS" in w for w in result.warnings)


def test_no_ags_anywhere_omits_ags_output(tmp_path):
    """If NO job has AGS, no merged AGS is produced."""
    j1 = _make_job_dir(tmp_path / "j1", ags=False)
    j2 = _make_job_dir(tmp_path / "j2", ags=False)
    out = tmp_path / "out"

    result = merge_results([j1, j2], out)

    names = {f.name for f in result.files}
    assert "Borehole_ags4_merged.ags" not in names
    assert sum("AGS" in w for w in result.warnings) == 2
