#!/usr/bin/env python3
"""inspect-swe .eval -> minitrace-DSL container preprocessor.

Reads an inspect-swe `.eval` log (zstd-zip via inspect_ai.log.read_eval_log)
and emits a JSON array of containers, one per sample. Each container holds
the data for two minitrace traces emitted by the inspect-swe DSL
(inspect-swe.dsl.yaml, in the development tree only; not shipped in the
release, see the top-level README):

  - outer: inspect-swe harness view (inspect-ai event stream — bridge
           ModelEvent, SandboxEvent, SpanBegin/End, Compaction, ...).
  - inner: the wrapped CLI's native conversation, parsed from the
           per-sample `ClaudeCodeDebug:stdout` store entry (claude-code
           stream-json, one JSON message per line).

Outer and inner are linked via `lineage.wraps` / `lineage.wrapped-by`
edges (resolved post-emission by `cross_trace.py`) per cycle 13 scoping
note I1 (double-trace shape).

Design decisions I1–I8, from the inspect-swe scoping and the phase-1 corpus:

  I1: double-trace adapter, outer + inner, linked by wraps/wrapped-by.
  I2: inner-trace source = `ClaudeCodeDebug:stdout` (corpus-config gate:
      `claude_code(debug=True, ...)`). When `debug=False` is run, stdout
      is consumed by `annotate_agent_spans()` and discarded; this
      preprocessor MUST emit a loud coverage diagnostic in that case.
  I3: structural 1:1 pairing inside the .eval container. Multi-attempt
      retries (`retry_uncaught_errors` fired) produce `stdout: list[str]`
      with len>1; LAST attempt is canonical, prior attempts become
      `ext:inspect_swe:retry_attempt` annotations. The current
      smoke corpus is len=1 across all 4 samples, so the multi-attempt
      path is shape conjecture until exercised.
  I6: bridge ModelEvents preserved verbatim on the outer trace
      (cross-validation primitive against the inner trace's
      self-reported model calls).
  I7: SandboxEvents preserved verbatim on the outer trace; DSL will
      project them as `ext:inspect_ai:sandbox_event` annotations.

Usage:
    python preprocess_inspect_swe.py <input.eval> <output.json> [--sample-id ID]

Requires `inspect_ai` (read_eval_log + event model_dump). Run inside a
venv that has it installed.

Conforms to minitrace-spec v0.3.0 + minitrace-adapter-dsl-spec v0.1.0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_reader import jsonl_text_lines  # noqa: E402

try:
    from inspect_ai.log import read_eval_log  # type: ignore[import-not-found]
except ImportError as exc:
    print(
        "error: `inspect_ai` is required to read inspect-swe .eval files.\n"
        "  Run inside a venv that has it installed,\n"
        "  or install with `pip install inspect-ai`.",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


# ──────────────────────────────────────────────────────────────────────
# Outer-trace event taxonomy (inspect-ai 21-event set, cycle-12 precedent)
# ──────────────────────────────────────────────────────────────────────

# Source-defined 21-event taxonomy per cycle-12 source survey §5.
# Unknown event types passthrough with a coverage diagnostic.
KNOWN_INSPECT_EVENT_TYPES = frozenset({
    "anchor", "approval", "branch", "compaction", "error", "info",
    "input", "logger", "model", "sample_init", "sample_limit",
    "sandbox", "score", "score_edit", "span_begin", "span_end",
    "state", "step", "store", "subtask", "tool",
})


# ──────────────────────────────────────────────────────────────────────
# Inner-trace (claude-code stream-json) classification
# ──────────────────────────────────────────────────────────────────────

# Top-level `type` field values observed in claude-code's
# `--output-format stream-json` stdout. Confirmed on the phase-1 corpus
# (echo / file_read / multi_step probes, 2026-05-15).
KNOWN_STREAMJSON_TYPES = frozenset({
    "system",     # subtype: init / status / hook / ...
    "assistant",  # message.content[] of text/thinking/tool_use
    "user",       # tool_result content (also: prompt continuations)
    "result",     # final summary message; not present in on-disk JSONL
})


def classify_inner_event_kind(record: dict) -> str:
    """Assign a stable event_kind discriminator to one stream-json line.

    Mirrors cycle 5's claude-code preprocessor classification at coarse
    granularity — the inner-DSL author (chunk C) decides whether to
    reshape further. Per scoping-note I2: stream-json stdout is
    structurally equivalent to claude-code's on-disk JSONL, but the
    stream-json variant adds a final `type=result` message that JSONL
    does not have.
    """
    rtype = record.get("type")
    if rtype == "system":
        sub = record.get("subtype") or "event"
        return f"system_{sub}"
    if rtype == "assistant":
        return "assistant_turn"
    if rtype == "user":
        msg = record.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    return "user_tool_result"
        return "user_prompt"
    if rtype == "result":
        sub = record.get("subtype") or "summary"
        return f"result_{sub}"
    return f"unknown:{rtype}"


def parse_inner_stream(stdout_blob: str) -> list[dict]:
    """Parse one stdout blob (multi-line stream-json) into event dicts.

    Each non-empty line is one stream-json message; JSON-decoded with
    an added `event_kind` discriminator. Empty lines skipped. Malformed
    lines surface as `{event_kind: parse_error, raw_line, error}`
    entries rather than aborting; downstream diagnostics flag them.
    """
    events: list[dict] = []
    # Framed as the readers frame jsonl (DSL rc3 §15): at CR too,
    # and a line of VT, FF or Unicode spaces is not empty but malformed.
    for _, stripped in jsonl_text_lines(stdout_blob):
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError as exc:
            events.append({
                "event_kind": "parse_error",
                "raw_line": stripped,
                "error": str(exc),
            })
            continue
        if isinstance(ev, dict):
            ev["event_kind"] = classify_inner_event_kind(ev)
            events.append(ev)
        else:
            events.append({
                "event_kind": "parse_error",
                "raw_line": stripped,
                "error": f"non-object top-level value: {type(ev).__name__}",
            })
    return events


def find_claude_code_version(inner_events: list[dict]) -> str | None:
    """Extract `claude_code_version` from the inner trace's `system init` line.

    The first stream-json message is always `{"type":"system","subtype":"init",...}`
    and carries `claude_code_version` among its fields. Returns None if no
    init line is present (e.g. empty stdout, parse errors, or future
    stream-json shape change).
    """
    for ev in inner_events:
        if ev.get("event_kind") == "system_init":
            v = ev.get("claude_code_version")
            if isinstance(v, str):
                return v
    return None


# ──────────────────────────────────────────────────────────────────────
# Diagnostics
# ──────────────────────────────────────────────────────────────────────

def collect_outer_diagnostics(outer_events: list[dict]) -> list[str]:
    """Surface unknown inspect-ai event types per cycle-12 precedent."""
    seen: set[str] = set()
    diags: list[str] = []
    for ev in outer_events:
        et = ev.get("event")
        if isinstance(et, str) and et not in KNOWN_INSPECT_EVENT_TYPES and et not in seen:
            seen.add(et)
            diags.append(
                f"unknown_outer_event_type: {et!r} (passthrough as "
                f"ext:inspect_ai:event:{et} per cycle-12 source-survey §5)"
            )
    return diags


def collect_inner_diagnostics(inner_events: list[dict]) -> list[str]:
    """Surface unknown stream-json top-level types or parse errors."""
    seen_kind: set[str] = set()
    diags: list[str] = []
    parse_errors = 0
    for ev in inner_events:
        ek = ev.get("event_kind", "")
        if ek == "parse_error":
            parse_errors += 1
            continue
        rtype = ev.get("type")
        if isinstance(rtype, str) and rtype not in KNOWN_STREAMJSON_TYPES and rtype not in seen_kind:
            seen_kind.add(rtype)
            diags.append(
                f"unknown_inner_streamjson_type: {rtype!r} "
                f"(passthrough as event_kind=unknown:{rtype})"
            )
    if parse_errors:
        diags.append(
            f"inner_stream_parse_errors: {parse_errors} line(s) failed "
            f"JSON decode; surfaced as event_kind=parse_error entries."
        )
    return diags


# ──────────────────────────────────────────────────────────────────────
# Outer-trace projection
# ──────────────────────────────────────────────────────────────────────

def project_outer_event(event_dict: dict) -> dict:
    """Project one inspect-ai event dict; flag unknown types.

    `event_dict` is the JSON-mode `model_dump()` of a pydantic event from
    inspect_ai.log.transcript. We preserve all fields verbatim (per A26)
    and add `_unknown_event_type: true` when the discriminator is
    outside the cycle-12 taxonomy.
    """
    out = dict(event_dict)
    et = event_dict.get("event")
    if et not in KNOWN_INSPECT_EVENT_TYPES:
        out["_unknown_event_type"] = True
    return out


# ──────────────────────────────────────────────────────────────────────
# Per-sample container build
# ──────────────────────────────────────────────────────────────────────

def build_sample_container(sample: Any, eval_meta: dict) -> dict:
    """Build a two-trace container for one inspect-swe sample.

    The container is the canonical events container consumed by
    the inspect-swe DSL. It carries both the outer_trace (inspect-ai
    event stream) and inner_trace (parsed claude-code stream-json) plus
    a unified `dsl_events` flat stream tagged with `_session` markers
    for DSL multi-session dispatch.
    """
    sample_id = sample.id
    epoch = sample.epoch
    sample_uuid = getattr(sample, "uuid", None)

    # ── Outer trace: inspect-ai events (verbatim, A26) ──────────────────
    raw_outer_events = [
        ev.model_dump(mode="json") for ev in (sample.events or [])
    ]
    outer_events = [project_outer_event(ev) for ev in raw_outer_events]
    outer_diags = collect_outer_diagnostics(raw_outer_events)

    # ── Wrapped-agent span bounds ────────────────────────────────────────
    # Claude Code's stream-json `system` and `result` lines carry no
    # timestamp, but minitrace-spec §3.5 requires one on an annotation.
    # The harness's `agent` span brackets the wrapped agent: its begin is
    # when Claude Code was started (init is its first line), its end when
    # the harness saw it finish (result is its last). The DSL falls back
    # to these when a record has no timestamp of its own.
    agent_begin = next(
        (ev for ev in raw_outer_events
         if ev.get("event") == "span_begin" and ev.get("type") == "agent"),
        None,
    )
    agent_end = next(
        (ev for ev in raw_outer_events
         if agent_begin is not None and ev.get("event") == "span_end"
         and ev.get("id") == agent_begin.get("id")),
        None,
    )
    if agent_begin is None or agent_end is None:
        outer_diags.append(
            f"agent_span_bounds_missing: begin={agent_begin is not None} "
            f"end={agent_end is not None} for sample_id={sample_id!r}; "
            f"inner annotations without a timestamp of their own get none"
        )

    # ── Inner trace: ClaudeCodeDebug:stdout ─────────────────────────────
    # The inspect-ai store namespaces ClaudeCodeDebug fields with a
    # `ClaudeCodeDebug:` prefix (verified phase-1 corpus 2026-05-15).
    store = sample.store or {}
    stdout_list = store.get("ClaudeCodeDebug:stdout", []) or []

    inner_diags: list[str] = []
    inner_events: list[dict] = []
    retry_attempts: list[dict] = []

    if not isinstance(stdout_list, list) or len(stdout_list) == 0:
        # P3 LOUD COVERAGE DIAGNOSTIC per scoping-note §"Corpus generation
        # requirements". Inner-trace capture path is gated on
        # `claude_code(debug=True, ...)`; missing stdout means the corpus
        # was generated without that knob set and the inner trace cannot
        # be reconstructed. Do NOT silently fall through.
        msg = (
            f"P3_COVERAGE_ERROR: ClaudeCodeDebug:stdout is empty for "
            f"sample_id={sample_id!r} epoch={epoch}. Inner-trace capture "
            f"failed. Likely cause: corpus generated without "
            f"`claude_code(debug=True, ...)`. See scoping note "
            f"§'Corpus generation requirements'."
        )
        inner_diags.append(msg)
        print(f"warning: {msg}", file=sys.stderr)
    else:
        # I3: last attempt promoted to canonical inner trace; prior
        # attempts (if any) emitted as ext:inspect_swe:retry_attempt
        # annotations at session scope. Current corpus is len=1 across
        # all 4 samples; multi-attempt path is shape conjecture per I3.
        canonical_blob = stdout_list[-1]
        if isinstance(canonical_blob, str):
            inner_events = parse_inner_stream(canonical_blob)
        else:
            inner_diags.append(
                f"inner_trace_type_error: stdout[-1] is not a string "
                f"(got {type(canonical_blob).__name__}); sample_id={sample_id!r}"
            )

        for i, blob in enumerate(stdout_list[:-1]):
            retry_attempts.append({
                "attempt_index": i,
                "raw_stdout": blob if isinstance(blob, str) else None,
                "parsed_events": (
                    parse_inner_stream(blob) if isinstance(blob, str) else []
                ),
            })

        if len(stdout_list) > 1:
            inner_diags.append(
                f"multi_attempt_conjecture: ClaudeCodeDebug:stdout has "
                f"{len(stdout_list)} attempts for sample_id={sample_id!r}. "
                f"Last attempt promoted to canonical inner trace; prior "
                f"attempts emitted as ext:inspect_swe:retry_attempt "
                f"annotations per scoping-note I3. SHAPE CONJECTURE: "
                f"this code path is untested by the phase-1 smoke corpus "
                f"(2026-05-15, all samples len=1) and may need "
                f"adjustment on first real exercise."
            )

        inner_diags.extend(collect_inner_diagnostics(inner_events))

    claude_code_version = find_claude_code_version(inner_events)

    # ── Trace IDs (lineage placeholders point here) ─────────────────────
    # DSL emits actual lineage edges using these IDs in
    # ${trace:<id>:content_hash} placeholders, resolved post-emission
    # by cross_trace.back_fill_cross_trace_references.
    base_id = sample_uuid or f"{sample_id}-{epoch}"
    outer_trace_id = f"inspect-swe-outer-{base_id}"
    inner_trace_id = f"inspect-swe-inner-{base_id}"

    outer_trace = {
        "trace_id": outer_trace_id,
        "trace_type": "agent-session",
        "actor": "outer",
        "producer_namespace": "inspect_swe",
        "events": outer_events,
    }
    inner_trace = {
        "trace_id": inner_trace_id,
        "trace_type": "agent-session",
        "actor": "inner",
        "producer_namespace": "claude_code",
        "claude_code_version": claude_code_version,
        "events": inner_events,
        "retry_attempts": retry_attempts,
    }

    # ── Unified DSL events stream (multi-session dispatch) ──────────────
    # dsl_runner.py walks one record_path per container. Each entry
    # carries a `_session: "outer"|"inner"` marker; the DSL dispatches
    # via `when: _session == "outer"` (or "inner") predicates plus
    # event-kind conditions. Retry-attempt events get an additional
    # `_retry_attempt_index` marker so the inner DSL can emit them as
    # session-level annotations rather than canonical turns.
    dsl_events: list[dict] = []
    for ev in outer_events:
        e = dict(ev)
        e["_session"] = "outer"
        e["_retry_attempt_index"] = None
        dsl_events.append(e)
    for ev in inner_events:
        e = dict(ev)
        e["_session"] = "inner"
        e["_retry_attempt_index"] = None
        dsl_events.append(e)
    for attempt in retry_attempts:
        for ev in attempt["parsed_events"]:
            e = dict(ev)
            e["_session"] = "inner"
            e["_retry_attempt_index"] = attempt["attempt_index"]
            dsl_events.append(e)

    container = {
        "sample_id": sample_id,
        "epoch": epoch,
        "sample_uuid": sample_uuid,
        "input": sample.input,
        # Eval-level provenance (carried for ext:inspect_ai:* / harness
        # annotations downstream).
        "eval_id": eval_meta.get("eval_id"),
        "run_id": eval_meta.get("run_id"),
        "task_id": eval_meta.get("task_id"),
        "task_name": eval_meta.get("task_name"),
        "task_version": eval_meta.get("task_version"),
        "model_eval": eval_meta.get("model"),
        "inspect_ai_version": eval_meta.get("inspect_ai_version"),
        "inspect_swe_version": eval_meta.get("inspect_swe_version"),
        "plan": eval_meta.get("plan"),
        # Wrapped-agent span bounds (timestamp fallback, see above).
        "agent_span_started_at": agent_begin.get("timestamp") if agent_begin else None,
        "agent_span_ended_at": agent_end.get("timestamp") if agent_end else None,
        # Two-trace data (per-actor; retained for human inspection).
        "outer_trace": outer_trace,
        "inner_trace": inner_trace,
        # Unified flat events stream for DSL iteration
        # (record_path: dsl_events).
        "dsl_events": dsl_events,
        # Top-line counts.
        "counts": {
            "outer_events": len(outer_events),
            "inner_events": len(inner_events),
            "retry_attempts": len(retry_attempts),
            "claude_code_stdout_attempts": (
                len(stdout_list) if isinstance(stdout_list, list) else 0
            ),
        },
        "diagnostics": outer_diags + inner_diags,
    }
    return container


# ──────────────────────────────────────────────────────────────────────
# Eval-level metadata extraction
# ──────────────────────────────────────────────────────────────────────

def extract_eval_meta(log: Any) -> dict:
    """Project eval-level provenance from the .eval log header.

    inspect-swe does not register itself in `eval.packages` (verified
    phase-1 corpus 2026-05-15: only `inspect_ai` is present). The
    inspect_swe version stays None unless a future release records it;
    DSL consumers can hard-pin from the adapter manifest, or sniff via
    `plan.steps[0].solver == 'Claude Code'`.
    """
    e = log.eval
    packages = e.packages or {}
    plan = log.plan.model_dump() if log.plan else None
    return {
        "eval_id": e.eval_id,
        "run_id": e.run_id,
        "task_id": e.task_id,
        "task_name": e.task,
        "task_version": getattr(e, "task_version", None),
        "model": e.model,
        "inspect_ai_version": packages.get("inspect_ai"),
        "inspect_swe_version": packages.get("inspect_swe"),  # likely None
        "plan": plan,
    }


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="inspect-swe .eval -> minitrace-DSL container preprocessor",
    )
    p.add_argument("input_path", type=Path,
                   help="Single .eval file from inspect-swe (zstd-zip)")
    p.add_argument("output_json", type=Path,
                   help="Output JSON array path")
    p.add_argument("--sample-id",
                   help="Convert only the sample matching this id (string match)")
    args = p.parse_args(argv[1:])

    if not args.input_path.exists():
        print(f"error: input path not found: {args.input_path}", file=sys.stderr)
        return 2
    if not args.input_path.is_file():
        print(
            f"error: input must be a single .eval file: {args.input_path}",
            file=sys.stderr,
        )
        return 3

    try:
        log = read_eval_log(str(args.input_path))
    except (OSError, ValueError) as exc:
        print(f"error: failed to open .eval container: {exc}", file=sys.stderr)
        return 4

    samples = log.samples or []
    if args.sample_id:
        samples = [s for s in samples if str(s.id) == args.sample_id]
        if not samples:
            print(
                f"error: no sample with id {args.sample_id!r}",
                file=sys.stderr,
            )
            return 6

    if not samples:
        print(f"error: no samples in {args.input_path}", file=sys.stderr)
        return 5

    eval_meta = extract_eval_meta(log)

    containers: list[dict] = []
    total_diagnostics = 0
    p3_errors = 0
    for sample in samples:
        container = build_sample_container(sample, eval_meta)
        diags = container.get("diagnostics", [])
        total_diagnostics += len(diags)
        if any(d.startswith("P3_COVERAGE_ERROR") for d in diags):
            p3_errors += 1
        containers.append(container)

    args.output_json.write_text(
        json.dumps(containers, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    n_samples = len(containers)
    n_outer = sum(c["counts"]["outer_events"] for c in containers)
    n_inner = sum(c["counts"]["inner_events"] for c in containers)
    n_retries = sum(c["counts"]["retry_attempts"] for c in containers)
    print(
        f"inspect-swe preprocess: {n_samples} sample container(s) "
        f"(outer_events={n_outer}, inner_events={n_inner}, "
        f"retry_attempts={n_retries}, diagnostics={total_diagnostics}"
        f"{f', P3_errors={p3_errors}' if p3_errors else ''})",
        file=sys.stderr,
    )
    # P3 errors are loud but not fatal — preprocessor still emits the
    # outer trace. Returning 0 lets the DSL run and consumers see the
    # diagnostic in the output JSON. Return non-zero only on hard input
    # errors (handled above).
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
