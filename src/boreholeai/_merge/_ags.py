"""AGS4 file parsing, merging, and writing.

Mirrors the TypeScript implementation in
`frontend/src/lib/ags-merge.ts` (post CSV-parser fix).
Any rule change here must also land in the TS source.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Singletons must have exactly 1 DATA row — taken from one deterministic source.
SINGLETON_GROUPS = ("PROJ", "TRAN")

# AGS 4.1.1 AU dictionary headings preceding the remark field of each group.
REMARK_PRECEDING_HEADINGS = {
    "LOCA": ("LOCA_REM", frozenset({"LOCA_ID", "LOCA_TYPE", "LOCA_STAT", "LOCA_NATE", "LOCA_NATN", "LOCA_GREF", "LOCA_GL"})),
    "PROJ": ("PROJ_MEMO", frozenset({"PROJ_ID", "PROJ_NAME", "PROJ_LOC", "PROJ_CLNT", "PROJ_CONT", "PROJ_ENG"})),
}

# Metadata groups — union by full row content, deduplicate.
META_GROUPS = frozenset({"UNIT", "TYPE", "ABBR", "DICT"})

# Output order: these named groups come first (in this order), then everything else.
OUTPUT_ORDER = ("PROJ", "TRAN", "UNIT", "TYPE", "ABBR", "DICT")


class AgsMergeConflict(ValueError):
    """Source AGS schemas cannot be merged without changing meaning."""


@dataclass(frozen=True)
class AgsSource:
    """Actual source document metadata used to identify conflicting variants."""

    source_file: str
    job_id: str = ""


@dataclass
class AgsGroup:
    name: str
    headings: list[str] = field(default_factory=list)
    unit: list[str] = field(default_factory=list)
    type: list[str] = field(default_factory=list)
    data: list[list[str]] = field(default_factory=list)


def merge_ags_files(
    paths: Iterable[Path], *, sources: Iterable[AgsSource] | None = None,
    location_mapping: list[dict] | None = None,
) -> str:
    """Merge multiple AGS4 files into a single AGS4 string.

    Identical complete borehole datasets are retained once. Differing datasets
    sharing an identifier receive stable source-variant identifiers. Singleton
    transmission metadata retains the last input, within one declared project.
    When supplied, location_mapping receives source-to-output identities only
    after the entire merge and serialization succeed.
    """
    paths = list(paths)
    if not paths:
        raise ValueError("merge_ags_files requires at least one path")

    all_files: list[list[AgsGroup]] = []
    for p in paths:
        text = Path(p).read_text(encoding="utf-8")
        all_files.append(_parse_ags(text))

    source_metadata = list(sources) if sources is not None else [
        AgsSource(source_file=Path(path).name) for path in paths
    ]
    if len(source_metadata) != len(paths):
        raise ValueError("sources must contain one AgsSource per AGS path")
    _preflight_sources(all_files)
    mappings = _preserve_source_variants(all_files, source_metadata)

    merged: dict[str, AgsGroup] = {}
    _merge_singletons(all_files, source_metadata, merged)

    for groups in all_files:
        for group in groups:
            if group.name in SINGLETON_GROUPS:
                continue
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

            if group.name in META_GROUPS:
                _align_headings(existing, group)
                seen = {tuple(r) for r in existing.data}
                for row in group.data:
                    padded = _align_row(row, group.headings, existing.headings)
                    key = tuple(padded)
                    if key not in seen:
                        existing.data.append(padded)
                        seen.add(key)
                continue

            # Whole-borehole duplicate suppression has already run across all groups.
            _align_headings(existing, group)
            existing.data.extend(
                _align_row(row, group.headings, existing.headings)
                for row in group.data
            )

    _validate_strength_links(merged)
    _validate_location_links(merged)
    output = _write_ags(merged)
    if location_mapping is not None:
        location_mapping.extend(mappings)
    return output




# -------------------------------------------
# Internal Helper Functions
# -------------------------------------------

def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ordinal_key(value: str) -> bytes:
    """Match JavaScript ordinal string sorting, including supplementary Unicode."""
    return value.encode("utf-16-be", errors="surrogatepass")


def _preflight_sources(all_files: list[list[AgsGroup]]) -> None:
    """Check every original input before any duplicate dataset is suppressed."""
    schemas: dict[str, AgsGroup] = {}
    projects: set[str] = set()
    ags_versions: set[str] = set()
    link_settings: set[tuple[str, str]] = set()
    for groups in all_files:
        by_name: dict[str, AgsGroup] = {}
        for group in groups:
            if group.name in by_name:
                raise AgsMergeConflict(f"AGS {group.name}: repeated GROUP in source; retain original files")
            by_name[group.name] = group
            if len(set(group.headings)) != len(group.headings):
                raise AgsMergeConflict(f"AGS {group.name}: duplicate heading")
            if any(len(row) > len(group.headings) for row in [group.unit, group.type, *group.data]):
                raise AgsMergeConflict(f"AGS {group.name}: row exceeds heading count; retain original files")
            if group.name in schemas:
                _align_headings(schemas[group.name], group)
            else:
                schemas[group.name] = AgsGroup(
                    group.name, list(group.headings), list(group.unit), list(group.type)
                )
        _validate_strength_links(by_name)
        _validate_location_links(by_name)
        link_settings.add(_record_link_settings(groups))
        for group_name, heading, identities in (
            ("PROJ", "PROJ_ID", projects), ("TRAN", "TRAN_AGS", ags_versions)
        ):
            group = by_name.get(group_name)
            if group is None or heading not in group.headings or not group.data:
                identities.add("")
            else:
                idx = group.headings.index(heading)
                identities.update(row[idx] if idx < len(row) else "" for row in group.data)
    if len(projects) > 1:
        raise AgsMergeConflict("AGS PROJ_ID: different declared projects; retain original files")
    if len(ags_versions) > 1:
        raise AgsMergeConflict("AGS TRAN_AGS: incompatible dictionary versions; retain original files")
    if len(link_settings) > 1:
        raise AgsMergeConflict("AGS TRAN_DLIM/TRAN_RCON: incompatible record-link separators; retain original files")


def _validate_location_links(groups: dict[str, AgsGroup]) -> None:
    loca = groups.get("LOCA")
    if loca is None:
        return
    ids = set()
    if loca is not None and "LOCA_ID" in loca.headings:
        idx = loca.headings.index("LOCA_ID")
        ids = {row[idx] if idx < len(row) else "" for row in loca.data}
    for group in groups.values():
        if group.name == "LOCA" or "LOCA_ID" not in group.headings:
            continue
        idx = group.headings.index("LOCA_ID")
        for row in group.data:
            loca_id = row[idx] if idx < len(row) else ""
            if loca_id not in ids:
                raise AgsMergeConflict(f"AGS {group.name}: missing parent location {loca_id}; retain original files")


def _record_link_settings(groups: list[AgsGroup]) -> tuple[str, str]:
    settings = {"TRAN_DLIM": "|", "TRAN_RCON": "+"}
    tran = next((group for group in groups if group.name == "TRAN"), None)
    if tran is not None:
        for heading in settings:
            if heading not in tran.headings:
                continue
            idx = tran.headings.index(heading)
            declared = {row[idx] for row in tran.data if idx < len(row) and row[idx]}
            if len(declared) > 1:
                raise AgsMergeConflict(f"AGS {heading}: conflicting record-link separators; retain original files")
            if declared:
                settings[heading] = declared.pop()
    delimiter, concatenator = settings["TRAN_DLIM"], settings["TRAN_RCON"]
    if delimiter == concatenator or any(
        len(value) != 1 or not value.isascii() or value.isspace() or value.isalnum()
        for value in (delimiter, concatenator)
    ):
        raise AgsMergeConflict("AGS TRAN_DLIM/TRAN_RCON: invalid record-link separators; retain original files")
    return delimiter, concatenator


def _remap_record_links(
    all_files: list[list[AgsGroup]],
    renames: list[dict[str, tuple[str, str]]],
    variants: dict[str, dict[str, list[int]]],
) -> None:
    for source_idx, groups in enumerate(all_files):
        if not renames[source_idx]:
            continue
        by_name = {group.name: group for group in groups}
        delimiter, concatenator = _record_link_settings(groups)
        for group in groups:
            for idx, descriptor in enumerate(group.type):
                if descriptor != "RL":
                    continue
                for row in group.data:
                    if idx >= len(row) or not row[idx]:
                        continue
                    links = row[idx].split(concatenator)
                    rewritten = []
                    for link in links:
                        parts = link.split(delimiter)
                        target = by_name.get(parts[0])
                        if len(parts) < 2 or not parts[0] or not parts[1] or target is None:
                            raise AgsMergeConflict(f"AGS {group.name}.{group.headings[idx]}: malformed or unresolved record link; retain original files")
                        if "LOCA_ID" in target.headings:
                            if target.headings[0] != "LOCA_ID":
                                raise AgsMergeConflict(f"AGS {target.name}: record-link location key is not first; retain original files")
                            if not any(target_row and target_row[0] == parts[1] for target_row in target.data):
                                raise AgsMergeConflict(f"AGS {group.name}.{group.headings[idx]}: missing record-link location; retain original files")
                            replacement = renames[source_idx].get(parts[1])
                            if replacement is not None:
                                if delimiter in replacement[0] or concatenator in replacement[0]:
                                    raise AgsMergeConflict("AGS source-variant identifier contains a record-link separator; retain original files")
                                parts[1] = replacement[0]
                        rewritten.append(delimiter.join(parts))
                    row[idx] = concatenator.join(rewritten)
    # Identical referring rows may point to different source variants after remapping.
    # Keep the originals for explicit reconciliation rather than deduplicating a link.
    for loca_id, datasets in variants.items():
        for indexes in datasets.values():
            if len(indexes) > 1 and len({_borehole_fingerprint(all_files[idx], loca_id) for idx in indexes}) > 1:
                raise AgsMergeConflict(f"AGS {loca_id}: identical source data references different location variants; retain original files")


def _borehole_fingerprint(groups: list[AgsGroup], loca_id: str) -> str:
    dataset: list[list[object]] = []
    for group in sorted(groups, key=lambda g: _ordinal_key(g.name)):
        if "LOCA_ID" not in group.headings:
            continue
        idx = group.headings.index("LOCA_ID")
        rows = [row for row in group.data if (row[idx] if idx < len(row) else "") == loca_id]
        if not rows:
            continue
        headings = sorted(group.headings, key=_ordinal_key)
        aligned_rows = [_align_row(row, group.headings, headings) for row in rows]
        aligned_rows.sort(key=lambda row: _ordinal_key(_canonical_json(row)))
        dataset.append([
            group.name, headings,
            _align_row(group.unit, group.headings, headings),
            _align_row(group.type, group.headings, headings), aligned_rows,
        ])
    return _canonical_json(dataset)


def _ensure_remark_heading(group: AgsGroup) -> int:
    """Return the remark column of a group, inserting it in dictionary order when absent."""
    heading, before = REMARK_PRECEDING_HEADINGS[group.name]
    if heading in group.headings:
        return group.headings.index(heading)
    idx = next((i for i, existing in enumerate(group.headings) if existing not in before), len(group.headings))
    if any(existing in before for existing in group.headings[idx:]):
        raise AgsMergeConflict(f"AGS {group.name}: cannot place {heading} in dictionary order; retain original files")
    old_length = len(group.headings)
    group.unit = _pad_row(group.unit, old_length)
    group.type = _pad_row(group.type, old_length)
    group.data = [_pad_row(row, old_length) for row in group.data]
    group.headings.insert(idx, heading)
    group.unit.insert(idx, "")
    group.type.insert(idx, "X")
    for row in group.data:
        row.insert(idx, "")
    return idx


def _append_remark(group: AgsGroup, row: list[str], remark_idx: int, remark: str) -> None:
    row[:] = _pad_row(row, len(group.headings))
    row[remark_idx] = (row[remark_idx] + "; " if row[remark_idx] else "") + remark


def _merge_singletons(all_files: list[list[AgsGroup]], sources: list[AgsSource], merged: dict[str, AgsGroup]) -> None:
    """PROJ and TRAN come from one deterministic source (smallest source pair), so the
    output does not depend on selection order. Differing PROJ values are recorded in
    PROJ_MEMO with their sources rather than chosen silently."""
    def pair_key(idx: int) -> bytes:
        return _ordinal_key(_canonical_json([sources[idx].source_file, sources[idx].job_id]))

    for name in SINGLETON_GROUPS:
        owners = [(idx, group) for idx, groups in enumerate(all_files) for group in groups if group.name == name]
        owners.sort(key=lambda owner: pair_key(owner[0]))
        if not owners:
            continue
        representative = owners[0][1]
        clone = AgsGroup(
            name, list(representative.headings), list(representative.unit), list(representative.type),
            [list(row) for row in representative.data],
        )
        merged[name] = clone
        if name != "PROJ" or not clone.data:
            continue
        notes: list[str] = []
        for heading in sorted({h for _, group in owners for h in group.headings}, key=_ordinal_key):
            if heading == "PROJ_ID":
                continue
            values = []
            for idx, group in owners:
                column = group.headings.index(heading) if heading in group.headings else -1
                row = group.data[0] if group.data else []
                values.append([row[column] if 0 <= column < len(row) else "", sources[idx].source_file, sources[idx].job_id])
            if len({value[0] for value in values}) > 1:
                values.sort(key=lambda value: _ordinal_key(_canonical_json(value)))
                notes.append(f"{heading} differs: " + json.dumps(values, ensure_ascii=True, separators=(",", ":")))
        if notes:
            # Resolve the column first: inserting it may replace short rows with padded copies.
            memo_idx = _ensure_remark_heading(clone)
            _append_remark(clone, clone.data[0], memo_idx, "AGS source variants; " + "; ".join(notes))


def _preserve_source_variants(all_files: list[list[AgsGroup]], sources: list[AgsSource]) -> list[dict]:
    """Compare complete boreholes, then rename variants consistently in every group."""
    variants: dict[str, dict[str, list[int]]] = {}
    for source_idx, groups in enumerate(all_files):
        ids: set[str] = set()
        for group in groups:
            if "LOCA_ID" in group.headings:
                idx = group.headings.index("LOCA_ID")
                ids.update(row[idx] if idx < len(row) else "" for row in group.data)
        for loca_id in ids:
            fingerprint = _borehole_fingerprint(groups, loca_id)
            variants.setdefault(loca_id, {}).setdefault(fingerprint, []).append(source_idx)

    reserved = set(variants)
    keep: list[set[str]] = [set() for _ in all_files]
    renames: list[dict[str, tuple[str, str]]] = [{} for _ in all_files]

    for loca_id in sorted(variants, key=_ordinal_key):
        datasets = variants[loca_id]
        for fingerprint in sorted(datasets, key=_ordinal_key):
            indexes = datasets[fingerprint]
            representative = min(indexes, key=lambda idx: _ordinal_key(_canonical_json([sources[idx].source_file, sources[idx].job_id])))
            keep[representative].add(loca_id)
            if len(datasets) == 1:
                continue
            loca_group = next((group for group in all_files[representative] if group.name == "LOCA"), None)
            if loca_group is None or "LOCA_ID" not in loca_group.headings or not any(
                row[loca_group.headings.index("LOCA_ID")] == loca_id
                for row in loca_group.data if len(row) > loca_group.headings.index("LOCA_ID")
            ):
                raise AgsMergeConflict(f"AGS {loca_id}: source variant is missing LOCA row for provenance; retain original files")
            source = sources[representative]
            stem = source.source_file.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")[:24]
            digest = hashlib.sha256(_canonical_json([source.source_file, source.job_id, fingerprint]).encode("utf-8")).hexdigest()
            base = f"{loca_id}__{slug}_"
            width = 8
            candidate = base + digest[:width]
            while candidate in reserved and width < len(digest):
                width += 4
                candidate = base + digest[:width]
            suffix = 2
            while candidate in reserved:
                candidate = base + digest + f"_{suffix}"
                suffix += 1
            reserved.add(candidate)
            provenance = sorted({(sources[idx].source_file, sources[idx].job_id) for idx in indexes}, key=lambda pair: _ordinal_key(_canonical_json(pair)))
            remark = (
                "AGS source variant; physical identity not established; original LOCA_ID="
                + json.dumps(loca_id, ensure_ascii=True, separators=(",", ":"))
                + "; sources=" + json.dumps(provenance, ensure_ascii=True, separators=(",", ":"))
            )
            for idx in indexes:
                renames[idx][loca_id] = (candidate, remark)

    _remap_record_links(all_files, renames, variants)
    for source_idx, groups in enumerate(all_files):
        for group in groups:
            if "LOCA_ID" not in group.headings:
                continue
            loca_idx = group.headings.index("LOCA_ID")
            group.data = [row for row in group.data if (row[loca_idx] if loca_idx < len(row) else "") in keep[source_idx]]
            rem_idx = _ensure_remark_heading(group) if group.name == "LOCA" and renames[source_idx] else None
            for row in group.data:
                old_id = row[loca_idx] if loca_idx < len(row) else ""
                renamed = renames[source_idx].get(old_id)
                if renamed is None:
                    continue
                row[loca_idx] = renamed[0]
                if rem_idx is not None:
                    _append_remark(group, row, rem_idx, renamed[1])


    mappings: list[dict] = []
    for source_idx, source in enumerate(sources):
        for old_id in sorted(variants, key=_ordinal_key):
            if not any(source_idx in indexes for indexes in variants[old_id].values()):
                continue
            renamed = renames[source_idx].get(old_id)
            disposition = (
                "deduplicated" if old_id not in keep[source_idx]
                else "preserved_variant" if renamed is not None
                else "unchanged"
            )
            mappings.append({
                "source_file": source.source_file,
                "source_job_id": source.job_id,
                "original_loca_id": old_id,
                "merged_loca_id": renamed[0] if renamed is not None else old_id,
                "disposition": disposition,
            })
    return mappings


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
    """Align by name; reject metadata conflicts and indeterminate dictionary order."""
    headings = list(dict.fromkeys(existing.headings + incoming.headings))
    edges = {heading: set() for heading in headings}
    for source in (existing.headings, incoming.headings):
        if len(set(source)) != len(source):
            raise AgsMergeConflict(f"AGS {existing.name}: duplicate heading")
        for previous, following in zip(source, source[1:]):
            edges[previous].add(following)
    ordered: list[str] = []
    remaining = set(headings)
    while remaining:
        candidates = [h for h in remaining if not any(h in edges[other] for other in remaining)]
        if len(candidates) != 1:
            raise AgsMergeConflict(f"AGS {existing.name}: ambiguous or conflicting heading order; retain original files")
        ordered.append(candidates[0])
        remaining.remove(candidates[0])
    for heading in existing.headings:
        if heading not in incoming.headings:
            continue
        i, j = existing.headings.index(heading), incoming.headings.index(heading)
        for descriptor in ("unit", "type"):
            old, new = getattr(existing, descriptor), getattr(incoming, descriptor)
            before, after = old[i] if i < len(old) else "", new[j] if j < len(new) else ""
            if before != after:
                raise AgsMergeConflict(
                    f"AGS {existing.name}.{heading}: {descriptor.upper()} conflict "
                    f"({before or 'unknown'} vs {after or 'unknown'}); retain original files"
                )
    original = existing.headings
    for descriptor in ("unit", "type"):
        old = dict(zip(original, getattr(existing, descriptor)))
        new = dict(zip(incoming.headings, getattr(incoming, descriptor)))
        setattr(existing, descriptor, [old.get(h, "") if h in original else new.get(h, "") for h in ordered])
    existing.data = [_align_row(row, original, ordered) for row in existing.data]
    existing.headings = ordered


def _align_row(row: list[str], source: list[str], target: list[str]) -> list[str]:
    values = dict(zip(source, row))
    return [values.get(heading, "") for heading in target]


def _pad_row(row: list[str], length: int) -> list[str]:
    """Pad a row to `length` with empty strings; never truncate."""
    if len(row) >= length:
        return row
    return row + [""] * (length - len(row))


def _validate_strength_links(groups: dict[str, AgsGroup]) -> None:
    """Reject key collisions/orphan samples rather than reconciling report revisions."""
    sample_key = ["LOCA_ID", "SAMP_TOP", "SAMP_REF", "SAMP_TYPE", "SAMP_ID"]
    keys = {
        "LOCA": ["LOCA_ID"],
        "UNIT": ["UNIT_UNIT"],
        "TYPE": ["TYPE_TYPE"],
        "ABBR": ["ABBR_HDNG", "ABBR_CODE"],
        "DICT": ["DICT_TYPE", "DICT_GRP", "DICT_HDNG"],
        "SAMP": sample_key,
        "RPLT": sample_key + ["SPEC_REF", "SPEC_DPTH"],
        "RUCS": sample_key + ["SPEC_REF", "SPEC_DPTH"],
        "IPEN": ["LOCA_ID", "IPEN_DPTH", "IPEN_TESN"],
        "IVAN": ["LOCA_ID", "IVAN_DPTH", "IVAN_TESN"],
    }
    for name, columns in keys.items():
        group = groups.get(name)
        if group is None or not all(h in group.headings for h in columns):
            continue
        seen = set()
        for row in group.data:
            key = tuple(_align_row(row, group.headings, columns))
            if key in seen:
                raise AgsMergeConflict(f"AGS {name}: duplicate test/sample key {key}; report revisions require reconciliation; retain original files")
            seen.add(key)
    samples = groups.get("SAMP")
    for name in ("RPLT", "RUCS"):
        group = groups.get(name)
        if group is None or not all(h in group.headings for h in sample_key):
            continue
        parents = {tuple(_align_row(row, samples.headings, sample_key)) for row in samples.data} if samples else set()
        for row in group.data:
            key = tuple(_align_row(row, group.headings, sample_key))
            if key not in parents:
                raise AgsMergeConflict(f"AGS {name}: missing parent sample {key}; report revisions require reconciliation; retain original files")


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
