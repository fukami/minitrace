#!/usr/bin/env python3
"""Axis-C drift watch for the claude-code conformance unit (Phase 3).

Thin per-harness wiring over `adapters/drift_core.py`. The generic signals (raw
shape, dark cell, new-unmapped, dropped-dependency) and the whole run loop live in
the core; this file supplies only what is claude-code-specific:

  • CELL_SIGNALS   — structural "did this contract cell fire?" predicates, some of
                     which key on claude-code tool names (WebFetch/WebSearch/…).
  • sidechain      — the native subagent transcripts at `<stem>/subagents/*.jsonl`
                     that the preprocessor auto-discovers; the raw-format arm must
                     read them too or a new native shape arriving inside a subagent
                     is invisible.
  • subtype arm    — attachment SUBTYPE rename/removal (2.1.153 `deferred_tools_delta`
                     -> 2.1.215 `agent_listing_delta`). A subtype the DSL keys on can
                     vanish without any generic signal firing; this warns on it.

Complements `<ver>/verify.py` (adapter-code freeze via content_hash). See the
regression README for the full signal contract.

Run (deps must be importable — use uv; docker/uv need the sandbox disabled):
    uv run --with pyyaml --with rfc8785 python drift_watch.py --baseline 2.1.153 --operator-type <type>
    uv run --with pyyaml --with rfc8785 python drift_watch.py --baseline 2.1.153 --operator-type <type> \
        --candidate-inputs /path/to/latest-capture/inputs

Exit 0 = no drift; exit 1 = drift; exit 2 = harness/usage error.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ADAPTER_DIR = (ROOT / "..").resolve()
ADAPTERS_ROOT = (ADAPTER_DIR / "..").resolve()
PREPROCESS = ADAPTER_DIR / "preprocess_claude_code.py"
DSL = ADAPTER_DIR / "claude-code.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, read_jsonl, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- Cell -> structural-signal extractor (claude-code) -----------------------
# Each entry answers "did this contract cell fire in this trace?" from the emitted
# trace shape alone. Version strings in taxonomies are matched by suffix so a fresh
# capture at a new harness version still matches. Cells absent here are unreachable
# on the headless capture path (15, 18, 19, 21, 23, 24, 25) or out of the JSONL
# adapter's reach (28); a manifest that annotates them would itself be the bug.

def _tax_has(trace: dict, needle: str) -> bool:
    for ann in trace.get("annotations") or []:
        for t in ann.get("taxonomy") or []:
            if needle in t:
                return True
    return False


def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


def _tool_names(trace: dict) -> set[str]:
    return {tc.get("tool_name") for tc in trace.get("tool_calls") or []}


# The cell-11 web tools also carry operation_type READ, so a plain "READ in ops"
# test lets them masquerade as cell 5 (filesystem read). Cell 5 fires only when op
# READ comes from a NON-web tool; the web READ is cell 11's.
_WEB_TOOLS = {"WebFetch", "WebSearch", "ToolSearch"}


def _has_fs_read(trace: dict) -> bool:
    return any(tc.get("operation_type") == "READ"
              and tc.get("tool_name") not in _WEB_TOOLS
              for tc in trace.get("tool_calls") or [])


def _lineage_types(trace: dict) -> set[str]:
    return {e.get("type") for e in trace.get("lineage") or []}


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
    11: lambda t: bool(_tool_names(t) & {"WebFetch", "WebSearch", "ToolSearch"}),
    12: lambda t: "delegates-to" in _lineage_types(t),
    13: lambda t: "delegated-by" in _lineage_types(t),
    14: lambda t: _tax_has(t, "/input-channel/framework_control"),
    16: lambda t: _tax_has(t, "/hook/"),
    17: lambda t: _tax_has(t, "/hook-additional-context/"),
    20: lambda t: _tax_has(t, "/attachment/command_permissions"),
    26: lambda t: any((t.get("metrics") or {}).get(k, 0)
                      for k in ("total_input_tokens", "total_output_tokens")),
    27: lambda t: any((tc.get("framework_metadata") or {}).get("mcp_server")
                      for tc in t.get("tool_calls") or []),
}


def fired_cells(traces: list[dict]) -> set[int]:
    """Cells that fired across a scenario run (claude-code CELL_SIGNALS).

    Kept as a module export so a manifest generator can derive
    `annotated_cells_fired` from the same predicates the self-test asserts against.
    """
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Subagent sidechain (claude-code) ---------------------------------------

def _sidechain_records(input_path: pathlib.Path) -> list[dict]:
    """Raw records from the native subagent sidechain `<stem>/subagents/*.jsonl`.

    The preprocessor auto-discovers these (delegation), and they carry record
    types/fields the main transcript never does (`isSidechain`, `agentId`, …).
    Feeding them to the raw-format arm keeps a new native shape that arrives inside
    a subagent from being a false negative.
    """
    recs: list[dict] = []
    sidechain_dir = input_path.with_suffix("") / "subagents"
    if sidechain_dir.is_dir():
        for sc in sorted(sidechain_dir.glob("*.jsonl")):
            recs.extend(read_jsonl(sc))
    return recs


# --- Attachment-subtype drift (signal 4, claude-code) -----------------------
# An attachment SUBTYPE VALUE can be renamed without any generic signal firing:
# the field PATH `attachment.type` still exists, the record TYPE is still
# `attachment`, and a subtype the DSL keys on that vanishes simply stops matching
# its rule — the rule goes silently dead. WARNING both directions (subtype presence
# is content-driven; gating fatally would false-positive on benign content
# variance). A disappeared subtype the DSL keys on is annotated as a dead rule.

def _attachment_subtypes(raw_records: list[dict]) -> set[str]:
    out: set[str] = set()
    for r in raw_records:
        if r.get("type") == "attachment":
            att = r.get("attachment")
            if isinstance(att, dict) and isinstance(att.get("type"), str):
                out.add(att["type"])
    return out


def _dsl_keyed_subtypes(dsl_path: pathlib.Path) -> set[str]:
    """Subtype VALUES the DSL routes on (`attachment_subtype, value: X`)."""
    out: set[str] = set()
    pat = re.compile(r"attachment_subtype\s*,\s*value:\s*([A-Za-z0-9_]+)")
    try:
        for m in pat.finditer(dsl_path.read_text()):
            out.add(m.group(1))
    except OSError:
        pass
    return out


def make_subtype_arm(dsl_path: pathlib.Path):
    dsl_subtypes = _dsl_keyed_subtypes(dsl_path)

    def arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
        base_subtypes = _attachment_subtypes(ctx.base_raw)
        cand_subtypes = _attachment_subtypes(ctx.cand_raw)
        gone = sorted(base_subtypes - cand_subtypes)
        appeared = sorted(cand_subtypes - base_subtypes)
        if not (gone or appeared):
            return [], []
        lines = []
        if gone:
            annotated = [f"{s}  [DSL keys on this — rule now unexercised]"
                         if s in dsl_subtypes else s for s in gone]
            lines.append("disappeared: " + ", ".join(annotated))
        if appeared:
            lines.append("appeared:    " + ", ".join(appeared))
        hint = (" (a disappeared+appeared pair is the rename fingerprint)"
                if gone and appeared else "")
        return [], ["attachment subtype set changed vs baseline"
                    f"{hint}:\n        " + "\n        ".join(lines)]

    return arm


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for claude-code")
    ap.add_argument("--baseline", default="2.1.153",
                    help="frozen version dir under regression/ (default: 2.1.153)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.jsonl captures; omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="claude-code",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        extra_raw_records=_sidechain_records,
        extra_arms=[make_subtype_arm(DSL)],
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
