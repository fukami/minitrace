#!/usr/bin/env python3
"""
minitrace format stability test — detects native format changes across framework versions.

Usage:
    # Generate reference schema from a known-good session
    python3 test-format-stability.py --extract session.jsonl > reference-schema.json

    # Test a new session against the reference
    python3 test-format-stability.py --check new-session.jsonl --reference reference-schema.json

    # Quick self-test: extract + check in one pass (reference is itself)
    python3 test-format-stability.py --self-test session.jsonl

    # Test all adapters against their format references
    python3 test-format-stability.py --all

The structural schema captures:
  - Record types and their frequency distribution
  - Field paths at every nesting level
  - Value types per field (str, int, float, bool, list, dict, null)
  - Enum-like values for fields with few distinct values

This does NOT check data correctness — only structural stability.
When a framework updates and changes its output format, this test catches it
before the adapter silently produces wrong minitrace output.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def extract_schema(records):
    """Extract structural schema from a list of JSONL records.

    Returns a dict describing the format structure:
    - record_types: {type_key: count}
    - field_paths: {path: {types: [str], count: int, sample_values: [...]}}
    """
    record_types = defaultdict(int)
    field_paths = defaultdict(lambda: {"types": set(), "count": 0, "values": set()})

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                vtype = type(v).__name__
                if v is None:
                    vtype = "null"
                field_paths[path]["types"].add(vtype)
                field_paths[path]["count"] += 1
                # Capture enum-like values (short strings, booleans, small ints)
                if isinstance(v, (bool, int)) or (isinstance(v, str) and len(v) < 50):
                    vals = field_paths[path]["values"]
                    if len(vals) < 20:  # cap at 20 distinct values
                        vals.add(str(v))
                walk(v, path)
        elif isinstance(obj, list):
            for item in obj[:5]:  # sample first 5
                walk(item, prefix + "[]")

    for rec in records:
        # Build type key
        rtype = rec.get("type", "<none>")
        payload = rec.get("payload", {})
        ptype = payload.get("type", "") if isinstance(payload, dict) else ""
        # Also handle item-based formats (Codex exec)
        item = rec.get("item", {})
        itype = item.get("type", "") if isinstance(item, dict) else ""

        if ptype:
            type_key = f"{rtype}/{ptype}"
        elif itype:
            type_key = f"{rtype}/{itype}"
        else:
            type_key = rtype

        record_types[type_key] += 1
        walk(rec)

    # Serialize
    schema = {
        "record_types": dict(record_types),
        "field_count": len(field_paths),
        "fields": {
            path: {
                "types": sorted(info["types"]),
                "count": info["count"],
                "sample_values": sorted(info["values"])[:10],
            }
            for path, info in sorted(field_paths.items())
        },
    }
    return schema


def compare_schemas(reference, current):
    """Compare two schemas and report differences.

    Returns (issues, warnings) where:
    - issues: breaking changes (missing record types, missing required fields)
    - warnings: non-breaking changes (new fields, new record types, type changes)
    """
    issues = []
    warnings = []

    # Record type changes
    ref_types = set(reference.get("record_types", {}).keys())
    cur_types = set(current.get("record_types", {}).keys())

    missing_types = ref_types - cur_types
    new_types = cur_types - ref_types

    for t in missing_types:
        issues.append(f"MISSING record type: {t} (was in reference)")
    for t in new_types:
        warnings.append(f"NEW record type: {t} (not in reference)")

    # Field changes
    ref_fields = set(reference.get("fields", {}).keys())
    cur_fields = set(current.get("fields", {}).keys())

    missing_fields = ref_fields - cur_fields
    new_fields = cur_fields - ref_fields

    for f in sorted(missing_fields):
        # Only flag as issue if the field was common (appeared in >20% of records)
        ref_info = reference["fields"][f]
        ref_total = sum(reference["record_types"].values())
        if ref_info["count"] > ref_total * 0.2:
            issues.append(f"MISSING field: {f} (appeared {ref_info['count']}x in reference)")
        else:
            warnings.append(f"Missing field: {f} (was rare: {ref_info['count']}x)")

    for f in sorted(new_fields):
        warnings.append(f"NEW field: {f}")

    # Type changes on common fields
    for f in ref_fields & cur_fields:
        ref_types_f = set(reference["fields"][f]["types"])
        cur_types_f = set(current["fields"][f]["types"])
        if ref_types_f != cur_types_f:
            warnings.append(
                f"TYPE change: {f}: {sorted(ref_types_f)} → {sorted(cur_types_f)}"
            )

    return issues, warnings


def load_jsonl(path):
    """Load JSONL file into list of records."""
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def test_all():
    """Test all adapters against their format references."""
    script_dir = Path(__file__).parent
    results = []

    for adapter_dir in sorted(script_dir.iterdir()):
        ref_dir = adapter_dir / "format-reference"
        if not ref_dir.is_dir():
            continue

        framework = adapter_dir.name
        for ref_file in sorted(ref_dir.glob("*.jsonl")):
            records = load_jsonl(ref_file)
            if not records:
                continue

            schema = extract_schema(records)
            # Self-test: extract and compare against itself
            # In a real workflow, you'd compare against a saved schema JSON
            issues, warnings = [], []

            schema_file = ref_file.with_suffix(".schema.json")
            if schema_file.exists():
                with open(schema_file) as f:
                    ref_schema = json.load(f)
                issues, warnings = compare_schemas(ref_schema, schema)
            else:
                # First run: save schema
                with open(schema_file, "w") as f:
                    json.dump(schema, f, indent=2)
                warnings.append(f"Schema reference created: {schema_file.name}")

            status = "FAIL" if issues else "PASS"
            results.append((framework, ref_file.name, status, issues, warnings))

    if not results:
        print("No format references found. Run discovery first.")
        return False

    all_pass = True
    for framework, filename, status, issues, warnings in results:
        print(f"{status} {framework}/{filename}")
        for issue in issues:
            print(f"  ERROR: {issue}")
            all_pass = False
        for warn in warnings:
            print(f"  WARN:  {warn}")

    print(f"\n--- Format Stability Summary ---")
    print(f"Adapters tested: {len(set(r[0] for r in results))}")
    print(f"Files tested: {len(results)}")
    print(f"Result: {'ALL PASS' if all_pass else 'FAILURES DETECTED'}")
    return all_pass


def main():
    p = argparse.ArgumentParser(description="minitrace format stability test")
    p.add_argument("--extract", help="Extract schema from JSONL file (print to stdout)")
    p.add_argument("--check", help="Check JSONL against reference schema")
    p.add_argument("--reference", help="Reference schema JSON (with --check)")
    p.add_argument("--self-test", help="Extract schema and verify non-empty")
    p.add_argument("--all", action="store_true",
                   help="Test all adapters against format references")
    args = p.parse_args()

    if args.all:
        success = test_all()
        sys.exit(0 if success else 1)

    if args.extract:
        records = load_jsonl(args.extract)
        schema = extract_schema(records)
        print(json.dumps(schema, indent=2))
        print(f"\n# {len(schema['record_types'])} record types, "
              f"{schema['field_count']} fields", file=sys.stderr)

    elif args.check and args.reference:
        records = load_jsonl(args.check)
        current = extract_schema(records)

        with open(args.reference) as f:
            reference = json.load(f)

        issues, warnings = compare_schemas(reference, current)

        for issue in issues:
            print(f"ERROR: {issue}")
        for warn in warnings:
            print(f"WARN:  {warn}")

        if issues:
            print(f"\nFAIL: {len(issues)} breaking changes detected")
            sys.exit(1)
        else:
            print(f"\nPASS ({len(warnings)} warnings)")

    elif args.self_test:
        records = load_jsonl(args.self_test)
        schema = extract_schema(records)
        if not schema["record_types"]:
            print(f"FAIL: no records in {args.self_test}")
            sys.exit(1)
        print(f"PASS {args.self_test}: {len(schema['record_types'])} record types, "
              f"{schema['field_count']} fields")

    else:
        p.print_help()


if __name__ == "__main__":
    main()
