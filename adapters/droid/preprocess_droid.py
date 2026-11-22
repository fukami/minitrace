#!/usr/bin/env python3
"""droid session → minitrace-DSL container preprocessor.

Reads droid (Factory) session captures from `~/.factory/sessions/` (or a
given path) and emits a JSON array of containers, one per session. Each
container becomes one minitrace agent-session trace via droid.dsl.yaml.

Design per the droid adapter scoping note (2026-05-12):

  D1 (single-trace): one container per <uuid>.jsonl + sidecar.
  D2 (paired-file): the preprocessor stitches the sidecar .settings.json
                    into a synthetic `settings` event at position 0.
  D3 (version cut): pin to droid 0.122.0+ tool vocabulary
                    (Execute / Create / Edit / Read / Grep / TodoWrite).
                    Legacy 0.74.0 (Bash / Write / Glob / Agent) emits
                    coverage diagnostics; not actively supported here.
  D4 (user dispatch): three-way (real prompt / tool-result / frame-shift)
                      based on content shape and message.visibility.
  D5 (settings mapping): all settings fields preserved on the synthetic
                         settings event for ext:droid:* annotations.
  D6 (risk_assessment): Execute.riskLevel / riskLevelReason captured
                        verbatim per tool_call.
  D7 (todo_state): records of type=todo_state surface as their own
                   event kind with messageIndex preserved.
  D8 (provider_metadata): inner-message openaiMessageId /
                          chatCompletionReasoningField are captured
                          per record; thinking.signatureProvider stays
                          on the thinking block.
  D9 (tool vocabulary): operation_type mapping done in DSL via
                        operation_types block; preprocessor passes
                        tool_name verbatim.
  D10 (telemetry): out-of-scope for adapter; documented in README.
  D11 (parentId): captured verbatim per record; linear chain expected.
  D12 (preprocessor + DSL split): this file plus droid.dsl.yaml.

Usage:
    python preprocess_droid.py <input_path> <output_json> [--session-id ID]

Where input_path is either:
  - A factory sessions root (contains <cwd-encoded>/ subdirs), or
  - A single <cwd-encoded> directory, or
  - A single <uuid>.jsonl file (sidecar resolved by suffix swap).

Conforms to minitrace-spec v0.3.0 + minitrace-adapter-dsl-spec v0.1.0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from input_tree import field, keep_within_tree  # noqa: E402
from redact import redact_by_key  # noqa: E402
from source_reader import (  # noqa: E402
    input_count, load_json_file_reporting, nested_json_loads, read_jsonl_records, result_as_text)
from timestamps import take_refused, to_spec_utc  # noqa: E402

# Droid 0.122.0+ tool name vocabulary. Legacy 0.74.0 names (Bash,
# Write, Glob, Agent) are NOT in this set and trigger a coverage
# diagnostic.
KNOWN_TOOL_NAMES = frozenset({
    "Execute",
    "Create",
    "Edit",
    "Read",
    "Grep",
    "TodoWrite",
})

# Tool name → operation type for the v0.3.0 trace. Mirrored in the
# DSL's operation_types block; kept here for diagnostic logging.
OPERATION_TYPE_MAP = {
    "Execute": "EXECUTE",
    "Read": "READ",
    "Create": "NEW",
    "Edit": "MODIFY",
    "Grep": "READ",
    "TodoWrite": "OTHER",
}


def safe_json_loads(raw: str | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    return nested_json_loads(raw, default)


def normalise_timestamp(ts: Any) -> str | None:
    """A droid timestamp in the minitrace v0.3.0 §3.5 form.

    Droid emits ISO-8601 millisecond-precision UTC (e.g. `2026-03-15T18:30:38.944Z`);
    the base schema requires six fractional digits. The value is read by
    timestamps.to_spec_utc, as the other pre-processors do: milliseconds
    padded to six digits, an offset converted to UTC, a leap second read as
    23:59:59.999999. A value it cannot read (an invalid calendar date,
    malformed, not a string) is null and counted for take_refused(), so it
    is warned about, never passed through. None and "" are null, not counted.
    """
    return to_spec_utc(ts)


def parse_jsonl(path: Path) -> list[dict]:
    # Through the shared reader: a damaged line is skipped, counted and
    # reported, not dropped silently; encoding damage is repaired.
    return read_jsonl_records(path)


def read_settings(jsonl_path: Path) -> dict:
    """Read the <uuid>.settings.json sidecar paired with the JSONL.

    Returns an empty dict if the sidecar is absent or unreadable.
    """
    sidecar = jsonl_path.with_suffix(".settings.json")
    if not sidecar.exists() and not sidecar.is_symlink():
        return {}
    # A companion file found by name, not named by the analyst: a symlink here
    # would pull any readable JSON into the settings annotation.
    if not keep_within_tree([sidecar], jsonl_path.parent):
        return {}
    data = load_json_file_reporting(sidecar)
    return data if isinstance(data, dict) else {}


def discover_sessions(input_path: Path) -> list[Path]:
    """Return list of <uuid>.jsonl session files under input_path.

    Resolves three shapes:
      1. Direct .jsonl path → [input_path]
      2. <cwd-encoded> directory with one or more .jsonl files → contents
      3. Sessions root (~/.factory/sessions/) → walk one level deep
    """
    if not input_path.exists():
        return []
    if input_path.is_file() and input_path.suffix == ".jsonl":
        return [input_path]
    if not input_path.is_dir():
        return []

    # Direct cwd-encoded dir
    direct_jsonls = keep_within_tree(sorted(input_path.glob("*.jsonl")), input_path)
    if direct_jsonls:
        return direct_jsonls

    # Sessions root: walk one level deep
    found: list[Path] = []
    for child in sorted(input_path.iterdir()):
        if child.is_dir():
            found.extend(sorted(child.glob("*.jsonl")))
    return keep_within_tree(found, input_path)


def classify_user_record(content_blocks: list[Any], visibility: str | None) -> str:
    """Return one of: user_real_prompt | user_tool_result | user_frame_shift.

    Dispatch priority per scoping D4:
      1. Any tool_result block in content[] → tool_result wrapper.
      2. visibility == "both" with a single text block → frame-shift.
      3. Otherwise → real operator prompt.
    """
    if not isinstance(content_blocks, list):
        return "user_real_prompt"

    has_tool_result = any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content_blocks
    )
    if has_tool_result:
        return "user_tool_result"

    if visibility == "both":
        # Frame-shift marker: producer-injected pseudo-user message.
        text_only_blocks = [
            b for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if text_only_blocks and len(text_only_blocks) == len(content_blocks):
            return "user_frame_shift"

    return "user_real_prompt"


def extract_text_blocks(content_blocks: list[Any]) -> str:
    """Concatenate the text of all type=text content blocks."""
    if not isinstance(content_blocks, list):
        return ""
    parts: list[str] = []
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts)


def extract_thinking_blocks(content_blocks: list[Any]) -> tuple[str, str | None, str | None]:
    """Return (joined_thinking_text, signature, signature_provider).

    Multiple thinking blocks join with newlines. signature/provider taken
    from the first thinking block encountered.
    """
    if not isinstance(content_blocks, list):
        return "", None, None
    parts: list[str] = []
    sig: str | None = None
    sig_provider: str | None = None
    for b in content_blocks:
        if not (isinstance(b, dict) and b.get("type") == "thinking"):
            continue
        t = b.get("thinking")
        if isinstance(t, str):
            parts.append(t)
        if sig is None and isinstance(b.get("signature"), str):
            sig = b.get("signature")
        if sig_provider is None and isinstance(b.get("signatureProvider"), str):
            sig_provider = b.get("signatureProvider")
    return "\n".join(parts), sig, sig_provider


def extract_tool_use_blocks(content_blocks: list[Any]) -> list[dict]:
    """Project tool_use content blocks into adapter records.

    Each record carries: id, tool_name, arguments (verbatim),
    operation_type (via map; OTHER for unknown), known (bool flag for
    coverage diagnostic), and optional risk_level / risk_reason for
    Execute calls.
    """
    if not isinstance(content_blocks, list):
        return []
    out: list[dict] = []
    for b in content_blocks:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        tc_id = b.get("id")
        tc_name_raw = b.get("name")
        tc_name: str = tc_name_raw if isinstance(tc_name_raw, str) else ""
        raw_input = b.get("input")
        tc_input: dict = raw_input if isinstance(raw_input, dict) else {}
        op_type = OPERATION_TYPE_MAP.get(tc_name, "OTHER") if tc_name else "OTHER"
        known = tc_name in KNOWN_TOOL_NAMES

        # D6: capture riskLevel / riskLevelReason on Execute calls. Both
        # fields are kept verbatim on the tool_use record AND surfaced as
        # discrete fields for the DSL to emit as a risk_assessment
        # annotation. The keys are NOT removed from arguments.
        risk_level: str | None = None
        risk_reason: str | None = None
        if tc_name == "Execute":
            rl = tc_input.get("riskLevel")
            if isinstance(rl, str) and rl:
                risk_level = rl
            rr = tc_input.get("riskLevelReason")
            if isinstance(rr, str) and rr:
                risk_reason = rr

        out.append({
            "id": tc_id if isinstance(tc_id, str) else None,
            "tool_name": tc_name if isinstance(tc_name, str) else "unknown",
            "arguments": tc_input,
            "operation_type": op_type,
            "tool_name_known": known,
            "risk_level": risk_level,
            "risk_reason": risk_reason,
        })
    return out


def extract_tool_result_blocks(content_blocks: list[Any]) -> list[dict]:
    """Project tool_result content blocks into adapter records.

    Each record carries: tool_use_id, content (verbatim string), is_error.
    """
    if not isinstance(content_blocks, list):
        return []
    out: list[dict] = []
    for b in content_blocks:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        out.append({
            "tool_use_id": b.get("tool_use_id"),
            "content": b.get("content") if isinstance(b.get("content"), (str, list, dict)) else "",
            "is_error": bool(b.get("is_error", False)),
        })
    return out


def annotate_records(
    raw_records: list[dict],
) -> tuple[list[dict], list[str]]:
    """Walk JSONL records, classify each into event_kind.

    Returns (annotated_records, coverage_diagnostics).
    """
    records: list[dict] = []
    diagnostics: list[str] = []
    results_as_json = 0

    # First pass: collect tool_result blocks keyed by tool_use_id across
    # the whole stream so we can pair them with tool_calls at the
    # originating assistant turn.
    result_by_id: dict[str, dict] = {}
    for raw in raw_records:
        if raw.get("type") != "message":
            continue
        msg = raw.get("message", {})
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        for tr in extract_tool_result_blocks(msg.get("content", [])):
            tuid = tr.get("tool_use_id")
            if isinstance(tuid, str):
                if tuid in result_by_id:
                    print(f"warning: second tool result for tool_use_id {tuid!r}; "
                          f"the earlier one is not in the trace", file=sys.stderr)
                result_by_id[tuid] = tr

    # Second pass: produce one record per raw record.
    for raw in raw_records:
        rtype = raw.get("type")
        rid = raw.get("id")
        parent_id = raw.get("parentId") if isinstance(raw.get("parentId"), str) else None
        # The timestamp is read only for a record that is emitted, so the
        # warning counts no value a skipped record carried.

        if rtype == "session_start":
            # Surfaced separately on the container; not emitted as a
            # downstream record.
            continue

        if rtype == "todo_state":
            todos_payload = raw.get("todos")
            records.append({
                "event_kind": "todo_state",
                "record_id": rid,
                "timestamp": normalise_timestamp(raw.get("timestamp")),
                "parent_id": None,
                "message_index": raw.get("messageIndex"),
                "todos_payload": todos_payload,
            })
            continue

        if rtype != "message":
            diagnostics.append(f"unknown_record_type: {rtype!r} at record id={rid}")
            continue

        msg = raw.get("message", {})
        if not isinstance(msg, dict):
            diagnostics.append(f"message_record_missing_inner: id={rid}")
            continue
        ts = normalise_timestamp(raw.get("timestamp"))

        role = msg.get("role")
        content_blocks = msg.get("content", [])
        visibility = msg.get("visibility") if isinstance(msg.get("visibility"), str) else None

        # Inner provider-bearing fields (D8).
        openai_message_id = msg.get("openaiMessageId") if isinstance(msg.get("openaiMessageId"), str) else None
        reasoning_field = msg.get("chatCompletionReasoningField") if isinstance(msg.get("chatCompletionReasoningField"), str) else None
        reasoning_content = msg.get("chatCompletionReasoningContent") if isinstance(msg.get("chatCompletionReasoningContent"), str) else None

        if role == "user":
            kind = classify_user_record(content_blocks, visibility)
            if kind == "user_tool_result":
                tool_results = extract_tool_result_blocks(content_blocks)
                records.append({
                    "event_kind": "user_tool_result",
                    "record_id": rid,
                    "timestamp": ts,
                    "parent_id": parent_id,
                    "visibility": visibility,
                    "openai_message_id": openai_message_id,
                    "tool_results": tool_results,
                    # Text in the same message as the results; lost before.
                    "accompanying_text": extract_text_blocks(content_blocks) or None,
                })
                continue
            if kind == "user_frame_shift":
                records.append({
                    "event_kind": "user_frame_shift",
                    "record_id": rid,
                    "timestamp": ts,
                    "parent_id": parent_id,
                    "visibility": visibility,
                    "openai_message_id": openai_message_id,
                    "content_text": extract_text_blocks(content_blocks),
                })
                continue
            # Real operator prompt
            records.append({
                "event_kind": "user_real_prompt",
                "record_id": rid,
                "timestamp": ts,
                "parent_id": parent_id,
                "visibility": visibility,
                "openai_message_id": openai_message_id,
                "content_text": extract_text_blocks(content_blocks),
            })
            continue

        if role == "assistant":
            text_content = extract_text_blocks(content_blocks)
            thinking_text, thinking_sig, thinking_sig_provider = extract_thinking_blocks(content_blocks)
            tool_calls = extract_tool_use_blocks(content_blocks)

            # Coverage diagnostics for unknown tool names
            for tc in tool_calls:
                if not tc["tool_name_known"]:
                    diagnostics.append(
                        f"unknown_tool_name: {tc['tool_name']!r} "
                        f"(turn record id={rid}); legacy 0.74.0 vocabulary "
                        f"or genuinely new — pin to 0.122.0+ per D3"
                    )

            # Attach matched results inline on each tool_call record so
            # the DSL can emit atomic tool_calls without cross-record
            # iteration.
            #
            # `success` is outcome-derived, not presence-derived: a result
            # coming back is not the same as the call having worked. droid
            # signals the outcome on the paired tool_result's is_error, so
            # success = a result arrived AND it was not an error. An
            # unresolved call (no matching result) is not a success either.
            # Computed here rather than in the DSL because the expression
            # needs a conjunction and dsl_runner's evaluator has no BoolOp
            # branch — `a and not b` would fall through to the string-literal
            # fallback and emit the expression text as the value. Every other
            # preprocess-path adapter exposes `success` the same way.
            for tc in tool_calls:
                tcid = tc.get("id")
                if isinstance(tcid, str) and tcid in result_by_id:
                    tr = result_by_id[tcid]
                    # output.result is a string or null (v0.2.0): a list of
                    # content blocks or an object is written as JSON text and
                    # counted (ruled 2026-11-08).
                    tc["result_content"], structured = result_as_text(tr.get("content"))
                    results_as_json += structured
                    tc["result_is_error"] = tr.get("is_error")
                    tc["has_result"] = True
                    tc["success"] = not bool(tr.get("is_error"))
                else:
                    tc["result_content"] = None
                    tc["result_is_error"] = False
                    tc["has_result"] = False
                    tc["success"] = False

            if tool_calls:
                kind = "assistant_with_tools"
            elif thinking_text and not text_content:
                kind = "assistant_thinking_only"
            elif text_content or thinking_text:
                kind = "assistant_text"
            else:
                kind = "assistant_empty"

            records.append({
                "event_kind": kind,
                "record_id": rid,
                "timestamp": ts,
                "parent_id": parent_id,
                "content_text": text_content,
                "thinking_text": thinking_text,
                "thinking_signature": thinking_sig,
                "thinking_signature_provider": thinking_sig_provider,
                "openai_message_id": openai_message_id,
                "reasoning_field": reasoning_field,
                "reasoning_content": reasoning_content,
                "tool_calls": tool_calls,
            })
            continue

        diagnostics.append(f"unknown_role: {role!r} at record id={rid}")

    if results_as_json:
        print(f"warning: droid: {results_as_json} tool result(s) not a string, written as JSON text "
              f"(output.result is a string or null)", file=sys.stderr)
    return records, diagnostics


def project_settings(settings: dict) -> dict:
    """Project the settings.json sidecar into an annotation-friendly form.

    All fields are preserved verbatim under the projection; absence is
    explicit (None) rather than missing keys. The DSL emits one
    annotation per non-None field under the ext:droid:* namespace.
    """
    def _str_or_none(v: Any) -> str | None:
        return v if isinstance(v, str) else None

    raw, n_redacted = redact_by_key(settings)
    return {
        "model": _str_or_none(settings.get("model")),
        "reasoning_effort": _str_or_none(settings.get("reasoningEffort")),
        "interaction_mode": _str_or_none(settings.get("interactionMode")),
        "autonomy_level": _str_or_none(settings.get("autonomyLevel")),
        "autonomy_mode": _str_or_none(settings.get("autonomyMode")),
        "tags": settings.get("tags") if isinstance(settings.get("tags"), list) else None,
        "provider_lock": _str_or_none(settings.get("providerLock")),
        "provider_lock_timestamp": _str_or_none(settings.get("providerLockTimestamp")),
        "assistant_active_time_ms": settings.get("assistantActiveTimeMs") if isinstance(settings.get("assistantActiveTimeMs"), (int, float)) else None,
        # Raw passthrough for any field we didn't explicitly project,
        # so the adapter is robust to future settings additions; values
        # under secret-named keys replaced (redact.py), and counted.
        "_raw": raw,
        "_raw_redacted": n_redacted,
    }


def derive_token_totals(settings: dict) -> tuple[int | None, int | None]:
    """Session-scoped token totals from the sidecar, for metrics.total_*_tokens.

    Reads `tokenUsage` and deliberately NOT `inclusiveTokenUsage`:

    - `tokenUsage` is present at both tested rungs; `inclusiveTokenUsage` is a
      0.178.0 addition, so keying on it would leave 0.122.0 dark and split
      cell-26 coverage by rung rather than covering the span.
    - `inclusiveTokenUsage` rolls up child sessions (it is the sibling of
      `childInclusiveTokenUsageBySessionId`). The v0.1 droid adapter is
      single-trace with no fork/sub-agent emission, so those child tokens have
      no corresponding content in the trace; counting them would make
      metrics.total_*_tokens describe work the trace does not contain.
    - On the current corpus the two are numerically identical (no droid
      session in the frozen set spawns children), so the choice is free today
      and only binds once one does. It is a fleet precedent regardless: a
      trace's token metrics describe THAT trace.

    Returns (None, None) when the sidecar reports no tokenUsage. None, not 0 —
    absent measurement must not read as a measured zero (same failure shape as
    deriving success from result presence; see this file's tool-result block).
    """
    tu = settings.get("tokenUsage")
    if not isinstance(tu, dict):
        return None, None

    def _int_or_none(v: Any, what: str) -> int | None:
        # Absent stays null; anything else unusable is null and reported (DSL spec §4).
        return None if v is None else input_count(v, what)

    return (_int_or_none(tu.get("inputTokens"), "droid tokenUsage.inputTokens"),
            _int_or_none(tu.get("outputTokens"), "droid tokenUsage.outputTokens"))


def derive_provider_hint(settings_proj: dict) -> str:
    """Derive a coarse provider hint from settings.

    Returns one of: "anthropic", "openai-compatible", "factory", "unknown".
    Used only as a passive hint on environment; A26 verbatim-preservation
    keeps the original providerLock string under ext:droid:provider_lock.
    """
    pl = settings_proj.get("provider_lock")
    if not isinstance(pl, str):
        return "unknown"
    if pl == "anthropic":
        return "anthropic"
    if pl == "generic-chat-completion-api":
        return "openai-compatible"
    return "factory"


def build_container(jsonl_path: Path, raw_records: list[dict]) -> dict | None:
    """Build one container per session JSONL file."""
    # Locate session_start record for trace-level metadata.
    session_start: dict | None = None
    for raw in raw_records:
        if raw.get("type") == "session_start":
            session_start = raw
            break

    if session_start is None:
        # Defensive: a JSONL with no session_start is malformed by droid
        # convention. Skip rather than emit a partial container.
        return None

    session_id = session_start.get("id")
    if not isinstance(session_id, str):
        return None

    title = session_start.get("title") or session_start.get("sessionTitle") or ""
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        title = f"droid session {session_id[:8]}"

    cwd = session_start.get("cwd") if isinstance(session_start.get("cwd"), str) else None
    owner = session_start.get("owner") if isinstance(session_start.get("owner"), str) else None
    schema_version = session_start.get("version") if isinstance(session_start.get("version"), int) else None

    # Sidecar settings.
    settings = read_settings(jsonl_path)
    settings_proj = project_settings(settings)

    # Walk records and classify.
    records, diagnostics = annotate_records(raw_records)
    if not records:
        # Empty session (only session_start, no messages). Skip — turn
        # arrays with zero entries fail schema validation per cycle 8
        # process note #6.
        return None

    # Earliest / latest timestamps for session timing.
    timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
    started_at = timestamps[0] if timestamps else None
    ended_at = timestamps[-1] if timestamps else None

    # Synthetic session_init record so the DSL has a once-per-trace anchor
    # for session-level annotation emission.
    session_init = {
        "event_kind": "session_init",
        "record_id": None,
        "timestamp": started_at,
        "parent_id": None,
    }

    # Count any frame-shift records for downstream metrics.
    has_frame_shift = any(r["event_kind"] == "user_frame_shift" for r in records)
    has_todo_state = any(r["event_kind"] == "todo_state" for r in records)
    tool_call_count = sum(
        len(r.get("tool_calls", []))
        for r in records
        if r["event_kind"] == "assistant_with_tools"
    )
    total_input_tokens, total_output_tokens = derive_token_totals(settings)

    return {
        "session_id": session_id,
        "trace_id": f"droid-{session_id}",
        "trace_title": title,
        "version_label": f"droid/{schema_version}" if schema_version is not None else "droid/unknown",
        "source_format_versioned": f"droid-session-jsonl-v{schema_version}" if schema_version is not None else "droid-session-jsonl-unknown",
        "harness_version": "droid/0.122.0+",
        "created_at": started_at,
        "updated_at": ended_at,
        "model_observed": settings_proj.get("model") or "unknown",
        "provider_hint": derive_provider_hint(settings_proj),
        "working_directory": cwd,
        "owner": owner,
        "schema_version": schema_version,
        "settings_projection": settings_proj,
        "diagnostics": diagnostics,
        "has_frame_shift": has_frame_shift,
        "has_todo_state": has_todo_state,
        "tool_call_count": tool_call_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "messages": [session_init] + records,
    }


def process_one(jsonl_path: Path) -> dict | None:
    raw_records = parse_jsonl(jsonl_path)
    if not raw_records:
        return None
    take_refused()
    container = build_container(jsonl_path, raw_records)
    if (n := take_refused()) and container is not None:   # a dropped session lost nothing into the output
        print(f"warning: droid: {field(str(jsonl_path))}: {n} timestamp(s) not convertible to the "
              f"§3.5 form, written as null", file=sys.stderr)
    return container


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="droid session → minitrace DSL container preprocessor",
    )
    p.add_argument("input_path", type=Path,
                   help="A factory sessions root, a <cwd-encoded> dir, or a single .jsonl file")
    p.add_argument("output_json", type=Path, help="Output JSON array path")
    p.add_argument("--session-id", help="Convert only the session matching this UUID prefix")
    args = p.parse_args(argv[1:])

    if not args.input_path.exists():
        print(f"error: input path not found: {args.input_path}", file=sys.stderr)
        return 2

    jsonls = discover_sessions(args.input_path)
    if args.session_id:
        jsonls = [j for j in jsonls if args.session_id in j.name]
    if not jsonls:
        print(f"error: no droid session JSONLs found under {args.input_path}", file=sys.stderr)
        return 4

    containers: list[dict] = []
    skipped_empty = 0
    total_diagnostics = 0
    versions_seen: set[str] = set()
    for jpath in jsonls:
        container = process_one(jpath)
        if container is None:
            skipped_empty += 1
            continue
        versions_seen.add(container["version_label"])
        total_diagnostics += len(container.get("diagnostics", []))
        containers.append(container)

    if not containers:
        print(f"error: no non-empty sessions in {args.input_path}", file=sys.stderr)
        return 5

    args.output_json.write_text(
        json.dumps(containers, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    n_sessions = len(containers)
    n_msgs = sum(max(0, len(c["messages"]) - 1) for c in containers)
    n_tools = sum(c["tool_call_count"] for c in containers)
    n_frame_shift = sum(1 for c in containers if c["has_frame_shift"])
    n_todo = sum(1 for c in containers if c["has_todo_state"])
    print(
        f"droid preprocess: {n_sessions} containers "
        f"(versions={sorted(versions_seen)}, messages={n_msgs}, "
        f"tool_calls={n_tools}, with_frame_shift={n_frame_shift}, "
        f"with_todo_state={n_todo}, diagnostics={total_diagnostics}, "
        f"skipped_empty={skipped_empty})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main(sys.argv))
