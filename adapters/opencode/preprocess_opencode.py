#!/usr/bin/env python3
"""opencode SQLite session DB → minitrace-DSL container preprocessor.

Reads an opencode SQLite DB (versions 1.2.20 + 1.14.40, dispatching on
session.version) and emits a JSON array of containers — one per session.
Each container becomes one minitrace agent-session trace via opencode.dsl.yaml.

Design per the opencode adapter scoping note (2026-05-10):

  D1 (preprocess): SQLite ingestion lives here, runner sees JSON only.
  D2 (canonicalisation): bare tool names (opencode tools globally unique).
  D3 (extension catalogue): not applicable; opencode has no MCP-extension
                            data-shape surface.
  D4 (schema dispatch): single preprocessor branches on session.version
                        with semver-aware ordering (1.2.20 < 1.14.40).
  D5 (coalescing): all reasoning blocks within an assistant message
                   concatenated into a single thinking string. First/last
                   block timing aggregated.
  D6 (lineage): use task.state.metadata.sessionId as structured cross-link
                (no regex, unlike goose's orchestrator__start_agent text scan).
  D8 (cache): flatten cache.{read,write} to flat primitives; preserve
              nested shape in framework_metadata.opencode_cache_shape.
  D9 (subagent_type): symmetric annotation on parent's task tool call AND
                      on child session's environment.
  D10 (agent attribution): derive session-level agent from first message's
                           data.agent (session.agent column is NULL in 1.14.40
                           despite existing).
  D11 (new tables): inspect session_message, event, event_sequence; if
                    populated, emit raw rows in framework_extra channels.
                    Skip when empty (current observed state).
  D12 (snapshot/patch): emit only for sessions where version < 1.14.40.
                        Bidirectional schema-dispatch demonstration.
  D13 (hosted gateway): no special-case for `provider: opencode` strings.

Usage:
    python preprocess_opencode.py <input.db> <output.json> [--session-id ID]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlite_source import MAX_VALUE_BYTES, EvidenceUnusable, open_evidence_db  # noqa: E402
from source_reader import count_sum, input_count, nested_json_loads, take_nested_refused  # noqa: E402
from timestamps import spec_ts  # noqa: E402


def parse_semver(version_str: str | None) -> tuple[int, int, int]:
    """Parse "1.2.20" into (1, 2, 20). Returns (0,0,0) on failure (sorts first)."""
    if not version_str:
        return (0, 0, 0)
    parts = version_str.split(".")
    if len(parts) != 3:
        return (0, 0, 0)
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (0, 0, 0)


# Threshold for D12 (snapshot/patch removal). Versions strictly less than
# this tuple emit snapshot+patch annotations; versions >= skip them.
_SNAPSHOT_REMOVED_AT: tuple[int, int, int] = (1, 14, 0)


def has_snapshot_feature(version: str | None) -> bool:
    """D12: snapshot/patch only for sessions before 1.14.0."""
    return parse_semver(version) < _SNAPSHOT_REMOVED_AT


def normalise_timestamp(ts: Any) -> str | None:
    """Convert opencode unix-millisecond timestamp (integer) to minitrace
    ISO8601 microsecond-precision UTC."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            d = dt.datetime.fromtimestamp(float(ts) / 1000.0, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return spec_ts(d)
    return None


# What one session's messages lost to their shape (ruled 2026-10-28): counted
# while they are read, reported by _convert in one "warning:" line per
# session, which verify_run reads as a loss.
_KNOWN_PART_TYPES = ("text", "reasoning", "tool", "step-start", "step-finish", "patch")
_shape_losses: Counter = Counter()


def take_shape_losses() -> Counter:
    losses = Counter(_shape_losses)
    _shape_losses.clear()
    return losses


def _type_label(value: Any) -> str:
    return value[:40] if isinstance(value, str) else type(value).__name__


def safe_json_loads(raw: str | None, default: Any = None) -> Any:
    """JSON parse, returning default on null/invalid."""
    if raw is None or raw == "":
        return default
    return nested_json_loads(raw, default)


def derive_source_format(version: str | None) -> str:
    return f"opencode-sqlite-v{version or 'unknown'}"


def derive_version_label(version: str | None) -> str:
    """Stable schema-version-derived label per cycle-9 dispatch convention."""
    if version is None:
        return "unknown"
    sv = parse_semver(version)
    if sv >= (1, 14, 0):
        return "1.14-era"
    if sv >= (1, 2, 0):
        return "1.2-era"
    return f"v{version}"


def coalesce_reasoning(parts: list[dict]) -> tuple[str | None, int | None, int | None]:
    """D5: concatenate all reasoning-part text per message; aggregate timing.

    Returns (combined_thinking_text, time_start_ms, time_end_ms).
    """
    texts: list[str] = []
    time_start: int | None = None
    time_end: int | None = None
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "reasoning":
            continue
        text = p.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
        time = p.get("time") or {}
        if isinstance(time, dict):
            ts = time.get("start")
            te = time.get("end")
            if isinstance(ts, (int, float)):
                if time_start is None or ts < time_start:
                    time_start = int(ts)
            if isinstance(te, (int, float)):
                if time_end is None or te > time_end:
                    time_end = int(te)
    if not texts:
        return None, time_start, time_end
    return "\n".join(texts), time_start, time_end


def extract_text(parts: list[dict]) -> str:
    """Concatenate all text-part text in order."""
    out: list[str] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "text":
            continue
        text = p.get("text")
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def extract_tool_calls(parts: list[dict]) -> tuple[list[dict], list[str]]:
    """Walk parts for type=tool entries. Atomic (request + response unified).

    Returns (tool_call_records, spawned_session_ids).
    Each tool record has: id (callID), tool_name, input, output, success,
    metadata, title, time, subagent_type (for task tool), spawned_session_id.
    """
    out: list[dict] = []
    spawned: list[str] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "tool":
            continue
        call_id = p.get("callID") or ""
        tool_name = p.get("tool") or ""
        state = p.get("state") or {}
        if not isinstance(state, dict):
            state = {}

        status = state.get("status")
        success = status == "completed"
        is_error = status in ("error", "failed")
        input_data = state.get("input") if isinstance(state.get("input"), dict) else {}
        output_data = state.get("output")
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        title = state.get("title") or ""
        time = state.get("time") if isinstance(state.get("time"), dict) else {}

        # D9: subagent_type from task tool input
        subagent_type: str | None = None
        # D6: cross-link sessionId from task tool metadata
        spawned_session_id: str | None = None
        if tool_name == "task":
            stype = input_data.get("subagent_type") if isinstance(input_data, dict) else None
            if isinstance(stype, str) and stype:
                subagent_type = stype
            sid = metadata.get("sessionId") if isinstance(metadata, dict) else None
            if isinstance(sid, str) and sid:
                spawned_session_id = sid
                if sid not in spawned:
                    spawned.append(sid)

        out.append({
            "id": call_id,
            "tool_name": tool_name,
            "raw_name": tool_name,
            "input": input_data or {},
            "output_text": output_data if isinstance(output_data, str) else json.dumps(output_data) if output_data is not None else "",
            "success": success,
            "is_error": is_error,
            "tool_status": status,
            "metadata": metadata or {},
            "title": title,
            "time_start": time.get("start") if isinstance(time, dict) else None,
            "time_end": time.get("end") if isinstance(time, dict) else None,
            "subagent_type": subagent_type,
            "spawned_session_id": spawned_session_id,
        })
    return out, spawned


def extract_snapshots(parts: list[dict]) -> list[str]:
    """D12: extract snapshot hashes from step-start/step-finish parts.
    Only meaningful for 1.2.20 sessions; returns empty list for 1.14.40+.
    """
    out: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") not in ("step-start", "step-finish"):
            continue
        snap = p.get("snapshot")
        if isinstance(snap, str) and snap and snap not in out:
            out.append(snap)
    return out


def extract_patches(parts: list[dict]) -> list[dict]:
    """D12: extract patch part summaries (1.2.20-only)."""
    out: list[dict] = []
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "patch":
            continue
        out.append({
            "hash": p.get("hash"),
            "files": p.get("files") or [],
        })
    return out


def extract_step_finish_metrics(parts: list[dict]) -> dict:
    """Aggregate token + cost from step-finish parts in a message.

    Multiple step-finish parts can occur per message (one per step boundary).
    Sum token counts; sum costs; preserve last reason as message-level.
    """
    total_tokens = 0
    total_input = 0
    total_output = 0
    total_reasoning = 0
    total_cache_read = 0
    total_cache_write = 0
    total_cost = 0.0
    last_reason: str | None = None
    has_any = False
    for p in parts:
        if not isinstance(p, dict) or p.get("type") != "step-finish":
            continue
        has_any = True
        tokens = p.get("tokens") or {}
        if isinstance(tokens, dict):
            total_tokens = count_sum(total_tokens, input_count(tokens.get("total"), "opencode tokens.total"), "opencode tokens.total")
            total_input = count_sum(total_input, input_count(tokens.get("input"), "opencode tokens.input"), "opencode tokens.input")
            total_output = count_sum(total_output, input_count(tokens.get("output"), "opencode tokens.output"), "opencode tokens.output")
            total_reasoning = count_sum(total_reasoning, input_count(tokens.get("reasoning"), "opencode tokens.reasoning"), "opencode tokens.reasoning")
            cache = tokens.get("cache") or {}
            if isinstance(cache, dict):
                total_cache_read = count_sum(total_cache_read, input_count(cache.get("read"), "opencode tokens.read"), "opencode tokens.read")
                total_cache_write = count_sum(total_cache_write, input_count(cache.get("write"), "opencode tokens.write"), "opencode tokens.write")
        cost = p.get("cost")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
        reason = p.get("reason")
        if isinstance(reason, str):
            last_reason = reason
    if not has_any:
        return {}
    return {
        "total_tokens": total_tokens,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "reasoning_tokens": total_reasoning,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "cost": total_cost,
        "stop_reason": last_reason,
    }


def annotate_messages(
    raw_messages: list[dict],
    parts_by_message: dict[str, list[dict]],
    session_version: str | None,
) -> tuple[list[dict], list[str]]:
    """Convert raw messages + their parts into pre-annotated DSL records.

    Returns (records, spawned_session_ids).
    Implements D5 (reasoning coalescing), D6 (cross-link extraction via tool
    metadata), D9 (subagent_type), D12 (snapshot/patch only for 1.2.20-era).
    """
    records: list[dict] = []
    all_spawned: list[str] = []
    snapshot_capable = has_snapshot_feature(session_version)

    for raw in raw_messages:
        msg_id = raw.get("id")
        if not isinstance(msg_id, str):
            continue
        data = safe_json_loads(raw.get("data"), {})
        if not isinstance(data, dict):
            data = {}
        role = data.get("role")
        agent = data.get("agent")
        mode = data.get("mode")
        model_id = data.get("modelID")
        provider_id = data.get("providerID")
        finish = data.get("finish")
        time = data.get("time") or {}
        time_created = time.get("created") if isinstance(time, dict) else None
        time_completed = time.get("completed") if isinstance(time, dict) else None
        # User messages may have a model object instead of flat IDs
        if not model_id:
            model_obj = data.get("model")
            if isinstance(model_obj, dict):
                model_id = model_obj.get("modelID")
                provider_id = provider_id or model_obj.get("providerID")
        cost_msg = data.get("cost")
        tokens_msg = data.get("tokens") or {}

        parts = parts_by_message.get(msg_id, [])
        # Each part row's `data` column is JSON; unwrap once
        unwrapped_parts: list[dict] = []
        for prow in parts:
            pdata = safe_json_loads(prow.get("data"), {})
            if isinstance(pdata, dict):
                if pdata.get("type") == "patch":
                    # Collected (before 1.14) but mapped by no DSL rule: it
                    # never reaches the trace (ruled 2026-10-28: reported).
                    _shape_losses["patch part(s) not carried into the trace"] += 1
                elif pdata.get("type") not in _KNOWN_PART_TYPES:
                    _shape_losses[f"part(s) of unknown type ({_type_label(pdata.get('type'))})"] += 1
                pdata.setdefault("_part_id", prow.get("id"))
                pdata.setdefault("_part_time_created", prow.get("time_created"))
                unwrapped_parts.append(pdata)
            else:
                _shape_losses["part not an object"] += 1

        text_content = extract_text(unwrapped_parts)
        thinking, thinking_start, thinking_end = coalesce_reasoning(unwrapped_parts)
        tool_calls, spawned = extract_tool_calls(unwrapped_parts)
        for sid in spawned:
            if sid not in all_spawned:
                all_spawned.append(sid)
        snapshots = extract_snapshots(unwrapped_parts) if snapshot_capable else []
        patches = extract_patches(unwrapped_parts) if snapshot_capable else []
        step_metrics = extract_step_finish_metrics(unwrapped_parts)

        # Determine event_kind
        if role == "user":
            event_kind = "user_text"
        elif role == "assistant":
            if tool_calls:
                event_kind = "assistant_with_tools"
            elif text_content:
                event_kind = "assistant_text"
            elif thinking:
                event_kind = "assistant_thinking_only"
            else:
                # Empty assistant message (no text, thinking, or tools).
                # Don't emit a turn (would violate spec), but keep the record
                # so its step_metrics + cost still aggregate at session level.
                # Observed in 1.2.20 corpus: some sessions have assistant messages
                # with only step-start+step-finish parts and no content.
                event_kind = "assistant_empty_with_metrics"
                # One that held other parts lost them, and says so; an empty
                # text or reasoning part held nothing.
                if any(p.get("type") not in ("step-start", "step-finish")
                       and not (p.get("type") in ("text", "reasoning") and not p.get("text"))
                       for p in unwrapped_parts):
                    _shape_losses["assistant message with no text, reasoning or tool call"] += 1
        else:
            event_kind = "unknown"

        records.append({
            "event_kind": event_kind,
            "role": role,
            "message_id": msg_id,
            "agent": agent,
            "mode": mode,
            "model_id": model_id,
            "provider_id": provider_id,
            "finish": finish,
            "content_text": text_content,
            "thinking": thinking,
            "thinking_time_start_ms": thinking_start,
            "thinking_time_end_ms": thinking_end,
            "tool_calls": tool_calls,
            "snapshots": snapshots,
            "patches": patches,
            "timestamp": normalise_timestamp(time_created),
            "completed_timestamp": normalise_timestamp(time_completed),
            "cost": cost_msg if isinstance(cost_msg, (int, float)) else None,
            "tokens_raw": tokens_msg if isinstance(tokens_msg, dict) else {},
            "step_metrics": step_metrics,
        })

    return records, all_spawned


def derive_session_agent(records: list[dict]) -> str | None:
    """D10: derive session-level agent from first message's agent field."""
    for r in records:
        agent = r.get("agent")
        if agent:
            return agent
    return None


def derive_session_model(records: list[dict]) -> tuple[str | None, str | None]:
    """Derive session-level model from first assistant message."""
    for r in records:
        if r.get("role") == "assistant":
            return r.get("model_id"), r.get("provider_id")
    # Fall back to first user message (which carries model declaration)
    for r in records:
        if r.get("model_id"):
            return r.get("model_id"), r.get("provider_id")
    return None, None


def aggregate_session_cost(records: list[dict]) -> tuple[float, dict]:
    """Sum cost + tokens across all assistant messages in the session."""
    total_cost = 0.0
    total_tokens = 0
    total_input = 0
    total_output = 0
    total_reasoning = 0
    total_cache_read = 0
    total_cache_write = 0
    for r in records:
        if r.get("role") != "assistant":
            continue
        cost = r.get("cost")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
        tokens = r.get("tokens_raw") or {}
        if isinstance(tokens, dict):
            total_tokens = count_sum(total_tokens, input_count(tokens.get("total"), "opencode tokens.total"), "opencode tokens.total")
            total_input = count_sum(total_input, input_count(tokens.get("input"), "opencode tokens.input"), "opencode tokens.input")
            total_output = count_sum(total_output, input_count(tokens.get("output"), "opencode tokens.output"), "opencode tokens.output")
            total_reasoning = count_sum(total_reasoning, input_count(tokens.get("reasoning"), "opencode tokens.reasoning"), "opencode tokens.reasoning")
            cache = tokens.get("cache") or {}
            if isinstance(cache, dict):
                total_cache_read = count_sum(total_cache_read, input_count(cache.get("read"), "opencode tokens.read"), "opencode tokens.read")
                total_cache_write = count_sum(total_cache_write, input_count(cache.get("write"), "opencode tokens.write"), "opencode tokens.write")
    return total_cost, {
        "total": total_tokens,
        "input": total_input,
        "output": total_output,
        "reasoning": total_reasoning,
        "cache_read": total_cache_read,
        "cache_write": total_cache_write,
    }


def fetch_session_message_rows(db: sqlite3.Connection, session_id: str) -> list[dict]:
    """D11: SELECT all rows from session_message for a session.

    Returns [] if table doesn't exist (1.2.20 DBs) or no rows.
    """
    try:
        cursor = db.execute(
            "SELECT * FROM session_message WHERE session_id = ?",
            (session_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return []
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, r)) for r in rows]
    except sqlite3.OperationalError:
        return []


def fetch_event_rows(db: sqlite3.Connection, session_id: str) -> list[dict]:
    """D11: events are aggregate-id keyed; correlate to session by aggregate_id."""
    try:
        cursor = db.execute(
            "SELECT * FROM event WHERE aggregate_id = ?",
            (session_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return []
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, r)) for r in rows]
    except sqlite3.OperationalError:
        return []


def build_container(
    session_meta: dict,
    records: list[dict],
    spawned_session_ids: list[str],
    optional_session_messages: list[dict],
    optional_events: list[dict],
) -> dict:
    """Assemble the per-session container the DSL consumes."""
    session_id = session_meta["id"]
    version = session_meta.get("version") or ""
    title_raw = session_meta.get("title") or ""
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    if not title:
        title = f"opencode session {session_id}"

    # D10: derive agent from messages
    derived_agent = derive_session_agent(records)
    derived_model, derived_provider = derive_session_model(records)

    # Aggregate cost + tokens at session level (D8: flatten + preserve nested)
    total_cost, token_totals = aggregate_session_cost(records)
    cache_nested_shape = {
        "read": token_totals["cache_read"],
        "write": token_totals["cache_write"],
    }

    # 1.14.40-specific session fields (NULL for 1.2.20 sessions)
    workspace_id = session_meta.get("workspace_id")
    session_path = session_meta.get("path")
    summary_additions = session_meta.get("summary_additions")
    summary_deletions = session_meta.get("summary_deletions")
    summary_files = session_meta.get("summary_files")
    time_compacting = session_meta.get("time_compacting")
    time_archived = session_meta.get("time_archived")

    # D6: parent_id from session table is structured; also captured via task
    # tool cross-link (the two should agree). Prefer session.parent_id since
    # it's authoritative when present.
    parent_id = session_meta.get("parent_id")

    # Synthetic session_init record for once-per-trace emission anchor
    session_init = {
        "event_kind": "session_init",
        "role": None,
        "message_id": None,
        "agent": None,
        "mode": None,
        "model_id": None,
        "provider_id": None,
        "finish": None,
        "content_text": "",
        "thinking": None,
        "thinking_time_start_ms": None,
        "thinking_time_end_ms": None,
        "tool_calls": [],
        "snapshots": [],
        "patches": [],
        "timestamp": normalise_timestamp(session_meta.get("time_created")),
        "completed_timestamp": None,
        "cost": None,
        "tokens_raw": {},
        "step_metrics": {},
    }

    return {
        "session_id": session_id,
        "trace_id": f"opencode-{session_id}",
        "trace_title": title,
        "version": version,
        "version_label": derive_version_label(version),
        "source_format_versioned": derive_source_format(version),
        "harness_version": "opencode/multi-version",
        "created_at": normalise_timestamp(session_meta.get("time_created")),
        "updated_at": normalise_timestamp(session_meta.get("time_updated")),
        "compacted_at": normalise_timestamp(time_compacting),
        "archived_at": normalise_timestamp(time_archived),
        "agent": derived_agent,
        "model_observed": derived_model or "unknown",
        "provider": derived_provider or "unknown",
        "project_id": session_meta.get("project_id"),
        "workspace_id": workspace_id,
        "session_path": session_path,
        "session_summary": {
            "additions": summary_additions,
            "deletions": summary_deletions,
            "files": summary_files,
        } if any(v is not None for v in (summary_additions, summary_deletions, summary_files)) else None,
        "parent_session_id_from_db": parent_id,
        "spawned_session_ids": spawned_session_ids,
        # filled by link_parent_child:
        "parent_session_id": parent_id,
        "parent_trace_id": f"opencode-{parent_id}" if parent_id else None,
        "spawned_session_ids_in_batch": [],
        "spawned_trace_ids_in_batch": [],
        "messages": [session_init] + records,
        # Session-level token + cost totals
        "total_cost": total_cost,
        "total_tokens": token_totals["total"],
        "input_tokens": token_totals["input"],
        "output_tokens": token_totals["output"],
        "reasoning_tokens": token_totals["reasoning"],
        "cache_read_tokens": token_totals["cache_read"],
        "cache_write_tokens": token_totals["cache_write"],
        # D8: nested cache shape preservation
        "opencode_cache_shape": cache_nested_shape,
        # D11: pass-through forward-compat channels (empty when tables empty)
        "opencode_session_messages": optional_session_messages,
        "opencode_events": optional_events,
        # Emission guards for the two pass-throughs. The DSL has no length
        # predicate (value:/present: only; present:true would fire on []), so
        # non-emptiness is derived here, as vibe does for loops_present.
        "opencode_session_messages_present": bool(optional_session_messages),
        "opencode_events_present": bool(optional_events),
        # The event log replays message/part content (message.part.updated
        # carries the prompt and response text) that the trace already holds in
        # turns; embedding it raw made the annotation 69-84% of the trace on the
        # 1.17.15 rungs and put a second copy of the text outside any
        # turn-level redaction. The annotation carries this summary instead
        # (since 2026-09-25): row count and rows per event type, no data.
        "opencode_events_summary": {
            "row_count": len(optional_events),
            "by_type": dict(sorted(Counter(
                str(r.get("type")) for r in optional_events).items())),
        },
        # D12: feature-availability flag for DSL conditional
        "snapshot_feature_present": has_snapshot_feature(version),
    }


def link_parent_child(containers: list[dict]) -> None:
    """Cross-link parent.spawned_session_ids ↔ child.parent_session_id.

    Same pattern as goose preprocessor: split spawned into full + in-batch
    for safe DSL emission.
    """
    by_id = {c["session_id"]: c for c in containers}
    for parent in containers:
        full = parent.get("spawned_session_ids") or []
        in_batch = [cid for cid in full if cid in by_id]
        parent["spawned_session_ids_in_batch"] = in_batch
        parent["spawned_trace_ids_in_batch"] = [f"opencode-{cid}" for cid in in_batch]
    # Children: session.parent_id from DB is authoritative when present.
    # If the parent is in the batch, populate parent_trace_id consistently.
    for child in containers:
        pid = child.get("parent_session_id")
        if pid and pid in by_id:
            child["parent_trace_id"] = f"opencode-{pid}"


def load_sessions(db: sqlite3.Connection, session_id: str | None) -> list[dict]:
    db.row_factory = sqlite3.Row
    if session_id:
        rows = db.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM session ORDER BY time_created, id").fetchall()
    return [dict(r) for r in rows]


def load_messages(db: sqlite3.Connection, session_id: str) -> list[dict]:
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM message WHERE session_id = ? ORDER BY time_created, id",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_parts_for_session(db: sqlite3.Connection, session_id: str) -> dict[str, list[dict]]:
    """Group parts by message_id, ordered by time_created."""
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM part WHERE session_id = ? ORDER BY time_created, id",
        (session_id,),
    ).fetchall()
    by_msg: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        by_msg.setdefault(d["message_id"], []).append(d)
    return by_msg


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="opencode SQLite → minitrace DSL container preprocessor")
    p.add_argument("input_db", type=Path, help="Path to opencode SQLite DB")
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
        # Not a SQLite database, a malformed schema, or not opencode's tables.
        print(f"error: {args.input_db}: cannot be read as an opencode store ({e})", file=sys.stderr)
        return 2


def _convert(args: argparse.Namespace) -> int:
    if not args.input_db.exists():
        print(f"error: input db not found: {args.input_db}", file=sys.stderr)
        return 2

    db = open_evidence_db(args.input_db, tables=("session", "message", "part", "event", "session_message"))
    sessions = load_sessions(db, args.session_id)
    if not sessions:
        print(f"error: no sessions found in {args.input_db}", file=sys.stderr)
        db.close()
        return 4

    containers: list[dict] = []
    skipped_empty = 0
    versions_seen: set[str] = set()
    for meta in sessions:
        sid = meta["id"]
        sver = meta.get("version") or ""
        versions_seen.add(sver)
        messages = load_messages(db, sid)
        parts_by_msg = load_parts_for_session(db, sid)
        take_nested_refused()
        take_shape_losses()
        records, spawned = annotate_messages(messages, parts_by_msg, sver)
        losses = take_shape_losses()
        if losses:
            print(f"warning: opencode session {sid}: "
                  + "; ".join(f"{n} {what}" for what, n in sorted(losses.items())), file=sys.stderr)
        if not records:
            skipped_empty += 1
            continue
        # D11: optional table inspection (1.14.40-only tables)
        opt_session_messages = fetch_session_message_rows(db, sid)
        opt_events = fetch_event_rows(db, sid)
        container = build_container(
            meta, records, spawned, opt_session_messages, opt_events,
        )
        refused = take_nested_refused()
        if refused:
            container["nested_json_refused"] = refused
        containers.append(container)

    db.close()
    link_parent_child(containers)

    args.output_json.write_text(json.dumps(containers, indent=2, ensure_ascii=False))

    n_sessions = len(containers)
    n_msgs = sum(max(0, len(c["messages"]) - 1) for c in containers)  # subtract session_init
    n_tools = sum(
        len(m.get("tool_calls", []))
        for c in containers
        for m in c["messages"]
    )
    n_lineage_parents = sum(1 for c in containers if c.get("spawned_session_ids_in_batch"))
    n_total_cost = sum(c.get("total_cost") or 0.0 for c in containers)
    print(
        f"opencode preprocess: {n_sessions} containers "
        f"(versions={sorted(versions_seen)}, messages={n_msgs}, tool_calls={n_tools}, "
        f"parents_with_in_batch_children={n_lineage_parents}, "
        f"total_cost=${n_total_cost:.6f}, skipped_empty={skipped_empty})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main(sys.argv))
