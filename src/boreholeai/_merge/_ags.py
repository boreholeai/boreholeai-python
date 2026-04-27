"""AGS4 file parsing, merging, and writing.

Mirrors the TypeScript implementation in
`frontend/src/app/api/jobs/download/route.ts` (post CSV-parser fix).
Any rule change here must also land in the TS source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Singletons must have exactly 1 DATA row — keep latest (last file wins).
SINGLETON_GROUPS = frozenset({"PROJ", "TRAN"})

# Metadata groups — union by full row content, deduplicate.
META_GROUPS = frozenset({"UNIT", "TYPE", "ABBR", "DICT"})

# Output order: these named groups come first (in this order), then everything else.
OUTPUT_ORDER = ("PROJ", "TRAN", "UNIT", "TYPE", "ABBR", "DICT")


@dataclass
class AgsGroup:
    name: str
    headings: list[str] = field(default_factory=list)
    unit: list[str] = field(default_factory=list)
    type: list[str] = field(default_factory=list)
    data: list[list[str]] = field(default_factory=list)


def merge_ags_files(paths: Iterable[Path]) -> str:
    """Merge multiple AGS4 files into a single AGS4 string.

    Order matters — last file wins for SINGLETON_GROUPS and for data-group
    LOCA_ID dedup. The caller controls the order.
    """
    paths = list(paths)
    if not paths:
        raise ValueError("merge_ags_files requires at least one path")

    all_files: list[list[AgsGroup]] = []
    for p in paths:
        text = Path(p).read_text(encoding="utf-8")
        all_files.append(_parse_ags(text))

    merged: dict[str, AgsGroup] = {}

    for groups in all_files:
        for group in groups:
            existing = merged.get(group.name)

            if existing is None:
                merged[group.name] = AgsGroup(
                    name=group.name,
                    headings=list(group.headings),
                    unit=list(group.unit),
                    type=list(group.type),
                    data=[list(row) for row in group.data],
                )
                continue

            if group.name in SINGLETON_GROUPS:
                existing.headings = list(group.headings)
                existing.unit = list(group.unit)
                existing.type = list(group.type)
                existing.data = [list(row) for row in group.data]
                continue

            if group.name in META_GROUPS:
                _align_headings(existing, group)
                seen = {tuple(r) for r in existing.data}
                for row in group.data:
                    padded = _pad_row(list(row), len(existing.headings))
                    key = tuple(padded)
                    if key not in seen:
                        existing.data.append(padded)
                        seen.add(key)
                continue

            # Data group: align, then dedup by LOCA_ID (latest wins)
            _align_headings(existing, group)
            try:
                loca_idx = existing.headings.index("LOCA_ID")
            except ValueError:
                loca_idx = -1

            if loca_idx == -1:
                for row in group.data:
                    existing.data.append(_pad_row(list(row), len(existing.headings)))
                continue

            incoming_by_loca: dict[str, list[list[str]]] = {}
            for row in group.data:
                padded = _pad_row(list(row), len(existing.headings))
                loca_id = padded[loca_idx] if loca_idx < len(padded) else ""
                incoming_by_loca.setdefault(loca_id, []).append(padded)

            removed = set(incoming_by_loca.keys())
            existing.data = [
                row for row in existing.data
                if (row[loca_idx] if loca_idx < len(row) else "") not in removed
            ]
            for rows in incoming_by_loca.values():
                existing.data.extend(rows)

    return _write_ags(merged)




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _parse_ags(text: str) -> list[AgsGroup]:
    """Parse an AGS4 file into a list of groups."""
    groups: list[AgsGroup] = []
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in normalised.split("\n\n") if b.strip()]

    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue

        first = _parse_ags_line(lines[0])
        if len(first) < 2 or first[0] != "GROUP" or not first[1]:
            continue

        group = AgsGroup(name=first[1])

        for line in lines[1:]:
            fields_ = _parse_ags_line(line)
            if not fields_:
                continue
            descriptor = fields_[0]
            values = fields_[1:]
            if descriptor == "HEADING":
                group.headings = list(values)
            elif descriptor == "UNIT":
                group.unit = list(values)
            elif descriptor == "TYPE":
                group.type = list(values)
            elif descriptor == "DATA":
                group.data.append(list(values))

        if group.headings:
            groups.append(group)

    return groups


def _parse_ags_line(line: str) -> list[str]:
    """Split one AGS4 CSV line into fields, respecting quoted strings.

    Mirrors the TypeScript `parseAgsLine` in route.ts. Commas inside
    `"..."` are literal; `""` inside a quoted field is an escaped `"`.
    """
    fields: list[str] = []
    current = ""
    in_quotes = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    current += '"'
                    i += 2
                    continue
                in_quotes = False
            else:
                current += ch
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            fields.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    fields.append(current.strip())
    return fields


def _align_headings(existing: AgsGroup, incoming: AgsGroup) -> None:
    """Expand `existing.headings` to include any new headings from `incoming`.

    Pads existing data rows with empty strings to match the new width.
    """
    for i, heading in enumerate(incoming.headings):
        if heading not in existing.headings:
            existing.headings.append(heading)
            existing.unit.append(incoming.unit[i] if i < len(incoming.unit) else "")
            existing.type.append(incoming.type[i] if i < len(incoming.type) else "")
            for row in existing.data:
                row.append("")


def _pad_row(row: list[str], length: int) -> list[str]:
    """Pad a row to `length` with empty strings; never truncate."""
    if len(row) >= length:
        return row
    return row + [""] * (length - len(row))


def _write_ags(groups: dict[str, AgsGroup]) -> str:
    """Write merged groups to an AGS4 format string.

    Output order: PROJ, TRAN, UNIT, TYPE, ABBR, DICT, then any others
    in insertion order. CRLF line endings; double-quoted fields; `""`
    escape for embedded quotes.
    """
    def q(v: str) -> str:
        return '"' + v.replace('"', '""') + '"'

    ordered: list[AgsGroup] = []
    for name in OUTPUT_ORDER:
        g = groups.get(name)
        if g is not None:
            ordered.append(g)
    seen_ordered = set(OUTPUT_ORDER)
    for name, g in groups.items():
        if name not in seen_ordered:
            ordered.append(g)

    blocks: list[str] = []
    for g in ordered:
        lines: list[str] = []
        lines.append(",".join([q("GROUP"), q(g.name)]))
        lines.append(",".join(q(v) for v in ["HEADING", *g.headings]))
        lines.append(",".join(q(v) for v in ["UNIT", *g.unit]))
        lines.append(",".join(q(v) for v in ["TYPE", *g.type]))
        for row in g.data:
            padded = _pad_row(list(row), len(g.headings))
            lines.append(",".join(q(v) for v in ["DATA", *padded]))
        blocks.append("\r\n".join(lines))

    return "\r\n\r\n".join(blocks) + "\r\n"
