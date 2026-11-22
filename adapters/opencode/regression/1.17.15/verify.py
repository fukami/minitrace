#!/usr/bin/env python3
"""Regression verify for the opencode adapter at harness 1.17.15.

Re-runs preprocess + DSL on each frozen input (an opencode SQLite db scoped to the
conformance scenario) and asserts the output matches manifest.json: trace_count,
per-trace content_hash, and unmapped_path_count. Catches ADAPTER drift — any
preprocess/DSL change that alters the emitted trace over a fixed input corpus.
(Format/Axis-C drift is a separate loop: re-capture at a new opencode version and
run drift_watch.py.)

content_hash folds provenance.source_path, so the freeze is path-sensitive: cwd =
this dir, input paths = inputs/<name>.db exactly as recorded in the manifest, so the
hashes reproduce wherever the repo is checked out.

Run (deps must be importable — use uv):
    uv run --with pyyaml --with rfc8785 --with jsonschema python verify.py
Exit 0 = all scenarios match; exit 1 = drift (details printed).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[2]))
from schema_gate import schema_errors  # noqa: E402
PREPROCESS = ROOT / ".." / ".." / "preprocess_opencode.py"
DSL_RUNNER = ROOT / ".." / ".." / ".." / "dsl_runner.py"
DSL = ROOT / ".." / ".." / "opencode.dsl.yaml"


def _run(scenario_input: str, tmp: pathlib.Path) -> tuple[list[str], int, list[str]]:
    """Run the adapter on one input; return (content_hashes, unmapped_path_count).

    cwd = ROOT and scenario_input is the manifest-relative path, so the recorded
    provenance.source_path — and thus content_hash — is checkout-independent.
    """
    events = tmp / "events.json"
    traces = tmp / "traces.json"
    coverage = tmp / "coverage.json"
    subprocess.run(
        [sys.executable, str(PREPROCESS), scenario_input, str(events)],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=300,
    )
    subprocess.run(
        [sys.executable, str(DSL_RUNNER), str(DSL), str(events),
         "--output", str(traces), "--coverage", str(coverage),
         "--operator-type", "scripted"],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=300,
    )
    out = json.loads(traces.read_text())
    hashes = [t["content_hash"] for t in out]
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
