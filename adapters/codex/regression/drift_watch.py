#!/usr/bin/env python3
"""Axis-C drift watch for the codex conformance unit (harness #6).

Thin per-harness wiring over `adapters/drift_core.py`, the codex sibling of the
claude-code / goose / opencode / pi / droid watchers. The generic signals and
the run loop live in the core; this file supplies only what is codex-specific:

  • preprocess   — preprocess_codex.py normalises BOTH native source formats
                   (rollout `~/.codex/sessions/rollout-*.jsonl` and
                   `codex exec --json` stdout) into one event_kind container
                   via field-presence dispatch; the DSL is source-format-
                   agnostic. The raw surface reads the native JSONL with the
                   default read_jsonl reader, so signal-0 sees the true native
                   vocabulary (rollout: session_meta / turn_context /
                   response_item / event_msg; exec: thread/turn/item).
  • CELL_SIGNALS — the contract cells codex can structurally reach. codex's
                   tool surface is the shell (exec_command) plus patch-shaped
                   functions; preprocess classifies operation_type from the
                   command string / function name (READ / NEW / MODIFY /
                   EXECUTE / OTHER). No web tool, no subagent/task tool, no
                   lineage, none of the claude-code-only channels — the same
                   pi-shaped reachable set: 1,2,3,4,5,6,7,8,10,26.
  • version arm  — codex carries its harness RELEASE version in-band:
                   session_meta.payload.cli_version (rollout format only; the
                   exec format has no version record at all, so exec-source
                   runs read None and a fresh exec capture depends on
                   --framework-meta for its version identity). Unlike pi/droid
                   format-ints, cli_version changes on every release, so on a
                   release delta the arm WARNING is EXPECTED and doubles as
                   capture verification: a delta where cli_version did NOT
                   change means the candidate was captured with the wrong
                   binary.

Inputs are codex native `.jsonl` session logs scoped to the conformance
scenario (one per scenario, basename inputs/<name>.jsonl). Both this watcher's
raw surface and the preprocess path consume the same file.

Run (deps must be importable — use the adapter venv or uv):
    ../../../.venv/bin/python drift_watch.py \
        --baseline <ver> --candidate-inputs /path/to/capture/inputs --operator-type <type>
Exit 0 = no drift; exit 1 = drift; exit 2 = harness/usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ADAPTER_DIR = (ROOT / "..").resolve()
ADAPTERS_ROOT = (ADAPTER_DIR / "..").resolve()
PREPROCESS = ADAPTER_DIR / "preprocess_codex.py"
DSL = ADAPTER_DIR / "codex.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- codex in-band release version -------------------------------------------

def _cli_version(jsonl_path: pathlib.Path):
    """The codex RELEASE version, carried in-band on the rollout session_meta
    record (payload.cli_version). Returns None for the exec format (which has
    no version record) or when absent — None is data here, not an error: an
    exec-source rung's version identity rests on manifest.harness_version."""
    try:
        for line in re.split(r"\r\n|\r|\n", jsonl_path.read_text()):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "session_meta":
                return (obj.get("payload") or {}).get("cli_version")
    except (OSError, json.JSONDecodeError):
        return None
    return None


def version_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    """WARNING on an in-band cli_version change vs baseline. For codex this is
    a RELEASE version (not a storage-format int), so across a release delta the
    warning is EXPECTED and serves as capture verification — read it as
    'the candidate really is the other version'. The suspicious cases are the
    inverse: an unchanged cli_version across a supposed release delta (wrong
    binary in the capture), or a rollout-source candidate reading None
    (truncated capture, missing session_meta)."""
    base = _cli_version(ctx.base_input)
    cand = _cli_version(ctx.cand_input)
    if base != cand:
        return [], [f"codex session_meta.cli_version (in-band RELEASE version) "
                    f"changed vs baseline: {base} -> {cand} (expected on a "
                    "release delta; if this run was meant to be same-version, "
                    "the capture used the wrong binary)"]
    return [], []


# --- Cell -> structural-signal extractor (codex) -----------------------------
# The same minitrace contract cells, restricted to what codex can structurally
# reach. Predicates read the emitted trace only. codex operation_type comes
# from preprocess_codex.py's classifiers (command-string regex for
# exec_command, function-name mapping for patch-shaped tools): READ / NEW /
# MODIFY / EXECUTE / OTHER. No web tool (cell 11 never), no subagent/task tool
# (cells 9/12/13 absent), no framework_control/hook/attachment/MCP surface
# (14/16/17/20/27 absent).


def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


CELL_SIGNALS = {
    1: lambda t: any(tn.get("role") == "user" for tn in t.get("turns") or []),
    2: lambda t: any(tn.get("role") == "assistant" for tn in t.get("turns") or []),
    3: lambda t: bool(t.get("tool_calls")),
    # cell 4: tool output captured WITH a classified origin. The other watchers
    # test content_origin == "tool_output"; codex never emits that literal —
    # shell-mediated use collapses to "local_exec" (spec footnote [1]) and the
    # patch-shaped functions map to local_file/model_echo — so the codex
    # predicate is "any truthy content_origin", not the literal.
    4: lambda t: any((tc.get("output") or {}).get("content_origin")
                     for tc in t.get("tool_calls") or []),
    5: lambda t: "READ" in _ops(t),
    6: lambda t: "MODIFY" in _ops(t),
    7: lambda t: "NEW" in _ops(t),
    8: lambda t: "EXECUTE" in _ops(t),
    10: lambda t: "OTHER" in _ops(t),
    26: lambda t: any((t.get("metrics") or {}).get(k, 0)
                      for k in ("total_input_tokens", "total_output_tokens")),
}


def fired_cells(traces: list[dict]) -> set[int]:
    """Cells that fired across a scenario run (codex CELL_SIGNALS). Module export
    so a manifest generator derives annotated_cells_fired from the same
    predicates the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for codex")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/ (0.114.0 archived "
                         "anchor, 0.132.0 image baseline, 0.145.0)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.jsonl captures; omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="codex",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        extra_arms=[version_arm],
        ledger=ROOT / "drift-ledger.yaml",
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
