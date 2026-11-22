#!/usr/bin/env python3
"""goose SQLite session DB → minitrace-DSL container pre-processor.

Reads a goose sessions.db (SQLite, schema_version 7 OR 11) and emits a
JSON array of containers — one container per session row. Each container
becomes one minitrace agent-session trace via goose.dsl.yaml.

Design per the goose adapter scoping note (2026-05-09):

  D1 (preprocess): SQLite ingestion lives here, runner sees JSON only.
  D2 (canonicalisation): tool names emitted as `<extension>__<tool>`.
  D3 (extension catalogue): per-session annotation derived from extension_data.
  D4 (schema dispatch): single preprocessor branches on schema_version.
  D5 (coalescing): all reasoning/thinking blocks within an assistant message
                   concatenated into a single thinking string. Per-block
                   signatures collapsed to first non-empty value.
  D6 (lineage): orchestrator__start_agent toolResponse text is regex-scanned
                for child session_id; emitted as spawned_session_ids on
                parent and parent_session_id on child.
  D7 (reasoning flag): model_config_json.reasoning surfaced as session-level
                       reasoning_flag (orthogonal to thinking content).

Schema-version dispatch:

  v7 corpus uses block type `reasoning` with `text` field.
  v11 cohort uses block type `thinking` with `thinking` + `signature` fields.
  Both normalise to a single coalesced thinking string per assistant message.

Usage:
    python preprocess_goose.py <input.db> <output.json> [--session-id ID]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlite_source import MAX_VALUE_BYTES, EvidenceUnusable, open_evidence_db  # noqa: E402
from source_reader import nested_json_loads, take_nested_refused  # noqa: E402
from timestamps import spec_ts  # noqa: E402

# Regex for extracting child session_id from orchestrator__start_agent
# toolResponse text. The cohort observed text is:
#   "Started agent session 'NAME' with ID: 20260508_6\n..."
# session_id format is YYYYMMDD_<int> per goose convention.
_ORCHESTRATOR_CHILD_RE = re.compile(
    r"Started agent session '[^']*' with ID: (\d{8}_\d+)"
)

# Regex for canonicalising malformed bare tool names that include trailing
# JSON-like noise (corpus edge case: `tree{"path": "/workspace"}`).
_MALFORMED_TOOL_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[\{].*$")


def fetch_schema_version(db: sqlite3.Connection) -> int | None:
    """Read latest schema_version row. Returns int or None if table absent/empty."""
    try:
        row = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def normalise_timestamp(ts: Any) -> str | None:
    """Convert a goose timestamp (sqlite TIMESTAMP string or unix int) to
    minitrace ISO8601 microsecond-precision UTC."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            d = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return spec_ts(d)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # SQLite TIMESTAMP default is "YYYY-MM-DD HH:MM:SS" UTC
        if "T" not in s:
            s = s.replace(" ", "T", 1)
        # Strip trailing Z if present, parse, re-emit microsecond
        body = s[:-1] if s.endswith("Z") else s
        # Try multiple precisions
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                d = dt.datetime.strptime(body, fmt).replace(tzinfo=dt.timezone.utc)
                return spec_ts(d)
            except ValueError:
                continue
        return None
    return None


def canonicalise_tool_name(raw_name: str | None, extension: str | None) -> str:
    """Emit `<extension>__<tool>` form. Strip malformed trailing JSON.

    If extension is missing, fall back to the bare tool name (still normalised
    for malformed-name corpus edge cases).
    """
    if not raw_name:
        return extension + "__unknown" if extension else "unknown"
    name = raw_name.strip()
    m = _MALFORMED_TOOL_NAME_RE.match(name)
    if m:
        name = m.group(1)
    # Strip any extension prefix already present (avoid double-namespacing)
    if extension and name.startswith(extension + "__"):
        return name
    if extension:
        return f"{extension}__{name}"
    return name


def parse_extension_data(raw: str | None) -> tuple[list[str], dict]:
    """Parse `sessions.extension_data` JSON. Returns (catalogue_names, raw_dict).

    Returns ([], {}) for empty/null/invalid payloads.
    """
    if not raw or raw == "{}":
        return [], {}
    data = nested_json_loads(raw, None)
    if not isinstance(data, dict):
        return [], {}
    # extension_data shape: keyed by extension name; values vary
    catalogue = sorted(data.keys())
    return catalogue, data


def parse_model_config(raw: str | None) -> dict:
    """Parse `sessions.model_config_json`. Returns empty dict on error."""
    if not raw:
        return {}
    return nested_json_loads(raw, None) or {}


def extract_text_from_mcp_content(content: list) -> tuple[str, bool]:
    """Concatenate inner text blocks of an MCP-shaped tool result.

    toolResult.value.content is a list of {type, text, annotations?} blocks.
    Returns (joined_text, structured_content_present_flag).
    """
    parts: list[str] = []
    structured = False
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        # Other inner block types pass through silently; structured detection
        # happens at the outer toolResult.value.structuredContent level.
    return "\n".join(parts), structured


# What one session's messages lost to their shape (ruled 2026-10-28): counted
# while they are read, reported by _convert in one "warning:" line per
# session, which verify_run reads as a loss.
_shape_losses: dict[str, int] = {}


def _lost(what: str) -> None:
    _shape_losses[what] = _shape_losses.get(what, 0) + 1


def _tool_id(block: dict) -> str:
    """A toolRequest/toolResponse id. A present value that is not a string
    (a list, an object, a number) cannot key the request/response pairing:
    it reads as no id and is counted as a shape loss (ruled 2026-11-06)."""
    v = block.get("id")
    if v is None or isinstance(v, str):
        return v or ""
    _lost("tool id not a string")
    return ""


def take_shape_losses() -> dict[str, int]:
    losses = dict(_shape_losses)
    _shape_losses.clear()
    return losses


def _type_label(value: Any) -> str:
    return value[:40] if isinstance(value, str) else type(value).__name__


def annotate_messages(raw_messages: list[dict]) -> tuple[list[dict], list[str]]:
    """Convert raw message rows into pre-annotated DSL records.

    Implements D5 (within-message coalescing) and the assistant-thinking-only
    accumulation pattern from v0.2.0 lines ~390-410. Returns (records,
    spawned_session_ids).

    Schema-version dispatch is implicit: v7 `reasoning` and v11 `thinking`
    block types are both consumed in the same loop, both projected to a
    single coalesced `thinking` field on output. v11-only `signature` is
    captured when present; absent in v7 records.
    """
    records: list[dict] = []
    spawned: list[str] = []
    # call id -> canonical tool name of the request, across messages: a child
    # session is taken only from the response to an orchestrator start_agent
    # request, never from arbitrary tool output that merely quotes the text.
    request_tool: dict[str, str] = {}
    pending_thinking_parts: list[str] = []
    pending_thinking_signature: str | None = None
    pending_thinking_at: str | None = None

    for raw in raw_messages:
        role = raw.get("role")
        blocks = nested_json_loads(raw.get("content_json") or "[]", [])
        if not isinstance(blocks, list):
            _lost("message content not a list")
            blocks = []

        metadata = nested_json_loads(raw.get("metadata_json") or "{}", {})
        if not isinstance(metadata, dict):
            metadata = {}

        # Visibility flags pass-through (per VP6 resolution)
        goose_visibility: dict | None = None
        if metadata:
            uv = metadata.get("userVisible")
            av = metadata.get("agentVisible")
            if uv is not None or av is not None:
                # Only forward when at least one is non-default (default both true)
                if uv is False or av is False:
                    goose_visibility = {
                        "user_visible": bool(uv) if uv is not None else True,
                        "agent_visible": bool(av) if av is not None else True,
                    }

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_signature: str | None = None
        tool_calls: list[dict] = []
        tool_responses: list[dict] = []

        for block in blocks:
            if not isinstance(block, dict):
                _lost("block not an object")
                continue
            btype = block.get("type")
            if btype not in ("text", "reasoning", "thinking", "toolRequest", "toolResponse"):
                _lost(f"block of unknown type ({_type_label(btype)})")

            if btype == "text":
                t = block.get("text")
                if isinstance(t, str):
                    text_parts.append(t)

            elif btype in ("reasoning", "thinking"):
                # v7 uses `reasoning` with `text` field; v11 uses `thinking`
                # with `thinking` + `signature` fields. Coalesce all into
                # one thinking string. Capture first non-empty signature.
                if btype == "thinking":
                    t = block.get("thinking")
                    sig = block.get("signature")
                    if isinstance(sig, str) and sig and not thinking_signature:
                        thinking_signature = sig
                else:
                    t = block.get("text")
                if isinstance(t, str) and t:
                    thinking_parts.append(t)

            elif btype == "toolRequest":
                call_id = _tool_id(block)
                tool_call = block.get("toolCall") or {}
                value = tool_call.get("value") if isinstance(tool_call, dict) else {}
                if not isinstance(value, dict):
                    value = {}
                meta = block.get("_meta") or {}
                extension = meta.get("goose_extension") if isinstance(meta, dict) else None
                canonical = canonicalise_tool_name(value.get("name"), extension)
                if call_id:
                    request_tool[call_id] = canonical
                tool_calls.append({
                    "id": call_id,
                    "canonical_name": canonical,
                    "raw_name": value.get("name") or "",
                    "goose_extension": extension or "unknown",
                    "arguments": value.get("arguments") or {},
                    "tool_call_status": tool_call.get("status") if isinstance(tool_call, dict) else None,
                })

            elif btype == "toolResponse":
                call_id = _tool_id(block)
                tool_result = block.get("toolResult") or {}
                value = tool_result.get("value") if isinstance(tool_result, dict) else {}
                if not isinstance(value, dict):
                    value = {}
                inner_content = value.get("content") or []
                result_text, _ = extract_text_from_mcp_content(inner_content)
                is_error = bool(value.get("isError", False))
                structured_present = "structuredContent" in value
                tool_responses.append({
                    "id": call_id,
                    "success": not is_error,
                    "is_error": is_error,
                    "result_text": result_text,
                    "structured_content_present": structured_present,
                    "tool_result_status": tool_result.get("status") if isinstance(tool_result, dict) else None,
                })
                # D6: scan orchestrator__start_agent text for child session_id,
                # only in the response to that request.
                m = (_ORCHESTRATOR_CHILD_RE.search(result_text)
                     if request_tool.get(call_id) == "orchestrator__start_agent" else None)
                if m:
                    child_id = m.group(1)
                    if child_id not in spawned:
                        spawned.append(child_id)

        # Determine event_kind discriminator
        content_text = "\n".join(text_parts) if text_parts else ""
        thinking_text = "\n".join(thinking_parts) if thinking_parts else ""

        if role == "user":
            if tool_responses:
                event_kind = "user_tool_response"
            else:
                event_kind = "user_text"
        elif role == "assistant":
            if tool_calls:
                event_kind = "assistant_with_tools"
            elif content_text:
                event_kind = "assistant_text"
            elif thinking_text:
                event_kind = "assistant_thinking_only"
            else:
                # Empty assistant message (no text, no tools, no thinking).
                # Skip emission; nothing to record. One that held blocks lost
                # them, and says so; a text, reasoning or thinking block whose
                # text, thinking and signature are all absent or "" held
                # nothing (a signature alone is content: it is not carried).
                if any(not (isinstance(b, dict) and b.get("type") in ("text", "reasoning", "thinking")
                            and all(b.get(k) in (None, "") for k in ("text", "thinking", "signature")))
                       for b in blocks):
                    _lost("assistant message dropped with no text, thinking or tool call")
                continue
        elif role == "system":
            event_kind = "system"
        else:
            event_kind = "unknown"

        # Apply D5 coalescing accumulation: thinking-only messages defer their
        # thinking to the next non-thinking-only assistant turn (matches v0.2.0
        # behaviour at lines ~390-410). User and tool-response turns flush
        # any pending thinking by attaching it to the NEXT assistant turn.
        if event_kind == "assistant_thinking_only":
            if thinking_text:
                if not pending_thinking_parts:
                    pending_thinking_at = normalise_timestamp(
                        raw.get("created_timestamp") or raw.get("timestamp"))
                pending_thinking_parts.append(thinking_text)
            if thinking_signature and not pending_thinking_signature:
                pending_thinking_signature = thinking_signature
            continue

        # Merge pending thinking into this turn if it's an assistant turn
        if event_kind in ("assistant_text", "assistant_with_tools"):
            if pending_thinking_parts:
                merged = "\n".join(pending_thinking_parts)
                if thinking_text:
                    thinking_text = merged + "\n" + thinking_text
                else:
                    thinking_text = merged
                if pending_thinking_signature and not thinking_signature:
                    thinking_signature = pending_thinking_signature
                pending_thinking_parts = []
                pending_thinking_signature = None
                pending_thinking_at = None

        record = {
            "event_kind": event_kind,
            "role": role,
            "content_text": content_text,
            "thinking": thinking_text or None,
            "thinking_signature": thinking_signature,
            "tool_calls": tool_calls,
            "tool_responses": tool_responses,
            "timestamp": normalise_timestamp(raw.get("created_timestamp") or raw.get("timestamp")),
            "tokens": raw.get("tokens") or 0,
            "goose_visibility": goose_visibility,
            "message_id": raw.get("message_id"),
            # Text in a user message that also carries tool responses; the
            # DSL emits it as its own turn (it used to be lost).
            "accompanying_text": (content_text or None) if event_kind == "user_tool_response" else None,
        }
        records.append(record)

    # Thinking still pending at session end has no assistant turn to join:
    # emit it as one, with no text, rather than dropping it.
    if pending_thinking_parts:
        records.append({
            "event_kind": "assistant_text",
            "role": "assistant",
            "content_text": "",
            "thinking": "\n".join(pending_thinking_parts),
            "thinking_signature": pending_thinking_signature,
            "tool_calls": [],
            "tool_responses": [],
            "timestamp": pending_thinking_at,
            "tokens": 0,
            "goose_visibility": None,
            "message_id": None,
            "accompanying_text": None,
        })
    return records, spawned


def detect_goose_version_label(schema_version: int) -> str:
    """Schema-version-derived stable label per A16 (anchor on schema_version,
    not docstring or claimed CLI version)."""
    if schema_version == 7:
        return "v7-era"
    if schema_version == 11:
        return "v11-era"
    return f"schema-v{schema_version}"


def derive_source_format(schema_version: int) -> str:
    return f"goose-sqlite-v{schema_version}"


def build_container(
    session_meta: dict,
    records: list[dict],
    spawned_session_ids: list[str],
    schema_version: int,
) -> dict:
    """Assemble the per-session container the DSL consumes."""
    session_id = session_meta["id"]
    model_config = parse_model_config(session_meta.get("model_config_json"))
    extension_catalogue, extension_data_raw = parse_extension_data(
        session_meta.get("extension_data")
    )

    title_raw = session_meta.get("name") or session_meta.get("description") or ""
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    if not title:
        title = f"goose session {session_id}"

    # v11-only fields; null in v7 records
    goose_mode = session_meta.get("goose_mode") if schema_version >= 11 else None
    thread_id = session_meta.get("thread_id") if schema_version >= 11 else None

    # Inject a synthetic session_init record at the head of messages so the
    # DSL has a stable anchor for once-per-trace emission (extension-catalogue
    # annotation, lineage edges). Pattern matches claude-code's approach.
    session_init = {
        "event_kind": "session_init",
        "role": None,
        "content_text": "",
        "thinking": None,
        "thinking_signature": None,
        "tool_calls": [],
        "tool_responses": [],
        "timestamp": normalise_timestamp(session_meta.get("created_at")),
        "tokens": 0,
        "goose_visibility": None,
        "message_id": None,
    }

    return {
        "session_id": session_id,
        "trace_id": f"goose-{session_id}",
        "trace_title": title,
        "schema_version": schema_version,
        "source_format_versioned": derive_source_format(schema_version),
        "detected_goose_version": detect_goose_version_label(schema_version),
        "harness_version": "goose/schema-v7+v11",
        "created_at": normalise_timestamp(session_meta.get("created_at")),
        "updated_at": normalise_timestamp(session_meta.get("updated_at")),
        "provider": session_meta.get("provider_name"),
        "model_observed": (
            model_config.get("model_name")
            or model_config.get("model")
            or "unknown"
        ),
        "reasoning_flag": model_config.get("reasoning"),
        "temperature": model_config.get("temperature"),
        "context_limit": model_config.get("context_limit"),
        "max_tokens": model_config.get("max_tokens"),
        "toolshim_enabled": model_config.get("toolshim"),
        "working_dir": session_meta.get("working_dir"),
        "session_type": session_meta.get("session_type"),
        "goose_mode": goose_mode,
        "thread_id": thread_id,
        "extension_catalogue": extension_catalogue,
        "extension_data_raw": extension_data_raw,
        "spawned_session_ids": spawned_session_ids,
        # parent_session_id is filled in cross-link pass (post-build)
        "parent_session_id": None,
        "messages": [session_init] + records,
        # Session-level token totals from sessions table (more reliable than
        # per-message tokens which are sparse — only assistant turns carry tokens)
        # null, NOT 0, when the column is absent/NULL — an absent measurement is
        # not a measurement of zero (fleet precedent: adapters/tests/
        # test_absent_measurement.py, whose docstring named goose as the
        # violator). The goose `sessions` schema declares these three as
        # nullable INTEGER with no default, so this is reachable, not academic.
        # It matters more since these feed metrics.total_*_tokens: a fabricated
        # 0 there would be read by cell 26 and by cross-harness token comparison.
        "total_tokens": session_meta.get("total_tokens"),
        "input_tokens": session_meta.get("input_tokens"),
        "output_tokens": session_meta.get("output_tokens"),
    }


def link_parent_child(containers: list[dict]) -> None:
    """Cross-link parent.spawned_session_ids ↔ child.parent_session_id.

    Splits each parent's spawned list into two fields:
      - spawned_session_ids: full detected list (annotation evidence — both
        present and absent children matter for the asymmetry signal).
      - spawned_session_ids_in_batch: filtered to children actually present
        in this input batch (safe for emit_lineage cross-trace references).

    Sets parent_trace_id on children that have an in-batch parent, for
    symmetric `spawned-by` lineage emission.
    """
    by_id = {c["session_id"]: c for c in containers}
    for parent in containers:
        full = parent.get("spawned_session_ids") or []
        in_batch = [cid for cid in full if cid in by_id]
        parent["spawned_session_ids_in_batch"] = in_batch
        parent["spawned_trace_ids_in_batch"] = [f"goose-{cid}" for cid in in_batch]
        for child_id in in_batch:
            child = by_id[child_id]
            child["parent_session_id"] = parent["session_id"]
            child["parent_trace_id"] = f"goose-{parent['session_id']}"
    # Ensure missing keys exist on every container (DSL sees stable shape)
    for c in containers:
        c.setdefault("spawned_session_ids_in_batch", [])
        c.setdefault("spawned_trace_ids_in_batch", [])
        c.setdefault("parent_trace_id", None)


def load_sessions(db: sqlite3.Connection, session_id: str | None) -> list[dict]:
    db.row_factory = sqlite3.Row
    if session_id:
        rows = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM sessions ORDER BY created_at, id").fetchall()
    return [dict(r) for r in rows]


def load_messages(db: sqlite3.Connection, session_id: str) -> list[dict]:
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="goose SQLite → minitrace DSL container preprocessor")
    p.add_argument("input_db", type=Path, help="Path to goose sessions.db")
    p.add_argument("output_json", type=Path, help="Output JSON array path")
    p.add_argument("--session-id", help="Convert only this session id")
    args = p.parse_args(argv[1:])
    # Ruled 2026-10-28: a store the evidence reader refuses, or a value longer
    # than its length limit (SQLite fails the whole query), ends the
    # conversion with a message and exit 2, as an unreadable input does.
    try:
        return _convert(args)
    except EvidenceUnusable as e:
        print(f"error: {args.input_db}: {e}", file=sys.stderr)
        return 2
    except sqlite3.DataError:
        print(f"error: {args.input_db}: a value longer than {MAX_VALUE_BYTES} bytes", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as e:
        # Not a SQLite database, a malformed schema, or not goose's tables.
        print(f"error: {args.input_db}: cannot be read as a goose store ({e})", file=sys.stderr)
        return 2


def _convert(args: argparse.Namespace) -> int:
    if not args.input_db.exists():
        print(f"error: input db not found: {args.input_db}", file=sys.stderr)
        return 2

    db = open_evidence_db(args.input_db, tables=("schema_version", "sessions", "messages"))
    schema_version = fetch_schema_version(db)
    if schema_version is None:
        print(
            f"error: {args.input_db} has no schema_version row; "
            "exclude from baseline (per VP1)",
            file=sys.stderr,
        )
        db.close()
        return 3

    sessions = load_sessions(db, args.session_id)
    if not sessions:
        print(f"error: no sessions found in {args.input_db}", file=sys.stderr)
        db.close()
        return 4

    containers: list[dict] = []
    skipped_empty = 0
    for meta in sessions:
        messages = load_messages(db, meta["id"])
        take_nested_refused()
        take_shape_losses()
        records, spawned = annotate_messages(messages)
        losses = take_shape_losses()
        if losses:
            print(f"warning: goose session {meta['id']}: "
                  + "; ".join(f"{n} {what}" for what, n in sorted(losses.items())), file=sys.stderr)
        if not records:
            # Empty session (no non-init records). Schema requires non-empty
            # turns array; skip rather than emit invalid trace. Matches v0.2.0
            # quality-D drop behaviour.
            skipped_empty += 1
            continue
        container = build_container(meta, records, spawned, schema_version)
        refused = take_nested_refused()
        if refused:
            container["nested_json_refused"] = refused
        containers.append(container)

    db.close()
    link_parent_child(containers)

    args.output_json.write_text(json.dumps(containers, indent=2, ensure_ascii=False))

    n_sessions = len(containers)
    # Subtract synthetic session_init records to match raw SQLite row counts
    n_msgs = sum(max(0, len(c["messages"]) - 1) for c in containers)
    n_tools = sum(
        len(m.get("tool_calls", []))
        for c in containers
        for m in c["messages"]
    )
    n_lineage = sum(1 for c in containers if c.get("spawned_session_ids"))
    print(
        f"goose preprocess: {n_sessions} containers "
        f"(schema_v{schema_version}, messages={n_msgs}, tool_calls={n_tools}, "
        f"parent_sessions_with_children={n_lineage}, "
        f"skipped_empty_sessions={skipped_empty})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main(sys.argv))
