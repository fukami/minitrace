#!/usr/bin/env python3
"""Axis-C drift watch for the goose conformance unit (harness #2).

Thin per-harness wiring over `adapters/drift_core.py`, the goose sibling of
`adapters/claude-code/regression/drift_watch.py`. The generic signals and the run
loop live in the core; this file supplies only what is goose-specific:

  • raw_reader   — goose's raw ground truth is a SQLite `sessions.db`, not JSONL.
                   The reader returns the same {type: ...}-tagged record dicts the
                   rest of the core expects: one `session` record per `sessions`
                   row and one `message` record per `messages` row (matching
                   `format-reference/session-v3.27.0.jsonl`). It de-stringifies the
                   `content_json` / `metadata_json` blob columns so signal 0 sees
                   the message-content structure, not an opaque string.
  • CELL_SIGNALS — the contract cells goose can structurally reach. goose has no
                   web tools, no subagents, and no attachment/hook channels, so the
                   delegation / web / attachment / hook cells are absent; its tool
                   vocabulary is shell(EXECUTE) / edit(MODIFY) / write(NEW) /
                   read,tree(READ) / many(OTHER).
  • schema arm   — goose's analog of claude-code's subtype-rename arm: a change in
                   the sessions.db `schema_version` is a storage-format evolution
                   signal (WARNING) that no path/record-type diff would catch.

Inputs are the harvested `sessions.db` files (see frameworks/goose/capture_cycle.sh);
the manifest declares `input: inputs/<name>.db`, which both this watcher's
raw_reader and `preprocess_goose.py` consume.

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
PREPROCESS = ADAPTER_DIR / "preprocess_goose.py"
DSL = ADAPTER_DIR / "goose.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
from sqlite_source import open_evidence_db  # noqa: E402
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)


# --- goose raw reader (SQLite sessions.db -> type-tagged records) ------------

def _parse_blob(value):
    """Best-effort de-stringify a goose JSON blob column; keep the raw string on
    failure so a malformed blob never crashes the raw-surface walk."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def goose_raw_reader(db_path: pathlib.Path) -> list[dict]:
    """One `session` record per sessions row, one `message` record per messages
    row, matching the checked-in format-reference. The `content_json` /
    `metadata_json` blobs are parsed so signal 0 sees their internal structure
    (a new content-block field is real format drift, not an opaque string)."""
    con = open_evidence_db(db_path, tables=("schema_version", "sessions", "messages"))
    con.row_factory = sqlite3.Row
    recs: list[dict] = []
    try:
        for r in con.execute("SELECT * FROM sessions ORDER BY created_at, id"):
            d = dict(r)
            if "model_config_json" in d:
                d["model_config_json"] = _parse_blob(d["model_config_json"])
            recs.append({"type": "session", **d})
        for r in con.execute("SELECT * FROM messages ORDER BY session_id, id"):
            d = dict(r)
            for blob in ("content_json", "metadata_json"):
                if blob in d:
                    d[blob] = _parse_blob(d[blob])
            recs.append({"type": "message", **d})
    finally:
        con.close()
    return recs


def _schema_version(db_path: pathlib.Path):
    try:
        con = open_evidence_db(db_path, tables=("schema_version", "sessions", "messages"))
        try:
            row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def schema_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    """WARNING on a sessions.db schema_version change — goose's storage-format
    evolution signal (the analog of claude-code's attachment-subtype rename). A
    bump is not fatal here: the drift is real, but whether the adapter still parses
    the new schema is the operator's call (preprocess_goose supports v7/v11)."""
    base = _schema_version(ctx.base_input)
    cand = _schema_version(ctx.cand_input)
    if base != cand:
        return [], [f"goose sessions.db schema_version changed vs baseline: "
                    f"{base} -> {cand} (storage-format evolution; confirm "
                    "preprocess_goose.py still parses the candidate schema)"]
    return [], []


# --- Cell -> structural-signal extractor (goose) ----------------------------
# Same minitrace contract cells as claude-code, restricted to what goose can
# structurally reach. Predicates read the emitted trace only. goose tool
# vocabulary (from the archived goose corpus): shell=EXECUTE, edit=MODIFY,
# write=NEW, read/read_file/tree=READ, everything else=OTHER. No web tools (so no
# cell 11 and no web-exclusion on cell 5), no subagents (no 9/12/13), no
# attachment/hook channels (no 14/16/17/20).

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
    """Cells that fired across a scenario run (goose CELL_SIGNALS). Module export
    so a manifest generator derives annotated_cells_fired from the same predicates
    the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for goose")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/ (no default: goose "
                         "has no anchor rung until the first host capture is frozen)")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.db captures; omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="goose",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        raw_reader=goose_raw_reader,
        extra_arms=[schema_arm],
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
