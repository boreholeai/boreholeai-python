"""Borehole_data.json merging.

Mirrors the TypeScript implementation `mergeJsonBuffers` in
`frontend/src/app/api/jobs/download/route.ts`. Any rule change here must
also land in the TS source.

The merge concatenates the record arrays under each section key
(ground_profile.*, test_data.*) across all per-job files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def merge_json_files(paths: Iterable[Path]) -> str:
    """Merge Borehole_data.json files into one JSON string (indent=2).

    An unparseable per-job file is skipped (logged) so the merge still
    produces output for the remaining jobs — mirrors the TS
    `mergeJsonBuffers` skip-and-continue behaviour and the AGS/Excel
    mergers, which degrade rather than abort the whole batch.
    """
    merged: dict[str, dict[str, list[object]]] = {}
    for path in paths:
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
                if isinstance(rows, list):
                    dest.setdefault(key, []).extend(rows)
    return json.dumps(merged, indent=2)
