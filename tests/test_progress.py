"""Tests for `_progress.compute_progress` — the weighted-subgraph progress
algorithm mirrored from the frontend's `src/lib/progress.ts`.
"""

from __future__ import annotations

from boreholeai._progress import compute_progress


def test_zero_progress_at_start():
    pct = compute_progress(page=1, pages_total=1, completed_subgraphs=[], elapsed_in_current_sg=0)
    assert pct == 0.0


def test_caps_below_100_until_terminal():
    """Even if all 10 subgraphs of all pages are completed, the function
    never returns 100 — that's reserved for the renderer to use when status
    is 'completed'."""
    pct = compute_progress(
        page=1, pages_total=1,
        completed_subgraphs=["SG01", "SG02", "SG03", "SG04", "SG05",
                             "SG06", "SG07", "SG08", "SG09", "SG10"],
        elapsed_in_current_sg=0,
    )
    assert pct < 100.0
    assert pct >= 99.0  # we expect ~99


def test_sg01_has_largest_weight():
    """SG01 is 40% of a page's work — completing it should give a meaningful
    bump on a single-page job."""
    pct_no = compute_progress(page=1, pages_total=1, completed_subgraphs=[], elapsed_in_current_sg=0)
    pct_with_sg01 = compute_progress(
        page=1, pages_total=1, completed_subgraphs=["SG01"], elapsed_in_current_sg=0,
    )
    # Completing SG01 alone should jump roughly 40 percentage points.
    assert pct_with_sg01 - pct_no >= 35


def test_smooth_interpolation_within_subgraph():
    """Half-way through SG01's estimated 120s, the bar should be ~half-way
    through SG01's 40% slice (so ~20% overall on a 1-page job)."""
    half = compute_progress(
        page=1, pages_total=1, completed_subgraphs=[], elapsed_in_current_sg=60,
    )
    full = compute_progress(
        page=1, pages_total=1, completed_subgraphs=[], elapsed_in_current_sg=120,
    )
    assert 18 <= half <= 22  # ~20%
    # full ≈ 95% of SG01's 40% weight = 38%
    assert 35 <= full <= 40


def test_multi_page_distributes_weight():
    """On a 2-page job, page 1 fully done = 50%."""
    pct = compute_progress(
        page=2, pages_total=2,
        completed_subgraphs=[],   # we're on page 2 now, fresh start
        elapsed_in_current_sg=0,
    )
    # Page 1 is done (50%), page 2 just started (~0%) → ≈ 50
    assert 49 <= pct <= 51


def test_pages_total_zero_returns_zero():
    """Defensive: pages_total of 0 (shouldn't happen) doesn't crash."""
    pct = compute_progress(page=1, pages_total=0, completed_subgraphs=[], elapsed_in_current_sg=0)
    assert pct == 0.0


def test_unknown_subgraph_id_ignored():
    """A subgraph ID not in SUBGRAPH_TRACKING contributes 0 weight rather
    than crashing — defensive against future pipeline additions."""
    pct = compute_progress(
        page=1, pages_total=1,
        completed_subgraphs=["SG_FUTURE"], elapsed_in_current_sg=0,
    )
    # Should be small (just the SG01 in-progress interpolation, which is 0)
    assert 0 <= pct < 5
