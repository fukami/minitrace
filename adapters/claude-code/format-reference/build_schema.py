#!/usr/bin/env python3
"""Build a format-reference schema.json from a claude-code session JSONL.

The format-reference pins the native source shape at a given harness version so
adapter/DSL drift (Axis C) is measured against a known baseline. Each reference
is a pair: the session `.jsonl` and this derived `.schema.json` field inventory.

Reproducibility note: the original generator that produced the v2.1.76 (and the
goose) schema.json files was never checked in. This script re-establishes a
tracked, deterministic generator so a `latest`-image drift run can regenerate the
inventory. Its sample-selection heuristics are documented below; the STRUCTURE
matches the existing schema files (record_types / field_count / fields), but
edge-case sample_values may differ from the lost original — nothing consumes the
schema at runtime (it is reference documentation), so structural parity is what
matters.

Schema shape:
    {
      "record_types": { <type>: <count>, ... },   # top-level `type` field
      "field_count":  <int>,                        # distinct dotted paths
      "fields": {
        "<dotted.path>": {
          "types":         [ <python-type-name>, ... ],  # sorted, distinct
          "count":         <int>,                         # records with this path
          "sample_values": [ <str>, ... ]                 # sorted, distinct, capped
        }, ...
      }
    }

Dotted-path convention (matches coverage.py.walk_dict_paths and the v2.1.76 file):
list elements are addressed with a `[]` segment and the element's own type is
recorded at the bare list path (e.g. `message.content` -> ["list","str"],
`message.content[]` -> ["dict"]).

Sample-value heuristics:
  - scalars only (dict/list container nodes get an empty sample list);
  - value stringified with str() (so bools render "True"/"False", ints "42");
  - distinct, sorted, capped at SAMPLE_CAP;
  - strings longer than SAMPLE_MAXLEN are omitted (keeps large blobs/multiline
    tool bodies out of the reference).

Usage:
    python build_schema.py <session.jsonl> <out.schema.json>
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

SAMPLE_CAP = 10
SAMPLE_MAXLEN = 64
TYPE_FIELD = "type"


def _pytype(v: object) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _walk(obj: object, prefix: str, out: list[tuple[str, str, object]]) -> None:
    """Emit (path, python-type, value) for every node, mirroring the v2.1.76
    dotted-path/`[]` convention. Unions fields across ALL list elements — a
    claude-code `message.content` array mixes text / thinking / tool_use /
    tool_result blocks in different positions, so first-element-only sampling
    would drop most of the tool-call surface."""
    out.append((prefix, _pytype(obj), obj))
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{prefix}.{k}" if prefix else k
            _walk(v, child, out)
    elif isinstance(obj, list):
        child = f"{prefix}[]" if prefix else "[]"
        for elem in obj:
            if isinstance(elem, dict):
                out.append((child, "dict", elem))
                for k, v in elem.items():
                    _walk(v, f"{child}.{k}", out)
            else:
                out.append((child, _pytype(elem), elem))


def build_schema(records: list[dict]) -> dict:
    record_types: collections.Counter = collections.Counter()
    types_by_path: dict[str, set[str]] = collections.defaultdict(set)
    count_by_path: collections.Counter = collections.Counter()
    samples_by_path: dict[str, set[str]] = collections.defaultdict(set)

    for rec in records:
        if isinstance(rec, dict):
            record_types[rec.get(TYPE_FIELD, "_untyped")] += 1
        nodes: list[tuple[str, str, object]] = []
        _walk(rec, "", nodes)
        for path, ptype, value in nodes:
            if path == "":
                continue  # the record root itself
            types_by_path[path].add(ptype)
            count_by_path[path] += 1
            if not isinstance(value, (dict, list)) and value is not None:
                s = str(value)
                if len(s) <= SAMPLE_MAXLEN:
                    samples_by_path[path].add(s)

    fields = {}
    for path in sorted(types_by_path):
        fields[path] = {
            "types": sorted(types_by_path[path]),
            "count": count_by_path[path],
            "sample_values": sorted(samples_by_path[path])[:SAMPLE_CAP],
        }

    return {
        "record_types": dict(record_types.most_common()),
        "field_count": len(fields),
        "fields": fields,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    src, dst = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    records = [
        json.loads(line)
        for line in src.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    schema = build_schema(records)
    dst.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {dst.name}: {len(records)} records, "
        f"{len(schema['record_types'])} record-types, "
        f"{schema['field_count']} fields"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
