"""Job progress estimation — mirrors the frontend's `src/lib/progress.ts`.

Both implementations are kept in sync by hand and ultimately mirror the
backend's `tracking_config.py`. Any pipeline change to subgraph weights
or estimated durations needs to land in all three files.
"""

from __future__ import annotations

# Subgraph weight = relative share of one page's work (sums to 100).
# `estimated_seconds` = expected wall-clock duration of that subgraph
# under typical ML load. Used for smooth interpolation WITHIN a subgraph.
SUBGRAPH_TRACKING: dict[str, dict[str, int]] = {
    "SG01": {"weight": 40, "estimated_seconds": 120},
    "SG02": {"weight": 15, "estimated_seconds": 60},
    "SG03": {"weight": 12, "estimated_seconds": 50},
    "SG04": {"weight":  8, "estimated_seconds": 40},
    "SG05": {"weight":  5, "estimated_seconds": 20},
    "SG06": {"weight":  5, "estimated_seconds": 25},
    "SG07": {"weight":  5, "estimated_seconds": 15},
    "SG08": {"weight":  4, "estimated_seconds": 15},
    "SG09": {"weight":  3, "estimated_seconds": 10},
    "SG10": {"weight":  3, "estimated_seconds": 10},
}

SUBGRAPH_ORDER = ("SG01", "SG02", "SG03", "SG04", "SG05",
                  "SG06", "SG07", "SG08", "SG09", "SG10")

# Cap below 100 so the bar never hits 100% until status flips to "completed".
_MAX_PCT_WHILE_RUNNING = 99.0


def compute_progress(
    page: int,
    pages_total: int,
    completed_subgraphs: list[str],
    elapsed_in_current_sg: float,
) -> float:
    """Return job progress percentage in [0, 99].

    Weighted across pages and subgraphs:
      • completed pages contribute (page-1) × pageWeight
      • completed subgraphs within the current page contribute their weight
      • the in-progress subgraph contributes a partial slice based on
        `elapsed_in_current_sg / estimated_seconds`, capped at 95% of
        that subgraph's weight (so the bar never claims a subgraph is
        done before its DB write lands)

    The renderer is responsible for tracking `elapsed_in_current_sg`
    (time since `completed_subgraphs` last changed) and for snapping
    to 100 when the job's status becomes "completed".
    """
    if pages_total <= 0:
        return 0.0

    page_weight = 100.0 / pages_total
    completed_pages = max(0, page - 1)
    base_pct_from_pages = completed_pages * page_weight

    # Sum completed subgraph weights within the current page.
    completed_weight = sum(
        SUBGRAPH_TRACKING.get(sg, {}).get("weight", 0)
        for sg in completed_subgraphs
    )
    completed_pct_in_page = (completed_weight / 100.0) * page_weight

    # Find the next un-completed subgraph (the one currently running).
    completed_set = set(completed_subgraphs)
    current_sg = next(
        (sg for sg in SUBGRAPH_ORDER if sg not in completed_set),
        None,
    )
    if current_sg is None:
        # All 10 subgraphs of this page are done; bar sits just under
        # the next page's boundary until DB shows page advanced.
        return min(_MAX_PCT_WHILE_RUNNING, base_pct_from_pages + page_weight)

    sg_cfg = SUBGRAPH_TRACKING[current_sg]
    sg_absolute_weight = (sg_cfg["weight"] / 100.0) * page_weight
    estimated = max(1, sg_cfg["estimated_seconds"])
    ratio = min(1.0, elapsed_in_current_sg / estimated)
    if ratio >= 1.0:
        # Estimate exhausted — soft-cap at 95% of this subgraph's weight
        # so the bar can keep nudging up by a hair as we wait for the
        # DB write that says it's actually finished.
        interpolated = sg_absolute_weight * 0.95
    else:
        interpolated = ratio * sg_absolute_weight

    return min(
        _MAX_PCT_WHILE_RUNNING,
        base_pct_from_pages + completed_pct_in_page + interpolated,
    )
