#!/usr/bin/env python3
"""Axis-C drift watch for the cursor adapter.

Compares a frozen baseline rung against a fresh capture across the standard
drift arms (raw native surface, container surface, trace surface, contract
cells), plus the notes below.

  • CELL_SIGNALS — the contract cells cursor can structurally reach. cursor
                   exposes DISTINCT typed tools (read / edit / shell / glob /
                   createPlan / switchMode) rather than routing everything
                   through a shell, so unlike aider it DOES reach cell 5 (READ)
                   through a real read tool. Reachable set: 1,2,3,4,5,6,7,8,26.
                   No web / subagent / lineage / hook / MCP channels observed.

  • NO VERSION ARM — and this is a genuine gap, not an omission. aider carries
                   its release version in-band on the transcript banner and
                   codex carries it in session_meta, so for those harnesses a
                   version arm doubles as capture verification: an unchanged
                   version across a supposed release delta proves the candidate
                   was captured with the wrong binary. cursor's stream carries
                   NO version string anywhere. The version reaches the trace
                   only via framework-meta.json, which the capture harness
                   writes from the image tag. A mislabelled capture is therefore
                   NOT detectable from the artefact. See drift-ledger.yaml
                   limitation `no-inband-version`.

                   Partial compensation: the tool_call envelope fields
                   (startedAtMs / completedAtMs / toolCallId) are absent at
                   2026.06.04-5fd875e and present at 2026.07.23-e383d2b, so the
                   raw-surface arm surfaces that particular delta as new paths
                   without needing a version string. That is evidence of A
                   change, not proof of WHICH version.

Inputs are cursor-agent `--output-format stream-json` captures scoped to the
conformance scenario (basename inputs/<name>.stream.jsonl). Both the raw surface
and the preprocess path consume the same file.

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
PREPROCESS = ADAPTER_DIR / "preprocess_cursor.py"
DSL = ADAPTER_DIR / "cursor.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
sys.path.insert(0, str(ADAPTER_DIR))
from drift_core import (  # noqa: E402
    DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)
from preprocess_cursor import tool_kind_of  # noqa: E402


# --- cursor stream raw surface ----------------------------------------------

def read_stream(path: pathlib.Path) -> list[dict]:
    """Parse a cursor stream-json capture into structural records for signal-0.

    Emits the shape of each event rather than its content: the event type, and
    for tool calls the tool kind plus which envelope and result keys are
    present. A native-vocabulary change — a new event type, a new tool kind, a
    new envelope field such as the startedAtMs/completedAtMs pair that arrived
    between the two frozen rungs — surfaces as a new path.

    Key sets are emitted as PRESENCE MAPS ({"startedAtMs": true}), never as
    lists of key names. A list would make a new key a new list VALUE, and the
    path walker compares PATHS: the four fields that arrived between the two
    frozen rungs were invisible to this arm until the maps replaced the lists.
    """
    def presence(d: dict, predicate=lambda k: True) -> dict:
        return {k: True for k in d if predicate(k)}

    recs: list[dict] = []
    for raw in re.split(r"\r\n|\r|\n", path.read_text(encoding="utf-8")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            recs.append({"type": "unparseable"})
            continue
        etype = ev.get("type")
        rec: dict = {"type": etype, "subtype": ev.get("subtype")}
        if etype == "system":
            rec["init"] = presence(ev, lambda k: k not in ("type", "subtype"))
        elif etype == "tool_call":
            tc = ev.get("tool_call") or {}
            kind = tool_kind_of(tc)
            rec["tool_kind"] = kind
            rec["envelope"] = presence(tc, lambda k: not k.endswith("ToolCall"))
            payload = tc.get(kind) if kind else None
            if isinstance(payload, dict):
                rec["payload"] = presence(payload)
                result = payload.get("result")
                if isinstance(result, dict):
                    rec["result"] = presence(result)
                args = payload.get("args")
                if isinstance(args, dict):
                    rec["args"] = presence(args)
        elif etype == "interaction_query":
            rec["query_type"] = ev.get("query_type")
        elif etype == "result":
            rec["result"] = presence(ev)
        recs.append(rec)
    return recs


# --- Cell -> structural-signal extractor (cursor) ----------------------------

def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


CELL_SIGNALS = {
    1: lambda t: any(tn.get("role") == "user" for tn in t.get("turns") or []),
    2: lambda t: any(tn.get("role") == "assistant" for tn in t.get("turns") or []),
    3: lambda t: bool(t.get("tool_calls")),
    # cell 4: cursor tool calls carry content_origin "local_exec" (shell output,
    # produced by execution) or "model_echo" (edit content, produced by the
    # model). Predicate is "any truthy content_origin", matching the aider and
    # codex footnote-[1] handling.
    4: lambda t: any((tc.get("output") or {}).get("content_origin")
                     for tc in t.get("tool_calls") or []),
    5: lambda t: "READ" in _ops(t),
    6: lambda t: "MODIFY" in _ops(t),
    7: lambda t: "NEW" in _ops(t),
    8: lambda t: "EXECUTE" in _ops(t),
    26: lambda t: any((t.get("metrics") or {}).get(k, 0)
                      for k in ("total_input_tokens", "total_output_tokens")),
}


def fired_cells(traces: list[dict]) -> set[int]:
    """Cells that fired across a scenario run (cursor CELL_SIGNALS). Module
    export so a manifest generator derives annotated_cells_fired from the same
    predicates the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for cursor")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.stream.jsonl captures; "
                         "omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="cursor",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        extra_arms=[],
        ledger=ROOT / "drift-ledger.yaml",
        raw_reader=read_stream,
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
