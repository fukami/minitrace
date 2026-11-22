#!/usr/bin/env python3
"""Regression verify for the pi adapter at harness 0.74.0.

Re-runs the DSL on each frozen input (a pi native JSONL session log scoped to the
conformance scenario) and asserts the output matches manifest.json: trace_count,
per-trace content_hash, and unmapped_path_count. Catches ADAPTER drift — any DSL
change that alters the emitted trace over a fixed input corpus. (Format/Axis-C
drift is a separate loop: re-capture at a new pi version and run drift_watch.py.)

pi is the original DSL pilot and has no preprocessor: the DSL runner reads the raw
JSONL directly. pi single-trace output carries no runner-provided content_hash and
no provenance.source_path, so this verify computes its own freeze invariant — the
canonical (RFC 8785 JCS) hash of the trace with the volatile provenance.converted_at
normalized. The absence of source_path makes the freeze checkout-independent for
free, so the input path does not enter the hash (unlike the container adapters).

Run (deps must be importable — use uv):
    uv run --with pyyaml --with rfc8785 --with jsonschema python verify.py
Exit 0 = all scenarios match; exit 1 = drift (details printed).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

import rfc8785

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2]))
from schema_gate import schema_errors  # noqa: E402
DSL_RUNNER = ROOT / ".." / ".." / ".." / "dsl_runner.py"
DSL = ROOT / ".." / ".." / "0.58.1.dsl.yaml"


def _canonical_hash(trace: dict) -> str:
    """sha256 of the RFC 8785 canonicalization, with the only volatile field
    (provenance.converted_at, a wall-clock stamp) pinned so the freeze is stable."""
    trace = json.loads(json.dumps(trace))  # copy; don't mutate the caller's dict
    trace.setdefault("provenance", {})["converted_at"] = "FROZEN"
    return "sha256:" + hashlib.sha256(rfc8785.dumps(trace)).hexdigest()


def _run(scenario_input: str, tmp: pathlib.Path) -> tuple[list[str], int, list[str]]:
    """Run the adapter on one input; return (content_hashes, unmapped_path_count).

    cwd = ROOT and scenario_input is the manifest-relative path. pi is
    preprocessor-less: the DSL runner consumes the native JSONL directly.
    """
    traces = tmp / "traces.json"
    coverage = tmp / "coverage.json"
    subprocess.run(
        [sys.executable, str(DSL_RUNNER), str(DSL), scenario_input,
         "--output", str(traces), "--coverage", str(coverage),
         "--operator-type", "scripted"],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=300,
    )
    out = json.loads(traces.read_text())
    # pi is single-trace: one session log -> one trace object (not a container
    # array). Normalize to a list so a future multi-trace pi scenario still works.
    out_list = out if isinstance(out, list) else [out]
    hashes = [_canonical_hash(t) for t in out_list]
    cov = json.loads(coverage.read_text())
    unmapped = sum(len(v) for v in (cov.get("unmapped_paths_by_type") or {}).values())
    return hashes, unmapped, schema_errors(out)


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    failures: list[str] = []
    for name, spec in manifest["scenarios"].items():
        with tempfile.TemporaryDirectory() as td:
            try:
                hashes, unmapped, schema = _run(spec["input"], pathlib.Path(td))
            except subprocess.CalledProcessError as e:
                failures.append(f"{name}: adapter run failed\n{e.stderr}")
                continue
            except subprocess.TimeoutExpired as e:
                failures.append(f"{name}: adapter run timed out after {e.timeout}s")
                continue
        problems = []
        if len(hashes) != spec["trace_count"]:
            problems.append(f"trace_count {len(hashes)} != {spec['trace_count']}")
        if hashes != spec["content_hashes"]:
            problems.append(
                "content_hash drift:\n"
                f"    expected {spec['content_hashes']}\n"
                f"    actual   {hashes}"
            )
        if unmapped != spec["unmapped_path_count"]:
            problems.append(
                f"unmapped_path_count {unmapped} != {spec['unmapped_path_count']}"
            )
        if schema:
            problems.append(
                f"schema: {len(schema)} error(s) (not drift; do not re-freeze)"
                f"\n    " + "\n    ".join(schema[:10])
            )
        if problems:
            failures.append(f"{name}:\n  " + "\n  ".join(problems))
        else:
            print(f"  OK  {name} ({spec['trace_count']} trace(s))")

    if failures:
        print("\nDRIFT DETECTED:\n" + "\n".join(failures), file=sys.stderr)
        print(
            "\nIf the change was intentional, re-freeze: update manifest.json with "
            "the new hashes/counts and commit alongside the adapter change.",
            file=sys.stderr,
        )
        return 1
    print(f"\nall {len(manifest['scenarios'])} scenario(s) match the freeze")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
