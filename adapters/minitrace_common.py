#!/usr/bin/env python3
"""
minitrace shared adapter utilities — common functions used by all framework adapters.

Extracted from minitrace-claude-adapter.py to avoid duplication across adapters.
Provides: truncation, path normalization, metrics computation, quality tier
assignment, deduplication, JSON output, timestamp parsing.
"""

import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "minitrace-v0.2.0"
TRUNCATE_LIMIT = 10240  # 10 KB
IDLE_THRESHOLD = 300  # 5 minutes, per spec default
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB — reject files larger than this
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_\-.]")
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


# --- Timestamp Parsing ---

def parse_timestamp(ts_str):
    """Parse ISO 8601 timestamp string to datetime (always UTC-aware)."""
    if not ts_str:
        return None
    try:
        ts_str = str(ts_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def format_timestamp(dt):
    """Format datetime as ISO 8601 UTC string."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    """Current time as UTC-aware datetime."""
    return datetime.now(timezone.utc)


def safe_fromtimestamp(epoch, auto_ms=True):
    """Safely convert an epoch value to UTC datetime.

    Handles millisecond vs second auto-detection, and catches
    OverflowError/OSError from extreme values.
    """
    if epoch is None:
        return None
    try:
        epoch = float(epoch)
        if auto_ms and epoch > 1e12:
            epoch = epoch / 1000
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError, TypeError):
        return None


# --- Content Processing ---

def sanitize_id(raw_id):
    """Sanitize a session/agent ID for safe use in file paths.

    Replaces any character that is not alphanumeric, hyphen, underscore,
    or dot with an underscore. Rejects traversal-capable values and
    truncates to filesystem-safe length.
    """
    if not raw_id:
        return "unknown"
    clean = _SAFE_ID_RE.sub("_", str(raw_id))
    # Strip leading dots to prevent hidden files and traversal (., ..)
    clean = clean.lstrip(".")
    if not clean:
        return "unknown"
    # Truncate to stay within filesystem name limits (255 bytes)
    return clean[:200]


def sanitize_period(period):
    """Validate and sanitize a YYYY-MM period string for path use."""
    if period and _PERIOD_RE.match(period):
        return period
    return "unknown"


def safe_int(value, default=0):
    """Safely convert a value to int for token accumulation.

    Handles string/float values from untrusted session data without
    type confusion in += operations.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def truncate_content(content, limit=TRUNCATE_LIMIT):
    """Truncate content for minitrace storage.

    Returns (truncated_text, full_bytes, full_hash).
    full_bytes and full_hash are None if content fits within limit.
    """
    if content is None:
        return None, None, None
    if isinstance(content, str):
        text = content
    else:
        text = str(content)
        # Guard against huge non-string objects: truncate the str() output
        if len(text) > limit * 4:
            text = text[:limit * 4]
    encoded = text.encode("utf-8")
    full_bytes = len(encoded)
    if full_bytes <= limit:
        return text, None, None
    full_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    # Truncate by bytes (not characters) to respect the byte limit,
    # then decode back safely to avoid splitting multi-byte sequences
    truncated = encoded[:limit].decode("utf-8", errors="ignore")
    return truncated + "\n[truncated]", full_bytes, full_hash


def normalize_path(file_path):
    """Normalize absolute paths relative to home directory."""
    if not file_path:
        return file_path
    file_path = os.path.normpath(file_path)
    home = os.path.expanduser("~")
    if file_path.startswith(home):
        file_path = "~" + file_path[len(home):]
    return file_path


# --- Deduplication ---

def deduplicate_tool_calls(tool_calls, key_fn=None):
    """Remove duplicate tool calls, preserving order.

    Validated against Kimi k2.5 duplicate hook case (26 records / 16 unique
    in cross-model test). Frameworks differ in how they handle duplicate
    tool_use_ids — some deduplicate internally, others pass duplicates through.

    Args:
        tool_calls: list of tool call dicts
        key_fn: optional function to extract dedup key from a tool call.
                Defaults to using the 'id' field.

    Returns:
        (deduplicated_list, duplicate_count)
    """
    if key_fn is None:
        key_fn = lambda tc: tc.get("id")

    seen = set()
    unique = []
    dupes = 0
    for tc in tool_calls:
        key = key_fn(tc)
        if key is None or key not in seen:
            if key is not None:
                seen.add(key)
            unique.append(tc)
        else:
            dupes += 1
    return unique, dupes


# --- Timing ---

def compute_active_duration(timestamps, threshold=IDLE_THRESHOLD):
    """Compute active duration excluding idle gaps > threshold seconds."""
    if len(timestamps) < 2:
        return None
    sorted_ts = sorted(timestamps)
    active = 0.0
    for i in range(1, len(sorted_ts)):
        gap = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
        if gap <= threshold:
            active += gap
    return active


def compute_timing(all_timestamps):
    """Compute timing fields from a list of datetimes.

    Returns dict with started_at, ended_at, duration_seconds,
    active_duration_seconds, hour_of_day, day_of_week.
    """
    if not all_timestamps:
        return {
            "privacy_level": "full",
            "duration_seconds": None,
            "active_duration_seconds": None,
            "started_at": None,
            "ended_at": None,
            "hour_of_day": None,
            "day_of_week": None,
        }

    started_at = min(all_timestamps)
    ended_at = max(all_timestamps)
    duration = (ended_at - started_at).total_seconds()
    active_duration = compute_active_duration(all_timestamps)

    return {
        "privacy_level": "full",
        "duration_seconds": duration,
        "active_duration_seconds": active_duration,
        "started_at": format_timestamp(started_at),
        "ended_at": format_timestamp(ended_at),
        "hour_of_day": started_at.hour,
        "day_of_week": started_at.weekday(),
    }


# --- Metrics ---

def compute_metrics(turns, tool_calls, timing, subagent_count=0, token_totals=None):
    """Compute session-level metrics from turns and tool_calls.

    Args:
        turns: list of turn dicts
        tool_calls: list of tool_call dicts
        timing: timing dict (from compute_timing)
        subagent_count: number of subagents spawned
        token_totals: dict with input, output, cache_read, cache_creation,
                      reasoning, tool keys (all optional)

    Returns:
        metrics dict matching minitrace schema
    """
    op_counts = defaultdict(int)
    for tc in tool_calls:
        op_counts[tc["operation_type"]] += 1

    tc_count = len(tool_calls)
    read_count = op_counts.get("READ", 0)

    tokens = token_totals or {}
    metrics = {
        "turn_count": len(turns),
        "tool_call_count": tc_count,
        "read_count": read_count,
        "modify_count": op_counts.get("MODIFY", 0),
        "create_count": op_counts.get("NEW", 0),
        "execute_count": op_counts.get("EXECUTE", 0),
        "delegate_count": op_counts.get("DELEGATE", 0),
        "read_ratio": round(read_count / tc_count, 3) if tc_count > 0 else None,
        "time_to_first_action": None,
        "idle_ratio": None,
        "total_input_tokens": tokens.get("input") or None,
        "total_output_tokens": tokens.get("output") or None,
        "total_cache_read_tokens": tokens.get("cache_read") or None,
        "total_cache_creation_tokens": tokens.get("cache_creation") or None,
        "total_reasoning_tokens": tokens.get("reasoning") or None,
        "total_tool_tokens": tokens.get("tool") or None,
        "session_cost": tokens.get("cost") or None,
        "subagent_count": subagent_count,
        "subagent_tool_calls": 0,
    }

    # time_to_first_action
    started_at = parse_timestamp(timing.get("started_at"))
    if tool_calls and started_at:
        first_tc_ts = parse_timestamp(tool_calls[0].get("timestamp"))
        if first_tc_ts:
            metrics["time_to_first_action"] = (first_tc_ts - started_at).total_seconds()

    # idle_ratio
    active = timing.get("active_duration_seconds")
    duration = timing.get("duration_seconds")
    if active is not None and duration and duration > 0:
        metrics["idle_ratio"] = round(1 - (active / duration), 3)

    return metrics


# --- Quality Tier ---

def assign_quality_tier(turns, tool_calls):
    """Assign quality tier (A/B/C/D) based on session content.

    A: Full conversation + tool I/O, >10 tool calls, >5 turns
    B: Conversation but limited tool I/O or few tool calls
    C: No conversation (metadata only)
    D: Empty/trivial
    """
    has_conversation = len(turns) > 0
    has_tool_io = any(
        tc.get("output", {}).get("result") is not None
        for tc in tool_calls
    )
    tc_count = len(tool_calls)

    if has_conversation and has_tool_io and tc_count > 10 and len(turns) > 5:
        return "A"
    elif has_conversation:
        return "B"
    elif not has_conversation:
        return "C"
    else:
        return "D"


# --- PII Detection ---

def detect_pii_in_paths(tool_calls):
    """Check if tool calls contain unsanitized user paths."""
    for tc in tool_calls:
        fp = tc.get("input", {}).get("file_path", "") or ""
        if "/Users/" in fp or "/home/" in fp:
            return True
    return False


# --- Tool Call Context ---

def compute_tool_call_context(tool_calls, turns=None):
    """Fill in context fields on tool_calls: position_in_session, tools_before.

    Modifies tool_calls in place.
    """
    total_tc = len(tool_calls)
    for i, tc in enumerate(tool_calls):
        tc["context"]["position_in_session"] = round(i / total_tc, 3) if total_tc > 0 else 0
        start = max(0, i - 5)
        tc["context"]["tools_before"] = [
            tool_calls[j]["tool_name"] for j in range(start, i)
        ]


# --- Session Builder ---

def build_session_skeleton(
    session_id,
    agent_framework,
    source_format,
    converter_version,
    profile="organic",
    scenario_id=None,
):
    """Build a minimal session dict with all required fields set to defaults.

    Adapters fill in framework-specific data after calling this.
    """
    return {
        "id": session_id,
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "scenario_id": scenario_id,
        "quality": None,
        "title": None,
        "summary": None,
        "classification": "internal",
        "provenance": {
            "source_format": source_format,
            "source_path": None,
            "converted_at": format_timestamp(now_utc()),
            "converter_version": converter_version,
            "original_session_id": session_id,
        },
        "flags": {
            "for_research": False,
            "needs_cleaning": True,
            "contains_error": False,
            "contains_pii": False,
            "category": [],
        },
        "environment": {
            "model": None,
            "model_version": None,
            "temperature": None,
            "tools_enabled": [],
            "system_prompt": None,
            "agent_framework": agent_framework,
            "agent_version": None,
            "platform_type": None,
            "provider_hint": "unknown",
        },
        "operational_context": {
            "working_directory": None,
            "git_branch": None,
            "git_ref": None,
            "autonomy_level": None,
            "sandbox": None,
            "framework_config": None,
        },
        "timing": {
            "privacy_level": "full",
            "duration_seconds": None,
            "active_duration_seconds": None,
            "started_at": None,
            "ended_at": None,
            "hour_of_day": None,
            "day_of_week": None,
        },
        "condition": None,
        "coordination": {
            "project_id": None,
            "predecessor_session": None,
            "concurrent_sessions": None,
            "human_attention": "unknown",
        },
        "handover": {"received": None, "produced": None},
        "turns": [],
        "tool_calls": [],
        "outcome": None,
        "annotations": [],
        "metrics": {},
    }


# --- Title Extraction ---

def extract_title(turns, max_len=80):
    """Extract session title from first substantive user message."""
    for t in turns:
        if t.get("role") == "user" and t.get("source") == "human" and t.get("content"):
            return t["content"][:max_len].replace("\n", " ").strip()
    return None


# --- Post-Conversion Canary ---

# Frameworks where tool calls are expected in non-trivial sessions.
# Ghost sessions (no tool calls) are legitimate, but 50 turns with 0 tool calls
# from Claude Code is suspicious.
_TOOL_EXPECTED_FRAMEWORKS = {
    "claude-code", "codex", "goose", "opencode", "droid",
    "gemini-cli", "pi", "vibe", "openclaw",
}


def canary_check(session, verbose=False):
    """Post-conversion sanity check. Detects silent parser breakage.

    Returns a list of warning strings. Empty list = no issues detected.
    Warnings are soft (never block conversion) but should be surfaced
    to the operator so broken adapters don't produce garbage silently.

    Checks:
      1. Structural: non-zero turns, timestamps present, tool names not all default
      2. Provenance (v0.2.0): input_channel and content_origin coverage
      3. Framework expectations: tool calls expected for non-trivial sessions
    """
    warnings = []
    metrics = session.get("metrics", {})
    turns = session.get("turns", [])
    tool_calls = session.get("tool_calls", [])
    env = session.get("environment", {})
    framework = env.get("agent_framework", "unknown")
    turn_count = metrics.get("turn_count", len(turns))
    tc_count = metrics.get("tool_call_count", len(tool_calls))
    sid = session.get("id", "?")

    # --- Structural checks ---

    # S1: Session with no turns at all
    if turn_count == 0:
        warnings.append(f"[S1] {sid}: 0 turns (empty session)")

    # S2: Tool calls expected but absent in non-trivial sessions
    if (
        framework in _TOOL_EXPECTED_FRAMEWORKS
        and turn_count > 5
        and tc_count == 0
    ):
        warnings.append(
            f"[S2] {sid}: {turn_count} turns but 0 tool calls "
            f"(framework={framework}, expected tool use)"
        )

    # S3: Tool calls with all-null timestamps
    if tc_count > 0:
        ts_count = sum(1 for tc in tool_calls if tc.get("timestamp"))
        if ts_count == 0:
            warnings.append(
                f"[S3] {sid}: {tc_count} tool calls, all timestamps null"
            )

    # S4: Tool calls with all-"unknown" tool names
    if tc_count > 0:
        unknown_count = sum(
            1 for tc in tool_calls
            if tc.get("tool_name") in (None, "unknown", "")
        )
        if unknown_count == tc_count:
            warnings.append(
                f"[S4] {sid}: {tc_count} tool calls, all tool_name unknown/null"
            )

    # S5: Model is "unknown" in a non-empty session
    if turn_count > 0 and env.get("model") in (None, "unknown"):
        warnings.append(f"[S5] {sid}: model is unknown")

    # --- v0.2.0 provenance checks ---

    # P1: input_channel coverage (expect at least one non-null if turns > 3)
    if turn_count > 3:
        ic_count = sum(1 for t in turns if t.get("input_channel"))
        if ic_count == 0:
            warnings.append(
                f"[P1] {sid}: {turn_count} turns, all input_channel null "
                f"(v0.2.0 field not populated)"
            )

    # P2: content_origin coverage (expect at least one non-null if tool calls > 5)
    if tc_count > 5:
        co_count = sum(
            1 for tc in tool_calls
            if tc.get("output", {}).get("content_origin")
        )
        if co_count == 0:
            warnings.append(
                f"[P2] {sid}: {tc_count} tool calls, all content_origin null "
                f"(v0.2.0 field not populated)"
            )

    if verbose and warnings:
        for w in warnings:
            print(f"  CANARY {w}", file=sys.stderr)

    return warnings


# --- Output ---

def write_session(session, output_dir, quality):
    """Write a minitrace session to the archive structure.

    Returns (file_path, file_size, period, quality).
    """
    started = session["timing"].get("started_at")
    period = sanitize_period(started[:7] if started else None)

    # F19: normalize provenance source_path before writing
    prov = session.get("provenance", {})
    if prov.get("source_path"):
        prov["source_path"] = normalize_path(prov["source_path"])

    out_path = Path(output_dir) / "active" / period
    out_path.mkdir(parents=True, exist_ok=True)

    safe_id = sanitize_id(session["id"])
    file_path = out_path / f"{safe_id}.minitrace.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)

    return str(file_path), os.path.getsize(file_path), period, quality


def write_manifests(session_index, output_dir):
    """Write root and period manifests from session index entries."""
    output = Path(output_dir)

    by_period = defaultdict(list)
    for entry in session_index:
        by_period[sanitize_period(entry.get("period"))].append(entry)

    total = len(session_index)
    by_profile = defaultdict(int)
    by_quality = defaultdict(int)
    by_classification = defaultdict(int)
    dates = []
    for e in session_index:
        by_profile[e["profile"]] += 1
        by_quality[e["quality"]] += 1
        by_classification[e["classification"]] += 1
        if e.get("started_at"):
            dates.append(e["started_at"])

    periods = []
    for period, entries in sorted(by_period.items()):
        tier = "active"
        period_dir = output / tier / period
        period_dir.mkdir(parents=True, exist_ok=True)

        period_manifest = {
            "version": "minitrace-manifest-v2",
            "period": period,
            "generated_at": format_timestamp(now_utc()),
            "sessions": [{
                "id": e["id"],
                "schema_version": SCHEMA_VERSION,
                "profile": e["profile"],
                "title": e.get("title"),
                "classification": e["classification"],
                "quality": e["quality"],
                "started_at": e.get("started_at"),
                "duration_seconds": e.get("duration_seconds"),
                "model": e.get("model"),
                "agent_framework": e.get("agent_framework", "unknown"),
                "turn_count": e.get("turn_count", 0),
                "tool_call_count": e.get("tool_call_count", 0),
                "file_path": f"{sanitize_id(e['id'])}.minitrace.json",
                "file_size_bytes": e.get("file_size_bytes", 0),
                "source_format": e.get("source_format"),
                "flags": e.get("flags", {}),
            } for e in entries],
        }

        manifest_path = period_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(period_manifest, f, indent=2, ensure_ascii=False)

        periods.append({
            "period": period,
            "path": f"{tier}/{period}/manifest.json",
            "session_count": len(entries),
        })

    root_manifest = {
        "version": "minitrace-manifest-v2",
        "generated_at": format_timestamp(now_utc()),
        "periods": periods,
        "statistics": {
            "total_sessions": total,
            "by_profile": dict(by_profile),
            "by_quality": dict(by_quality),
            "by_classification": dict(by_classification),
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            },
        },
    }

    root_path = output / "manifest.json"
    with open(root_path, "w", encoding="utf-8") as f:
        json.dump(root_manifest, f, indent=2, ensure_ascii=False)


# --- File Safety ---

import stat as stat_module


def check_file_safety(path, max_size=MAX_FILE_SIZE):
    """Validate a file is a regular file (not symlink/FIFO/device) and within size limit.

    Combines symlink check, file type check, and size check in one call.
    Returns file size or raises ValueError.
    """
    p = Path(path)
    if p.is_symlink():
        raise ValueError(f"Symlink rejected: {p.name}")
    st = os.stat(path)
    if not stat_module.S_ISREG(st.st_mode):
        raise ValueError(f"Not a regular file: {p.name}")
    if st.st_size > max_size:
        raise ValueError(
            f"File too large: {st.st_size:,} bytes (limit {max_size:,}). "
            f"Skipping {p.name}"
        )
    return st.st_size


def safe_open(path, max_size=MAX_FILE_SIZE):
    """Open a file safely, rejecting symlinks via O_NOFOLLOW where available.

    Returns a file object. Raises ValueError for unsafe files.
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise ValueError(f"Cannot open {Path(path).name}: {type(e).__name__}") from e
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            os.close(fd)
            raise ValueError(f"Not a regular file: {Path(path).name}")
        if st.st_size > max_size:
            os.close(fd)
            raise ValueError(f"File too large: {st.st_size:,} bytes")
    except Exception:
        os.close(fd)
        raise
    return os.fdopen(fd, "r", encoding="utf-8-sig", errors="replace")


# Keep old names as aliases for adapters that import them directly
def check_file_size(path, max_size=MAX_FILE_SIZE):
    """Legacy alias. Prefer check_file_safety() or safe_open()."""
    return check_file_safety(path, max_size)


def check_symlink(path):
    """Legacy alias. Prefer check_file_safety() or safe_open()."""
    p = Path(path)
    if p.is_symlink():
        raise ValueError(f"Symlink rejected: {p.name}")


# --- JSONL Parsing (shared) ---

def parse_jsonl(path):
    """Parse a JSONL file into a list of records, skipping corrupted lines.

    Uses safe_open() to reject symlinks (via O_NOFOLLOW), FIFOs, device files,
    and files exceeding MAX_FILE_SIZE. Handles UTF-8 BOM via utf-8-sig encoding.
    """
    records = []
    skipped = 0
    with safe_open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    if skipped > 0:
        print(f"  WARN {Path(path).name}: skipped {skipped} malformed line(s)",
              file=sys.stderr)
    return records


# --- Tool Call Builder ---

def build_tool_call(
    tc_id,
    turn_index,
    timestamp,
    tool_name,
    operation_type,
    file_path=None,
    command=None,
    arguments=None,
    success=True,
    result=None,
    error=None,
    duration_ms=None,
    framework_metadata=None,
    spawned_agent=None,
    content_origin=None,
    redacted=None,
):
    """Build a tool call dict matching the minitrace ToolCall schema.

    Content truncation is applied to result automatically.

    Args:
        turn_index: passed as emitting_turn_index in the output. May be
            None for shell-first frameworks where tool events are not
            subordinate to assistant turns.
        content_origin: v0.2.0. Where the tool output content originated.
            Values: "local_file", "local_exec", "web", "mcp_server",
            "database", "sub_agent", "model_echo", "user_provided", or None.
        redacted: v0.2.0. True if tool result was policy-redacted by
            the platform before export. None means unknown.
    """
    truncated_result, full_bytes, full_hash = truncate_content(result)

    return {
        "id": tc_id,
        "emitting_turn_index": turn_index,
        "timestamp": timestamp,
        "tool_name": tool_name,
        "operation_type": operation_type,
        "input": {
            "file_path": normalize_path(file_path) if file_path else None,
            "command": command,
            "arguments": arguments,
        },
        "output": {
            "success": success,
            "result": truncated_result,
            "error": error,
            "duration_ms": duration_ms,
            "truncated": full_bytes is not None,
            "full_bytes": full_bytes,
            "full_hash": full_hash,
            "full_reference": None,
            "redacted": redacted,
            "content_origin": content_origin,
        },
        "context": {
            "position_in_session": None,  # filled by compute_tool_call_context
            "tools_before": [],           # filled by compute_tool_call_context
            "time_since_last_user": None,
        },
        "framework_metadata": framework_metadata,
        "spawned_agent": spawned_agent,
    }


# --- Turn Builder ---

def build_turn(
    index,
    timestamp,
    role,
    source,
    content,
    tool_calls_in_turn=None,
    thinking=None,
    usage=None,
    input_channel=None,
    framework_metadata=None,
    model=None,
    content_type=None,
):
    """Build a turn dict matching the minitrace Turn schema.

    Args:
        input_channel: v0.2.0. Through what channel did this turn's content arrive.
            Values: "user_input", "system_prompt", "framework_control",
            "framework_content", "tool_output", "retrieval", or None.
            Legacy: "framework_inject" accepted by validators for backward
            compatibility but deprecated. New adapters MUST NOT use it.
        model: v0.2.0. Per-turn model identifier when known. null means
            fall back to session-level environment.model.
        content_type: v0.2.0. Content modality. Values: "text",
            "multimodal_text", "code", "reasoning", or None.
    """
    return {
        "index": index,
        "timestamp": timestamp,
        "role": role,
        "source": source,
        "model": model,
        "content_type": content_type,
        "input_channel": input_channel,
        "content": content,
        "tool_calls_in_turn": tool_calls_in_turn or [],
        "thinking": thinking,
        "intent_markers": None,
        "streaming": {"was_streamed": False, "stream_log": None},
        "usage": usage,
        "framework_metadata": framework_metadata,
    }


# --- Annotation Builder ---

def build_annotation(
    ann_id,
    annotator,
    scope_type,
    target_id,
    category,
    title,
    detail,
    tags=None,
    taxonomy_mappings=None,
):
    """Build an annotation dict matching the minitrace Annotation schema."""
    return {
        "id": ann_id,
        "timestamp": format_timestamp(now_utc()),
        "annotator": annotator,
        "scope": {"type": scope_type, "target_id": target_id},
        "content": {
            "category": category,
            "tags": tags or [],
            "title": title,
            "detail": detail,
        },
        "taxonomy_mappings": taxonomy_mappings or {
            "minitrace": [], "mast": [], "toolemu": []
        },
        "classification": None,
    }
