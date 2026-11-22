#!/usr/bin/env python3
"""Axis-C drift watch for the pi conformance unit (harness #4).

Thin per-harness wiring over `adapters/drift_core.py`, the pi sibling of the
claude-code / goose / opencode watchers. The generic signals and the run loop
live in the core; this file supplies only what is pi-specific:

  • no preprocess — pi is the original DSL pilot: its native raw IS a JSONL
                   session log and the DSL runner reads it directly (no
                   events.json intermediary). DriftConfig.preprocess is None, so
                   the core feeds the raw JSONL straight to the runner. The raw
                   surface is read with the default read_jsonl reader.
  • CELL_SIGNALS — the contract cells pi can structurally reach. pi 0.58.1 ships a
                   local filesystem/shell toolset only (bash / read / write /
                   edit / search|grep|glob|list_files, per pi/0.58.1.dsl.yaml
                   operation_types): read/search/grep/glob/list_files=READ,
                   write=NEW, edit=MODIFY, bash=EXECUTE, default=OTHER. It has NO
                   web tool, NO subagent/task tool, and emits no cross-session
                   lineage, so the delegate/web/lineage cells (9/11/12/13) and the
                   claude-code-only channels (framework_control input, hooks,
                   command-permission attachments, MCP metadata → 14/16/17/20/27)
                   are all absent. Reachable set: 1,2,3,4,5,6,7,8,10,26.
  • version arm  — pi's analog of goose's schema_version arm and opencode's
                   session.version arm. pi carries a storage-FORMAT version
                   in-band as the session record's integer `version` (3 in the
                   0.58.1 corpus). The harness RELEASE version (0.58.1, 0.80.3,
                   ...) is NOT in-band — it is capture provenance (manifest
                   harness_version). A change in the in-band format-int is a real
                   storage-format-evolution signal (WARNING): the DSL keys on the
                   session/message record shape, so a bumped format version may
                   move fields the adapter reads.

Inputs are pi native `.jsonl` session logs scoped to the conformance scenario
(one per scenario, basename inputs/<name>.jsonl). Both this watcher's raw surface
and the DSL runner consume the same file.

Run (deps must be importable — use uv):
    uv run --with pyyaml --with rfc8785 python drift_watch.py \
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
DSL = ADAPTER_DIR / "0.58.1.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- pi in-band format version ---------------------------------------------

def _format_version(jsonl_path: pathlib.Path):
    """The pi storage-FORMAT version, carried in-band on the session record's
    integer `version` field. Returns the first session record's version (a pi
    session log holds one session record), or None if absent. This is NOT the
    harness release version (0.58.1 / 0.80.3), which pi does not record in-band."""
    try:
        for line in re.split(r"\r\n|\r|\n", jsonl_path.read_text()):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "session":
                return obj.get("version")
    except (OSError, json.JSONDecodeError):
        return None
    return None


def version_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    """WARNING on an in-band session-record format-version change vs baseline —
    pi's storage-format-evolution signal (the analog of goose's schema_version arm
    and opencode's session.version arm). A bump is not fatal: the drift is real,
    but whether the DSL still reads the new shape is the operator's call. The DSL
    keys on the session / model_change / message record structure, so confirm the
    field paths it reads (message.content[], message.usage.*, model_change.modelId)
    survive the new format version."""
    base = _format_version(ctx.base_input)
    cand = _format_version(ctx.cand_input)
    if base != cand:
        return [], [f"pi session.version (in-band storage-format int) changed vs "
                    f"baseline: {base} -> {cand} (format evolution; confirm the "
                    "record paths 0.58.1.dsl.yaml reads still hold in the candidate)"]
    return [], []


# --- Cell -> structural-signal extractor (pi) -------------------------------
# The same minitrace contract cells as claude-code, restricted to what pi can
# structurally reach. Predicates read the emitted trace only. pi tool vocabulary
# (0.58.1.dsl.yaml operation_types): read/read_file/search/grep/glob/list_files
# =READ, write/write_file=NEW, edit/edit_file=MODIFY, bash=EXECUTE, default=OTHER.
# pi has NO web tool (every READ is a filesystem read → cell 5, never cell 11) and
# NO subagent/task tool (no DELEGATE, no delegates-to/delegated-by lineage → cells
# 9/12/13 absent), and no framework_control/hook/attachment/MCP surface.


def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


CELL_SIGNALS = {
    1: lambda t: any(tn.get("role") == "user" for tn in t.get("turns") or []),
    2: lambda t: any(tn.get("role") == "assistant" for tn in t.get("turns") or []),
    3: lambda t: bool(t.get("tool_calls")),
    4: lambda t: any((tc.get("output") or {}).get("content_origin") == "tool_output"
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
    """Cells that fired across a scenario run (pi CELL_SIGNALS). Module export so a
    manifest generator derives annotated_cells_fired from the same predicates the
    self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for pi")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/ (no default: pi has "
                         "no anchor rung until a rung is frozen)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.jsonl captures; omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="pi",
        preprocess=None,               # pi: DSL runner reads the native JSONL
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
