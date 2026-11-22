#!/usr/bin/env python3
"""Axis-C drift watch for the droid conformance unit (harness #5).

Thin per-harness wiring over `adapters/drift_core.py`, the droid sibling of the
claude-code / goose / opencode / pi watchers. The generic signals and the run
loop live in the core; this file supplies only what is droid-specific:

  • preprocess   — droid HAS a preprocessor (unlike pi). droid's native raw is a
                   PAIRED capture: a `<uuid>.jsonl` session log plus a
                   `<uuid>.settings.json` sidecar. preprocess_droid.py stitches
                   the sidecar in as a synthetic `settings` event and emits one
                   container per session, which the DSL turns into one trace.
                   So this uses the preprocess path (DriftConfig.preprocess set),
                   NOT pi's preprocess=None path.
  • sidecar arm  — the paired .settings.json is part of droid's native raw
                   surface (the preprocessor consumes it), but the default
                   read_jsonl reader only sees the .jsonl. `extra_raw_records`
                   reads the sidecar and returns it as a `settings`-typed record
                   so signal 0 sees a NEW settings key (a real format-evolution
                   signal) rather than being blind to the whole sidecar. Values
                   are volatile (tokenUsage, timestamps) but raw_surface compares
                   PATHS, not values, so only a new key registers.
  • CELL_SIGNALS — the contract cells droid can structurally reach. droid 0.122.0+
                   ships Execute/Read/Grep/Create/Edit/TodoWrite (droid.dsl.yaml
                   operation_types): Read/Grep=READ, Create=NEW, Edit=MODIFY,
                   Execute=EXECUTE, TodoWrite/default=OTHER. The v0.1 adapter is
                   single-trace with NO fork/sub-agent emission and NO web/MCP
                   tool surface (all deferred, see droid.dsl.yaml header), so the
                   delegate/web/lineage cells (9/11/12/13) and the claude-code-only
                   channels (14/16/17/20/27) are absent. Reachable: 1,2,3,4,5,6,7,
                   8,10,26 — the same shape as pi. Cell 26 (token metrics) went
                   from declared-reachable-but-dark to actually firing on
                   2026-07-25: the sidecar's tokenUsage is now mapped onto
                   metrics.total_{input,output}_tokens. Until then droid claimed
                   "the same shape as pi" while pi fired 26 and droid did not —
                   same DECLARED set, different ACTUAL coverage.
  • version arm  — droid's analog of goose's schema_version arm and pi's format
                   arm. droid carries a storage-FORMAT version in-band as the
                   session_start record's integer `version` (2 in the 0.122.0
                   corpus). The harness RELEASE version (0.122.0, 0.178.0, ...) is
                   NOT in-band — droid records no release stamp in the .jsonl OR
                   the .settings.json sidecar (verified: the sidecar carries model
                   / autonomy / tokenUsage only), so it lives in capture
                   provenance (manifest harness_version). A change in the in-band
                   format-int is a real storage-format-evolution signal (WARNING).

Inputs are droid native `.jsonl` session logs (each paired with a
`<name>.settings.json` sidecar) scoped to the conformance scenario, basename
inputs/<name>.jsonl. The preprocessor resolves the sidecar by suffix swap, so the
frozen sidecar must share the input's basename stem.

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
PREPROCESS = ADAPTER_DIR / "preprocess_droid.py"
DSL = ADAPTER_DIR / "droid.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- droid paired-sidecar raw surface ---------------------------------------

def sidecar_records(jsonl_path: pathlib.Path) -> list[dict]:
    """The paired `<uuid>.settings.json` sidecar as a single `settings`-typed
    record, so signal 0 (raw-surface) sees the sidecar's key structure. The
    preprocessor stitches this file into a synthetic settings event and the DSL
    projects it into ext:droid:* annotations, so a new sidecar key IS native
    format drift — without this hook the default read_jsonl reader would be blind
    to the whole sidecar. Empty list if the sidecar is absent (never crash the
    walk on a missing pair)."""
    sidecar = jsonl_path.with_suffix(".settings.json")
    if not sidecar.is_file():
        return []
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return [{"type": "settings", **data}]


# --- droid in-band format version -------------------------------------------

def _format_version(jsonl_path: pathlib.Path):
    """The droid storage-FORMAT version, carried in-band on the session_start
    record's integer `version` field. Returns the first session_start record's
    version, or None if absent. This is NOT the harness release version (0.122.0
    / 0.178.0), which droid does not record in-band (neither the .jsonl nor the
    .settings.json sidecar carries a release stamp)."""
    try:
        for line in re.split(r"\r\n|\r|\n", jsonl_path.read_text()):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "session_start":
                return obj.get("version")
    except (OSError, json.JSONDecodeError):
        return None
    return None


def version_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    """WARNING on an in-band session_start.version change vs baseline — droid's
    storage-format-evolution signal (the analog of goose's schema_version arm and
    pi's format arm). A bump is not fatal: the drift is real, but whether the DSL
    still reads the new shape is the operator's call. The preprocessor keys on the
    session_start / message record structure, so confirm the field paths the DSL
    reads (message.content[], message.usage.*, tool_use inputs) survive the new
    format version."""
    base = _format_version(ctx.base_input)
    cand = _format_version(ctx.cand_input)
    if base != cand:
        return [], [f"droid session_start.version (in-band storage-format int) "
                    f"changed vs baseline: {base} -> {cand} (format evolution; "
                    "confirm the record paths droid.dsl.yaml reads still hold in "
                    "the candidate)"]
    return [], []


# --- Cell -> structural-signal extractor (droid) ----------------------------
# The same minitrace contract cells as claude-code, restricted to what droid can
# structurally reach. Predicates read the emitted trace only. droid tool
# vocabulary (droid.dsl.yaml operation_types): Read/Grep=READ, Create=NEW,
# Edit=MODIFY, Execute=EXECUTE, TodoWrite/default=OTHER. The v0.1 adapter has NO
# web tool (every READ is a filesystem read → cell 5, never cell 11), NO
# subagent/task/fork emission (no DELEGATE, no delegates-to/delegated-by lineage
# → cells 9/12/13 absent), and no framework_control/hook/attachment/MCP surface
# (14/16/17/20/27). Same reachable set as pi.


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
    """Cells that fired across a scenario run (droid CELL_SIGNALS). Module export
    so a manifest generator derives annotated_cells_fired from the same predicates
    the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for droid")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/ (no default: droid "
                         "has no anchor rung until a rung is frozen)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.jsonl captures (each with its "
                         ".settings.json sidecar); omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="droid",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        extra_raw_records=sidecar_records,
        extra_arms=[version_arm],
        ledger=ROOT / "drift-ledger.yaml",
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
