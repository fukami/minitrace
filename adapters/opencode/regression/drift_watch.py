#!/usr/bin/env python3
"""Axis-C drift watch for the opencode conformance unit (harness #3).

Thin per-harness wiring over `adapters/drift_core.py`, the opencode sibling of the
claude-code and goose watchers. The generic signals and the run loop live in the
core; this file supplies only what is opencode-specific:

  • raw_reader   — opencode's raw ground truth is a SQLite DB (tables `session`,
                   `message`, `part`), not JSONL. The reader returns the same
                   {type: ...}-tagged record dicts the rest of the core expects:
                   one `session` record per sessions row, one `message` per
                   messages row, one `part` per parts row (matching
                   `format-reference/session-container-run.jsonl`). It
                   de-stringifies the `data` blob column on message/part rows so
                   signal 0 sees the message/part content structure, not an
                   opaque string.
  • CELL_SIGNALS — the contract cells opencode can structurally reach. Unlike
                   goose, opencode HAS web tools (webfetch/fetch → cell 11) and
                   subagents (task → DELEGATE + delegates-to/delegated-by lineage
                   → cells 9/12/13), so its reachable set is the fuller
                   claude-code-shaped one, minus the claude-code-only channels
                   (framework_control input, hooks, command-permission
                   attachments, MCP-server metadata) that opencode has no surface
                   for. Tool vocabulary (from opencode.dsl.yaml operation_types):
                   read/glob/grep/question=READ, webfetch/fetch=READ(web),
                   write=NEW, edit/todowrite=MODIFY, bash=EXECUTE, task=DELEGATE,
                   skill/invalid/default=OTHER.
  • version arm  — opencode's analog of goose's schema_version arm. opencode
                   carries its release version IN-BAND as `session.version`
                   (a semver: 1.2.20, 1.14.40, 1.17.15, ...). A change vs the
                   baseline is a storage-format-evolution signal (WARNING): the
                   preprocessor schema-dispatches on this value, so a new version
                   may cross a format break (snapshot/patch removal after 1.2.20,
                   the session_message/event/event_sequence tables added at
                   1.14.40) that no path/record-type diff alone would name.

Inputs are the harvested opencode `.db` files scoped to the conformance scenario
(see frameworks/opencode/ once built); the manifest declares `input:
inputs/<name>.db`, which both this watcher's raw_reader and preprocess_opencode.py
consume.

Run (deps must be importable — use uv):
    uv run --with pyyaml --with rfc8785 python drift_watch.py \
        --baseline <ver> --candidate-inputs /path/to/capture/inputs --operator-type <type>
Exit 0 = no drift; exit 1 = drift; exit 2 = harness/usage error.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ADAPTER_DIR = (ROOT / "..").resolve()
ADAPTERS_ROOT = (ADAPTER_DIR / "..").resolve()
PREPROCESS = ADAPTER_DIR / "preprocess_opencode.py"
DSL = ADAPTER_DIR / "opencode.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from sqlite_source import open_evidence_db  # noqa: E402
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- opencode raw reader (SQLite DB -> type-tagged records) ------------------

def _parse_blob(value):
    """Best-effort de-stringify an opencode JSON blob column; keep the raw string
    on failure so a malformed blob never crashes the raw-surface walk."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def opencode_raw_reader(db_path: pathlib.Path) -> list[dict]:
    """One `session` record per session row, one `message` per message row, one
    `part` per part row, matching the checked-in format-reference. The message and
    part `data` blobs are parsed so signal 0 sees their internal structure (a new
    content-part field is real format drift, not an opaque string). Parts are the
    richest surface: a new part `type` or a new field inside `data` is exactly the
    Axis-C signal this reader exists to expose."""
    con = open_evidence_db(db_path, tables=("session", "message", "part"))
    con.row_factory = sqlite3.Row
    recs: list[dict] = []
    try:
        for r in con.execute("SELECT * FROM session ORDER BY time_created, id"):
            recs.append({"type": "session", **dict(r)})
        for r in con.execute("SELECT * FROM message ORDER BY session_id, time_created, id"):
            d = dict(r)
            if "data" in d:
                d["data"] = _parse_blob(d["data"])
            recs.append({"type": "message", **d})
        for r in con.execute("SELECT * FROM part ORDER BY session_id, message_id, time_created, id"):
            d = dict(r)
            if "data" in d:
                d["data"] = _parse_blob(d["data"])
            recs.append({"type": "part", **d})
    finally:
        con.close()
    return recs


def _session_version(db_path: pathlib.Path):
    """The opencode release version, carried in-band on the session row. A single
    conformance-scenario db holds one session, so MAX is exact; if a candidate ever
    mixed versions this surfaces the newest, which is the drift-relevant one."""
    try:
        con = open_evidence_db(db_path, tables=("session", "message", "part"))
        try:
            row = con.execute("SELECT MAX(version) FROM session").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def version_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    """WARNING on an in-band session.version change vs baseline — opencode's
    storage-format-evolution signal (the analog of goose's schema_version arm). A
    bump is not fatal: the drift is real, but whether the adapter still parses the
    new version is the operator's call. preprocess_opencode.py schema-dispatches on
    this value (< 1.14.0 keeps snapshot/patch parts; >= 1.14.40 gains the
    session_message/event/event_sequence tables), so confirm the dispatch still
    covers the candidate."""
    base = _session_version(ctx.base_input)
    cand = _session_version(ctx.cand_input)
    if base != cand:
        return [], [f"opencode session.version changed vs baseline: "
                    f"{base} -> {cand} (in-band release/format evolution; confirm "
                    "preprocess_opencode.py schema dispatch still covers the "
                    "candidate version)"]
    return [], []


# --- Cell -> structural-signal extractor (opencode) -------------------------
# Same minitrace contract cells as claude-code, restricted to what opencode can
# structurally reach. Predicates read the emitted trace only. opencode tool
# vocabulary (opencode.dsl.yaml operation_types): read/glob/grep/question=READ,
# webfetch/fetch=READ(web), write=NEW, edit/todowrite=MODIFY, bash=EXECUTE,
# task=DELEGATE, skill/invalid/default=OTHER. opencode HAS web + subagents (so
# cells 11/9/12/13 are reachable, unlike goose) but has no framework_control
# input channel, no hooks, no command-permission attachments, and no MCP-server
# tool metadata (so cells 14/16/17/20/27 are absent, unlike claude-code).

# Web tools carry operation_type READ, so a plain "READ in ops" test lets them
# masquerade as cell 5 (filesystem read). Cell 5 fires only when READ comes from a
# NON-web tool; the web READ is cell 11's.
_WEB_TOOLS = {"webfetch", "fetch"}


def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


def _tool_names(trace: dict) -> set[str]:
    return {tc.get("tool_name") for tc in trace.get("tool_calls") or []}


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
    11: lambda t: bool(_tool_names(t) & _WEB_TOOLS),
    12: lambda t: "delegates-to" in _lineage_types(t),
    13: lambda t: "delegated-by" in _lineage_types(t),
    26: lambda t: any((t.get("metrics") or {}).get(k, 0)
                      for k in ("total_input_tokens", "total_output_tokens")),
}


def fired_cells(traces: list[dict]) -> set[int]:
    """Cells that fired across a scenario run (opencode CELL_SIGNALS). Module export
    so a manifest generator derives annotated_cells_fired from the same predicates
    the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for opencode")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/ (no default: opencode "
                         "has no anchor rung until the first host capture is frozen)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.db captures; omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="opencode",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        raw_reader=opencode_raw_reader,
        extra_arms=[version_arm],
        ledger=ROOT / "drift-ledger.yaml",
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
