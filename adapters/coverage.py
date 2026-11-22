"""Coverage instrumentation for the DSL runner.

Tracks two sets per adapter run:

  seen_paths      — every (record_type, dotted-field-path) tuple observed in the
                    INPUT data, with frequency counts. Walks every record, every
                    container, every block; flattens to dotted paths.

  consumed_paths  — every (record_type, dotted-field-path) tuple referenced by an
                    emit-rule expression in the DSL. Static analysis of the YAML
                    via regex matching against the context-variable surface
                    (record, record_first, container, container_first, block,
                    state, adapter, constants, trace).

The diff `seen_paths - consumed_paths` per record type is the **unmapped surface**
for the adapter — fields the input contains that the DSL does not read. This is
the early-warning signal for source-format drift: when a producer ships a new
field, it appears in seen but not consumed, even before any rule fails.

Usage:
    python3 dsl_runner.py adapter.dsl.yaml input.json --output traces.json \\
        --coverage coverage.json --operator-type scripted
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

# Context variables that DSL expressions can reference. Anchored to the §8.4
# inventory in the DSL spec draft-2.
CONTEXT_VARS = {
    "record", "record_first",
    "container", "container_first", "container_index",
    "block",
    "state", "adapter", "constants", "trace",
}

# Regex extracts `<context-var>.<dotted.path>` references from any DSL string.
# Matches forms:
#   record.message.role
#   $record.message.usage.input_tokens
#   ${record.timestamp}
#   container.subagents
#   $block.type
# Bare context-var references (no dotted path) also match with empty path.
_REF_RE = re.compile(
    r"\$?\b(" + "|".join(sorted(CONTEXT_VARS, key=len, reverse=True)) + r")"
    r"(?:\.([A-Za-z_][\w.\[\]]*?))?"
    r"(?=[^\w.]|$)"
)


def walk_dict_paths(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Yield (dotted-path, value-kind) tuples for a nested dict.

    value-kind is one of: "scalar", "null", "list[<elem-kind>]", "dict".
    For lists, the shape indicator comes from the first element, but dict
    elements are descended across ALL elements (union), so a path present
    only in a later element is still seen. Duplicate
    (path, kind) tuples across elements are kept — callers count frequency.
    """
    out: list[tuple[str, str]] = []
    if obj is None:
        out.append((prefix, "null"))
    elif isinstance(obj, dict):
        if not obj:
            out.append((prefix, "dict-empty"))
        else:
            for k, v in obj.items():
                child = f"{prefix}.{k}" if prefix else k
                out.extend(walk_dict_paths(v, child))
    elif isinstance(obj, list):
        if not obj:
            out.append((prefix, "list-empty"))
        else:
            elem = obj[0]
            if isinstance(elem, dict):
                out.append((prefix, "list[dict]"))
            elif isinstance(elem, list):
                out.append((prefix, "list[list]"))
            else:
                out.append((prefix, f"list[{type(elem).__name__}]"))
            for e in obj:
                if isinstance(e, dict):
                    for k, v in e.items():
                        child = f"{prefix}[].{k}" if prefix else f"[].{k}"
                        out.extend(walk_dict_paths(v, child))
    else:
        out.append((prefix, "scalar"))
    return out


def collect_seen_paths(
    raw: list[dict] | list[list[dict]],
    type_field: str = "type",
) -> dict[str, dict[str, int]]:
    """Walk input records, flatten to (record-type, dotted-path) frequency map.

    Records lacking the discriminator field are bucketed under "_untyped".
    Container-level fields (when input is a list of containers) are bucketed
    under "_container".

    Returns {record_type: {dotted_path: count}}.
    """
    counts: dict[str, Counter] = defaultdict(Counter)

    def _record_iter(item, container_keys_emitted):
        if isinstance(item, list):
            # container-mode: this is a list of records inside a container
            for rec in item:
                _record_iter(rec, container_keys_emitted)
            return
        if not isinstance(item, dict):
            return
        rtype = item.get(type_field) or "_untyped"
        for path, kind in walk_dict_paths(item):
            counts[rtype][f"{path}<{kind}>"] += 1

    if not raw:
        return {}

    if isinstance(raw[0], list):
        # raw is list-of-containers; each container is a list of records
        for c in raw:
            _record_iter(c, True)
    elif isinstance(raw[0], dict):
        # raw is list-of-records OR list-of-containers (single root form)
        # Heuristic: if the first element has the type_field, treat as records;
        # otherwise treat as containers and look inside.
        if type_field in raw[0]:
            for r in raw:
                _record_iter(r, False)
        else:
            # Treat as containers; scan their primary record arrays heuristically.
            for c in raw:
                if not isinstance(c, dict):
                    continue
                # Surface container-level keys as the "_container" type
                for path, kind in walk_dict_paths(c):
                    counts["_container"][f"{path}<{kind}>"] += 1
                # If the container has a list-of-dict member, recurse into it
                for v in c.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        for elem in v:
                            _record_iter(elem, True)

    return {rtype: dict(c.most_common()) for rtype, c in counts.items()}


def collect_consumed_paths(dsl: dict) -> dict[str, list[str]]:
    """Walk every string-valued node in the DSL and extract context-var.path
    references. Returns {context_var: sorted_list_of_dotted_paths}.

    Bare references (just the context var, no dotted suffix) record as the
    empty path "".

    Two consumption channels:
      1. Expression references in string values, e.g. `$record.foo.bar` or
         `${block.id}` — caught by `_REF_RE`.
      2. Predicate dispatch via DSL `field:` / `block_field:` / `container_field:`
         keys, e.g. `{ field: event_kind, value: user_prompt }` consumes
         `record.event_kind` even though no expression syntax is used.

    Without channel (2) the analyzer over-reports unmapped paths for fields
    that are used purely for predicate dispatch (event_kind, attachment_subtype,
    subtype, kind, etc.) — the ~35 "housekeeping false positives" surfaced by
    Phase 2 review.
    """
    refs: dict[str, set[str]] = defaultdict(set)

    # Map predicate key → context-var bucket the path is consumed against.
    PREDICATE_KEYS = {
        "field": "record",
        "block_field": "block",
        "container_field": "container",
    }

    def _scan(obj):
        if isinstance(obj, str):
            # A2(ii): optional-chaining `?.` is how the DSL accesses optional /
            # uncertain fields — exactly the ones most likely to drift. `_REF_RE`'s
            # path class excludes `?`, so `$record.message?.usage?.input` was captured
            # only as far as `message`, swallowing the whole `message.*` subtree as
            # consumed. Normalise `?.`→`.` first (mirrors the runner's `_normalise`).
            norm = obj.replace("?.", ".")
            for m in _REF_RE.finditer(norm):
                var = m.group(1)
                path = m.group(2) or ""
                refs[var].add(path)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                # Predicate-key consumption: the value is the dotted-path
                # being matched against the named context var.
                if k in PREDICATE_KEYS and isinstance(v, str):
                    refs[PREDICATE_KEYS[k]].add(v)
                _scan(v)
        elif isinstance(obj, list):
            for v in obj:
                _scan(v)

    _scan(dsl)
    return {k: sorted(v) for k, v in refs.items()}


def _strip_path_kind(path_with_kind: str) -> str:
    """Strip the `<kind>` suffix from a seen-path entry."""
    if "<" in path_with_kind:
        return path_with_kind.rsplit("<", 1)[0]
    return path_with_kind


# A canonical non-negative integer, as dsl_runner._LIST_INDEX: record_path_get
# reads such a segment at a list as an index and at an object as a key.
_LIST_INDEX = re.compile(r"0|[1-9][0-9]*")


def _record_path_spellings(record_path: str) -> set[str]:
    """The seen-path spellings a record_path can consume. Seen
    paths spell a list step as `[]` (`a[].events`), and which of a canonical
    index segment's two readings applies depends on the input, so each such
    segment yields both the `[]` spelling and the literal key, in every
    combination (ruled 2026-10-18)."""
    spellings = [""]
    for seg in record_path.split("."):
        options = [seg, "[]"] if _LIST_INDEX.fullmatch(seg) else [seg]
        spellings = [
            s + o if o == "[]" else (f"{s}.{o}" if s else o)
            for s in spellings for o in options
        ]
    return set(spellings)


def compute_unmapped(
    seen: dict[str, dict[str, int]],
    consumed_by_var: dict[str, list[str]],
    container_record_path: str | None = None,
) -> dict[str, list[str]]:
    """For each record-type in `seen`, return the list of dotted paths that
    are NOT in the consumed set for the relevant context-variable bucket.

    Buckets:
      - "_container" → checked against `consumed_by_var["container"]` plus the
        `input.container.record_path` config value (which is implicitly consumed).
      - other record-types → checked against `consumed_by_var["record"]`.

    The match is **prefix-or-equal**: a consumed path `message.content` matches
    seen paths `message.content`, `message.content[].type`, etc. — accessing
    `record.message.content` is treated as consuming the entire sub-tree.
    Conservative; may over-report consumption but avoids false-positive
    unmapped entries for fields whose sub-structure the DSL doesn't enumerate.
    """
    unmapped: dict[str, list[str]] = {}

    def _make_checker(consumed: set[str]) -> Any:
        def _is_consumed(path: str) -> bool:
            # Empty path means "the var was referenced as a whole" → consumes
            # everything.
            if "" in consumed:
                return True
            for cp in consumed:
                if not cp:
                    continue
                if path == cp or path.startswith(cp + ".") or path.startswith(cp + "["):
                    return True
            return False
        return _is_consumed

    record_consumed = set(consumed_by_var.get("record", []))
    container_consumed = set(consumed_by_var.get("container", []))
    if container_record_path:
        # The `record_path` in `input.container` is an implicit consumption of
        # the container-level field carrying the inner records. Treat its
        # dotted-path as consumed for the container bucket.
        container_consumed |= _record_path_spellings(container_record_path)
    block_consumed = set(consumed_by_var.get("block", []))

    record_check = _make_checker(record_consumed)
    container_check = _make_checker(container_consumed)
    block_check = _make_checker(block_consumed)

    for rtype, paths in seen.items():
        if rtype == "_container":
            check = container_check
        elif rtype == "_block":
            check = block_check
        else:
            check = record_check
        unmapped_paths = sorted(
            _strip_path_kind(p) for p in paths if not check(_strip_path_kind(p))
        )
        if unmapped_paths:
            unmapped[rtype] = unmapped_paths
    return unmapped


def compute_consumed_but_absent(
    seen: dict[str, dict[str, int]],
    consumed_by_var: dict[str, list[str]],
) -> list[str]:
    """A2(iii): the reverse direction — DSL references to fields the corpus does
    NOT provide ("read but never present"). `compute_unmapped` only computes
    `seen - consumed`; this computes `consumed - seen` for the record bucket.

    This is the direct detector for A1-class bugs: a field-name drift like the DSL
    reading `message.usage.inputTokens` while the corpus carries `message.usage.input`
    surfaces here immediately, instead of silently accumulating null→0.

    A consumed path is "present" if it is compatible with any seen record path:
    equal, an ancestor of a seen path (referenced a parent wholesale), or a
    descendant of a seen path. Fully-disjoint consumed paths are flagged.
    """
    seen_record: set[str] = set()
    for rtype, paths in seen.items():
        if rtype in ("_container", "_block"):
            continue
        for p in paths:
            seen_record.add(_strip_path_kind(p))

    if not seen_record:
        return []  # nothing observed (e.g. container-only walk); skip reverse check

    absent: list[str] = []
    for cp in consumed_by_var.get("record", []):
        if not cp:
            continue
        present = any(
            sp == cp
            or sp.startswith(cp + ".") or sp.startswith(cp + "[")
            or cp.startswith(sp + ".") or cp.startswith(sp + "[")
            for sp in seen_record
        )
        if not present:
            absent.append(cp)
    return sorted(absent)


def build_coverage_report(
    raw: list,
    dsl: dict,
    input_path: str,
    adapter_path: str,
    output_count: int,
    type_field: str = "type",
) -> dict:
    """Assemble the coverage report dict ready for JSON serialisation.

    Top-level shape:
      {
        "input_path": str,
        "adapter_path": str,
        "type_field": str,
        "input_summary": {container_count, record_count_total, record_count_by_type},
        "output_summary": {trace_count},
        "seen_paths_by_type": {rtype: {dotted_path<kind>: count}},
        "consumed_paths_by_context_var": {context_var: [paths]},
        "unmapped_paths_by_type": {rtype: [paths]},
      }
    """
    seen = collect_seen_paths(raw, type_field=type_field)
    consumed = collect_consumed_paths(dsl)
    input_cfg = dsl.get("input") if isinstance(dsl.get("input"), dict) else None
    container_cfg = input_cfg.get("container") if isinstance(input_cfg, dict) else None
    container_record_path: str | None = None
    if isinstance(container_cfg, dict):
        rp = container_cfg.get("record_path")
        if isinstance(rp, str):
            container_record_path = rp
    unmapped = compute_unmapped(seen, consumed, container_record_path=container_record_path)
    consumed_but_absent = compute_consumed_but_absent(seen, consumed)

    # Input summary
    container_count = 0
    record_count_total = 0
    record_count_by_type: Counter = Counter()
    if raw:
        if isinstance(raw[0], list):
            container_count = len(raw)
            for c in raw:
                for rec in c:
                    record_count_total += 1
                    if isinstance(rec, dict):
                        rt = rec.get(type_field) or "_untyped"
                        record_count_by_type[rt] += 1
        elif isinstance(raw[0], dict):
            if type_field in raw[0]:
                container_count = 1
                for r in raw:
                    record_count_total += 1
                    if isinstance(r, dict):
                        rt = r.get(type_field) or "_untyped"
                        record_count_by_type[rt] += 1
            else:
                container_count = len(raw)
                # Records nested in container; counted via seen walk.
                for rt in seen:
                    if rt != "_container":
                        record_count_by_type[rt] = sum(seen[rt].values()) // max(
                            1, sum(1 for p in seen[rt] if p.startswith("type<"))
                        )

    return {
        # Basename only: the invocation path is absolute and leaks the operator's
        # home directory and session directory into a shippable artefact. Nothing
        # reads this field; it identifies which input the report covers.
        "input_path": input_path.rsplit("/", 1)[-1],
        # Last two components only (e.g. "pi/0.58.1.dsl.yaml"): an absolute
        # path would carry the operator's home and checkout layout.
        "adapter_path": "/".join(str(adapter_path).replace("\\", "/").split("/")[-2:]),
        "type_field": type_field,
        "input_summary": {
            "container_count": container_count,
            "record_count_total": record_count_total,
            "record_count_by_type": dict(record_count_by_type.most_common()),
        },
        "output_summary": {
            "trace_count": output_count,
        },
        "seen_paths_by_type": seen,
        "consumed_paths_by_context_var": consumed,
        "unmapped_paths_by_type": unmapped,
        "consumed_but_absent_in_record": consumed_but_absent,
    }


def write_coverage_report(report: dict, path: str) -> None:
    """Serialise the coverage report to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        f.write("\n")
