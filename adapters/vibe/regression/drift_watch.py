#!/usr/bin/env python3
"""Axis-C drift watch for the vibe conformance unit (harness #8).

Thin per-harness wiring over `adapters/drift_core.py`, the vibe sibling of the
claude-code / goose / opencode / pi / droid / codex watchers. The generic
signals and the run loop live in the core; this file supplies only what is
vibe-specific:

  • raw_reader   — vibe's raw ground truth is a session DIRECTORY
                   (meta.json + messages.jsonl), not a single file; a scenario
                   input may also be a parent directory batching several
                   session dirs (vibe is the fleet's only multi-trace adapter:
                   one batch -> N traces with cross-trace lineage). The reader
                   returns {type: ...}-tagged records: one `meta` record per
                   session's meta.json and one `message.<role>` record per
                   messages.jsonl line — so a NEW ROLE in the message stream
                   surfaces as a new record type (FATAL), not as noise.
  • CELL_SIGNALS — the contract cells vibe can structurally reach. vibe's tool
                   surface is the widest after claude-code: shell (EXECUTE),
                   read_file/grep/list_directory (READ), write_file (NEW),
                   search_replace/edit_file/todo (MODIFY), task (DELEGATE ->
                   lineage cells 12/13 when parent and child are in the same
                   batch), web_search/web_fetch (cell 11), plus OTHER. No
                   hook / attachment / framework_control / MCP channels
                   (14/16/17/20/27 absent).
  • era arm      — vibe carries NO in-band harness version anywhere (meta.json
                   has no version field; messages.jsonl none either), so
                   version identity rests entirely on manifest.harness_version
                   and capture provenance. What IS in-band is the format ERA:
                   2.9.5+ meta.json carries `title_source` (plus
                   parent_session_id / loops); 2.4-era does not. The arm
                   WARNs on an era change vs baseline — expected on a
                   2.4->2.9+ delta, suspicious within one era.

Inputs are native vibe session directories scoped to the conformance scenario
(the manifest declares `input: inputs/<name>` where <name> is a session dir or
a batch dir of session_* subdirs). Both this watcher's raw surface and the
preprocess path consume the same directory.

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
PREPROCESS = ADAPTER_DIR / "preprocess_vibe.py"
DSL = ADAPTER_DIR / "vibe.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- vibe raw reader (session dir(s) -> type-tagged records) -----------------

def _session_dirs(input_path: pathlib.Path) -> list[pathlib.Path]:
    """A scenario input is either one session dir (meta.json + messages.jsonl
    directly) or a batch dir whose children are session dirs. Mirrors
    preprocess_vibe.discover_sessions without importing it (the reader must
    stay decoupled from preprocessor bugs — signal 0 is upstream of it)."""
    def is_session(p: pathlib.Path) -> bool:
        return (p / "meta.json").is_file() and (p / "messages.jsonl").is_file()
    if is_session(input_path):
        return [input_path]
    if input_path.is_dir():
        return [c for c in sorted(input_path.iterdir()) if is_session(c)]
    return []


def vibe_raw_reader(input_path: pathlib.Path) -> list[dict]:
    """One `meta` record per session's meta.json, one `message.<role>` record
    per messages.jsonl line. Tagging messages by role means a new role in the
    stream (vibe shipping a new message kind) fires the new-record-type FATAL
    arm instead of hiding inside a shared `message` bucket."""
    recs: list[dict] = []
    for sdir in _session_dirs(input_path):
        try:
            meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
        if isinstance(meta, dict):
            recs.append({"type": "meta", **meta})
        for line in re.split(r"\r\n|\r|\n", (sdir / "messages.jsonl").read_text(encoding="utf-8")):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                role = obj.get("role") or "_norole"
                recs.append({"type": f"message.{role}", **obj})
    return recs


# --- era arm (in-band format era; vibe has no in-band version) ---------------

def _eras(raw: list[dict]) -> set[str]:
    """Format era per meta record: `title_source` present -> 2.9-era, else
    2.4-era (same discriminator as preprocess_vibe.detect_version_label)."""
    return {"2.9-era" if "title_source" in r else "2.4-era"
            for r in raw if r.get("type") == "meta"}


def era_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    """WARNING on an in-band format-era change vs baseline. vibe carries no
    release version in-band at all, so this is the only in-band identity
    signal: expected to fire on a 2.4->2.9+ rung delta (read it as 'the
    candidate really is the other era'); a delta between two same-era rungs
    where this fires means the capture is not what the manifest claims."""
    base, cand = _eras(ctx.base_raw), _eras(ctx.cand_raw)
    if base != cand:
        return [], [f"vibe meta.json format era (title_source discriminator) "
                    f"changed vs baseline: {sorted(base)} -> {sorted(cand)} "
                    "(expected on a cross-era delta; within one era it means "
                    "the capture does not match the manifest's version claim)"]
    return [], []


# --- Cell -> structural-signal extractor (vibe) ------------------------------
# The same minitrace contract cells, restricted to what vibe can structurally
# reach. Predicates read the emitted trace only. vibe operation_type comes from
# the DSL's tool-name mapping (vibe.dsl.yaml operation_types). content_origin
# is uniformly the literal "tool_output" (DSL-pinned; verified against emitted
# traces on the 2026-03 corpus) — unlike codex, whose shell-mediated calls
# collapse to local_exec, so the literal predicate is correct here.

def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


def _tool_names(trace: dict) -> set[str]:
    return {tc.get("tool_name") for tc in trace.get("tool_calls") or []}


def _lineage_types(trace: dict) -> set[str]:
    return {e.get("type") for e in trace.get("lineage") or []}


# The cell-11 web tools also map to operation_type READ, and so does
# ask_user_question (an interaction, not a filesystem read) — a plain
# "READ in ops" test would let both masquerade as cell 5.
_WEB_TOOLS = {"web_search", "web_fetch"}
_NON_FS_READ_TOOLS = _WEB_TOOLS | {"ask_user_question"}


def _has_fs_read(trace: dict) -> bool:
    return any(tc.get("operation_type") == "READ"
               and tc.get("tool_name") not in _NON_FS_READ_TOOLS
               for tc in trace.get("tool_calls") or [])


CELL_SIGNALS = {
    1: lambda t: any(tn.get("role") == "user" for tn in t.get("turns") or []),
    2: lambda t: any(tn.get("role") == "assistant" for tn in t.get("turns") or []),
    3: lambda t: bool(t.get("tool_calls")),
    4: lambda t: any((tc.get("output") or {}).get("content_origin") == "tool_output"
                     for tc in t.get("tool_calls") or []),
    5: _has_fs_read,
    6: lambda t: "MODIFY" in _ops(t),
    7: lambda t: "NEW" in _ops(t),
    8: lambda t: "EXECUTE" in _ops(t),
    9: lambda t: "DELEGATE" in _ops(t),
    10: lambda t: "OTHER" in _ops(t),
    11: lambda t: bool(_tool_names(t) & _WEB_TOOLS),
    12: lambda t: "delegates-to" in _lineage_types(t),
    13: lambda t: "delegated-by" in _lineage_types(t),
    26: lambda t: any((t.get("metrics") or {}).get(k, 0)
                      for k in ("total_input_tokens", "total_output_tokens")),
}


def fired_cells(traces: list[dict]) -> set[int]:
    """Cells that fired across a scenario run (vibe CELL_SIGNALS). Module export
    so a manifest generator derives annotated_cells_fired from the same
    predicates the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for vibe")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/ (2.4.x archived "
                         "anchor, or an image rung)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh session-dir captures (one child dir per "
                         "scenario, matching the manifest input basenames); "
                         "omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="vibe",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        raw_reader=vibe_raw_reader,
        extra_arms=[era_arm],
        ledger=ROOT / "drift-ledger.yaml",
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
