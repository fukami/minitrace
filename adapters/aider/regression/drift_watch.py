#!/usr/bin/env python3
"""Axis-C drift watch for the aider conformance unit (harness #7 / conditional).

Thin per-harness wiring over `adapters/drift_core.py`, the aider sibling of the
claude-code / codex / goose / opencode / pi / droid watchers. Generic signals
and the run loop live in the core; this file supplies only what is aider-specific:

  • raw_reader   — aider has no JSONL/SQLite native log; its complete source is
                   the MARKDOWN transcript (.aider.chat.history.md). The default
                   read_jsonl reader is blind to it, so this watcher supplies a
                   markdown raw_reader that parses the transcript into structured
                   records (a banner meta record + one per turn) so signal-0 can
                   still see native-vocabulary drift (a new console marker, a
                   changed thinking tag, a new edit op).
  • CELL_SIGNALS — the contract cells aider can structurally reach. aider's tool
                   surface is whole-file edits (model_echo NEW/MODIFY) plus an
                   optional shell command (EXECUTE). No discrete READ tool
                   (files enter via the repo-map, not a tool call), no web /
                   subagent / lineage / hook / MCP channels. Reachable set:
                   1,2,3,4,6,7,8,26 (cell 5 READ and cell 10 OTHER are not
                   reachable by the edit surface).
  • version arm  — aider carries its RELEASE version in-band on the transcript
                   banner (`> Aider v<version>`). Like codex's cli_version this
                   changes on every release, so across a release delta the arm
                   WARNING is EXPECTED and doubles as capture verification: an
                   unchanged version across a supposed release delta means the
                   candidate was captured with the wrong binary.

Inputs are aider `.chat.history.md` transcripts scoped to the conformance
scenario (basename inputs/<name>.chat.history.md). Both the raw surface and the
preprocess path consume the same file.

Run (deps must be importable — use the adapter venv or uv):
    ../../../.venv/bin/python drift_watch.py \
        --baseline <ver> --candidate-inputs /path/to/capture/inputs --operator-type <type>
Exit 0 = no drift; exit 1 = drift; exit 2 = harness/usage error.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ADAPTER_DIR = (ROOT / "..").resolve()
ADAPTERS_ROOT = (ADAPTER_DIR / "..").resolve()
PREPROCESS = ADAPTER_DIR / "preprocess_aider.py"
DSL = ADAPTER_DIR / "aider.dsl.yaml"
DSL_RUNNER = ADAPTERS_ROOT / "dsl_runner.py"

sys.path.insert(0, str(ADAPTERS_ROOT))
sys.path.insert(0, str(ADAPTER_DIR))
from drift_core import (  # noqa: E402
    ArmContext, DriftConfig, fired_cells as _core_fired_cells, run_drift,
    add_operator_arguments, operator_args_from,
)
from preprocess_aider import parse_transcript  # noqa: E402


# --- aider markdown raw surface ---------------------------------------------

def read_transcript(path: pathlib.Path) -> list[dict]:
    """Parse an aider .chat.history.md into structured records for signal-0.

    Emits one `meta` record (banner: version, model, edit_format) and one
    `turn` record per turn with its structural shape (role, edit ops, whether
    thinking / tokens are present). A native-vocabulary change — a new banner
    field, a new edit op, a changed thinking marker — surfaces as a new path.
    """
    parsed = parse_transcript(path.read_text(encoding="utf-8"))
    recs: list[dict] = [{"type": "meta", **parsed["meta"]}]
    for t in parsed["turns"]:
        recs.append({
            "type": "turn",
            "role": t.get("role"),
            "has_thinking": bool(t.get("thinking")),
            "has_tokens": bool(t.get("tokens")),
            "edit_ops": sorted({e["op"] for e in (t.get("edits") or [])}),
            "n_edits": len(t.get("edits") or []),
        })
    return recs


def _aider_version(path: pathlib.Path):
    """The aider RELEASE version, in-band on the transcript banner."""
    try:
        return parse_transcript(path.read_text(encoding="utf-8"))["meta"].get("version")
    except OSError:
        return None


def version_arm(ctx: ArmContext) -> tuple[list[str], list[str]]:
    base = _aider_version(ctx.base_input)
    cand = _aider_version(ctx.cand_input)
    if base != cand:
        return [], [f"aider banner version (in-band RELEASE version) changed vs "
                    f"baseline: {base} -> {cand} (expected on a release delta; if "
                    "this run was meant to be same-version, the capture used the "
                    "wrong binary)"]
    return [], []


# --- Cell -> structural-signal extractor (aider) -----------------------------

def _ops(trace: dict) -> set[str]:
    return {tc.get("operation_type") for tc in trace.get("tool_calls") or []}


CELL_SIGNALS = {
    1: lambda t: any(tn.get("role") == "user" for tn in t.get("turns") or []),
    2: lambda t: any(tn.get("role") == "assistant" for tn in t.get("turns") or []),
    3: lambda t: bool(t.get("tool_calls")),
    # cell 4: aider edits carry content_origin == "model_echo" (the model
    # echoes the whole file), never the "tool_output" literal — so the aider
    # predicate is "any truthy content_origin", matching codex's footnote-[1]
    # handling.
    4: lambda t: any((tc.get("output") or {}).get("content_origin")
                     for tc in t.get("tool_calls") or []),
    6: lambda t: "MODIFY" in _ops(t),
    7: lambda t: "NEW" in _ops(t),
    8: lambda t: "EXECUTE" in _ops(t),
    26: lambda t: any((t.get("metrics") or {}).get(k, 0)
                      for k in ("total_input_tokens", "total_output_tokens")),
}


def fired_cells(traces: list[dict]) -> set[int]:
    """Cells that fired across a scenario run (aider CELL_SIGNALS). Module export
    so a manifest generator derives annotated_cells_fired from the same
    predicates the self-test asserts against."""
    return _core_fired_cells(traces, CELL_SIGNALS)


# --- Main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Axis-C drift watch for aider")
    ap.add_argument("--baseline", required=True,
                    help="frozen version dir under regression/")
    ap.add_argument("--candidate-inputs", type=pathlib.Path, default=None,
                    help="dir of fresh <scenario>.chat.history.md captures; "
                         "omit to self-test")
    add_operator_arguments(ap)
    args = ap.parse_args()

    cfg = DriftConfig(
        operator_args=operator_args_from(args),
        harness_name="aider",
        preprocess=PREPROCESS,
        dsl=DSL,
        dsl_runner=DSL_RUNNER,
        run_cwd=ROOT,
        cell_signals=CELL_SIGNALS,
        extra_arms=[version_arm],
        ledger=ROOT / "drift-ledger.yaml",
        raw_reader=read_transcript,
    )
    return run_drift(ROOT / args.baseline, args.candidate_inputs, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
