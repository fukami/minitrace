#!/usr/bin/env python3
"""Regression verify for the aider adapter at harness 0.82.0.

Re-runs preprocess + DSL on each frozen input (a codex rollout session log,
scoped to the conformance scenario) and asserts the output matches
manifest.json: trace_count, per-trace content_hash, and unmapped_path_count.
Catches ADAPTER drift — any preprocess/DSL change that alters the emitted
trace over a fixed input corpus. (Format/Axis-C drift across codex versions
is the separate two-rung loop: compare against the fresh-capture rung.)

aider traces carry no provenance.source_path, so the content_hash is
checkout-independent (the input path does not enter the hash). cwd = this
dir and input = inputs/<name>.jsonl for a stable relative invocation.

Run (deps must be importable — use the adapter venv or uv):
    ../../../.venv/bin/python verify.py
    # or: uv run --with pyyaml --with rfc8785 --with jsonschema python verify.py
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
PREPROCESS = ROOT / ".." / ".." / "preprocess_aider.py"
DSL_RUNNER = ROOT / ".." / ".." / ".." / "dsl_runner.py"
DSL = ROOT / ".." / ".." / "aider.dsl.yaml"
ADAPTERS_DIR = ROOT / ".." / ".." / ".."


def _run(scenario_input: str, tmp: pathlib.Path) -> tuple[list[str], int, list[str]]:
    """Run the adapter on one input; return (content_hashes, unmapped_path_count).

    aider is single-trace, so dsl_runner emits content_hash=null (the
    auto-stamp is multi-trace-only). We back-fill it here exactly as
    verify_run.py does, so the frozen hash reproduces.
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
         "--operator-type", "scripted", "--coverage-type-field", "event_kind"],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=300,
    )
    out = json.loads(traces.read_text())
    out_list = out if isinstance(out, list) else [out]
    if any(t.get("content_hash") is None for t in out_list):
        sys.path.insert(0, str(ADAPTERS_DIR.resolve()))
        from cross_trace import back_fill_cross_trace_references
        out_list = back_fill_cross_trace_references(out_list)
    hashes = [t["content_hash"] for t in out_list]
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
