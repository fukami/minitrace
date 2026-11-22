#!/usr/bin/env python3
"""vibe session directory → minitrace-DSL container preprocessor.

Reads vibe session captures (per-directory meta.json + messages.jsonl)
and emits a JSON array of containers, one per session. Each container
becomes one minitrace agent-session trace via vibe.dsl.yaml.

Design per the vibe adapter scoping note (2026-05-11):

  D1 (version range): both 2.4.2 and 2.9.5 via field-presence dispatch.
  D2 (dispatch): `config` presence in meta.json discriminates 2.9.5+.
  D3 (config snapshot): preserved as container.config_snapshot for
                         annotation emission.
  D4 (system_prompt): preserved as container.system_prompt for
                       annotation emission.
  D5 (injected): per-message flag preserved on each annotated record.
  D6 (agent_profile): preserved on session-level metadata.
  D7 (lineage): parent_session_id resolves to delegates-to / delegated-by
                edges via link_parent_child.
  D8 (tool_approval_outcomes): stats fields preserved for annotation.
  D9 (tool result content): pass-through; no per-tool parsing.
  D10 (tools_available schemas): preserved on session metadata.
  D11 (loops): pass-through; emit only when non-empty.
  D12 (preprocessor + DSL): this file plus vibe.dsl.yaml.

Usage:
    python preprocess_vibe.py <input_path> <output_json> [--session-id ID]

Where input_path is either:
  - A session dir (contains meta.json + messages.jsonl directly), or
  - A parent dir containing one or more `session_*` subdirs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from input_tree import keep_within_tree  # noqa: E402
from redact import redact_by_key  # noqa: E402
from source_reader import (  # noqa: E402
    input_amount, input_count, load_json_file_reporting, nested_json_loads, read_jsonl_records,
    take_nested_refused)
from timestamps import take_refused, to_spec_utc  # noqa: E402

SESSION_DIR_PATTERN = re.compile(r"^session_\d{8}_\d{6}_[0-9a-f]{8}$")


def safe_json_loads(raw: str | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    return nested_json_loads(raw, default)


def normalise_timestamp(ts: Any) -> str | None:
    """Vibe writes ISO-8601 with a numeric offset (...08.471846+00:00); spec
    §3.5 and the base schema require UTC with six fractional digits and a Z.
    Converted by the shared timestamps.to_spec_utc (same instant; lossless
    for the +00:00 vibe writes). A value it cannot read, a non-string too,
    is null and counted for the session's warning line (ruled 2026-10-29);
    null and the empty string are null.
    """
    return to_spec_utc(ts)


def detect_version_label(meta: dict) -> str:
    """Field-presence dispatch (D2).

    2.9.5+: meta.json carries `title_source`, `parent_session_id`, `loops`.
    2.4.x:  none of these present.

    The 2.4.x corpus already includes `config`, `system_prompt`, and
    `agent_profile` (counter to the v0.2.0 adapter docstring's
    schema description). The genuinely-2.9.5-new fields are
    title_source / parent_session_id / loops. Use title_source as
    primary discriminator: it's a string always present in 2.9.5 and
    not optional like parent_session_id (null on root sessions) or
    loops (empty array, can't distinguish from absent).
    """
    if "title_source" in meta:
        return "2.9-era"
    return "2.4-era"


def is_session_dir(p: Path) -> bool:
    """Heuristic: the dir name matches the session_ pattern, OR meta.json
    and messages.jsonl are both present (which covers re-bundled corpora
    where the dir name may have been normalised)."""
    if not p.is_dir():
        return False
    if SESSION_DIR_PATTERN.match(p.name):
        return (p / "meta.json").exists() and (p / "messages.jsonl").exists()
    # Permissive fallback for re-bundled or copied corpora
    return (p / "meta.json").exists() and (p / "messages.jsonl").exists()


def discover_sessions(input_path: Path) -> list[Path]:
    """Return list of session dirs under input_path.

    If input_path itself looks like a session dir, returns [input_path].
    Otherwise walks one level down and returns all matching subdirs.
    """
    if is_session_dir(input_path):
        # Named directly: the directory is the analyst's, its member files are
        # not; they must not be symlinks out of it.
        members = [input_path / "meta.json", input_path / "messages.jsonl"]
        return [input_path] if len(keep_within_tree(members, input_path)) == 2 else []
    if not input_path.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(input_path.iterdir()):
        if is_session_dir(child):
            found.append(child)
    found = keep_within_tree(found, input_path)
    # The files read inside each session dir, too.
    return [d for d in found
            if len(keep_within_tree([d / "meta.json", d / "messages.jsonl"], d)) == 2]


def parse_jsonl(path: Path) -> list[dict]:
    # Through the shared reader: a damaged line is skipped, counted and
    # reported, not dropped silently; encoding damage is repaired.
    return read_jsonl_records(path)


def match_tool_results(messages: list[dict]) -> dict[str, dict]:
    """Build a tool_call_id → tool-message record lookup.

    Tool messages in vibe have role=tool and a tool_call_id field that
    references the originating assistant message's tool_calls[i].id.
    """
    lookup: dict[str, dict] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if isinstance(tcid, str) and tcid:
            if tcid in lookup:
                # Kept last-wins as before, but not silently: an earlier
                # outcome is being replaced.
                print(f"warning: second tool result for call id {tcid!r}; "
                      f"the earlier one is not in the trace", file=sys.stderr)
            lookup[tcid] = msg
    return lookup


def extract_tool_calls(
    raw_calls: list[dict] | None,
    tool_results: dict[str, dict],
) -> list[dict]:
    """Flatten an assistant message's tool_calls into adapter-friendly records.

    Each record has: id, tool_name, raw_arguments (the JSON-string from
    vibe), arguments (parsed), output_text, success (heuristic from
    tool result content), tool_call_id.
    """
    if not isinstance(raw_calls, list):
        return []
    out: list[dict] = []
    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        # A non-string id reads as absent (ruled 2026-10-27): it matches no
        # result, since match_tool_results keys only non-empty strings.
        call_id = call.get("id")
        if not isinstance(call_id, str):
            call_id = ""
        func = call.get("function") or {}
        if not isinstance(func, dict):
            func = {}
        tool_name = func.get("name") or ""
        raw_args = func.get("arguments")
        parsed_args = safe_json_loads(raw_args, {}) if isinstance(raw_args, str) else (raw_args if isinstance(raw_args, dict) else {})

        # Look up the matched tool result, if any
        result_msg = tool_results.get(call_id, {})
        result_content = result_msg.get("content")
        result_name = result_msg.get("name") or tool_name

        # vibe records no status field. A bash result is framed by the
        # harness as command/stdout/stderr/returncode lines, returncode last:
        # success is read from that final line only, since command output
        # can contain the same text but cannot come after it. No trailer,
        # no success. A result the harness wraps in <tool_error>...</tool_error>
        # failed, whatever the tool. Other tools: assumed successful when a
        # result exists, an empty one included (spec v0.2.0 requires a
        # boolean; the README says so). A call with no result did not
        # succeed; vibe_has_result: false tells it from a recorded failure.
        has_result = call_id in tool_results
        text = result_content if isinstance(result_content, str) else ""
        framed = text.strip()
        if not has_result:
            success = False
        elif framed.startswith("<tool_error>") and framed.endswith("</tool_error>"):
            success = False
        elif tool_name == "bash":
            success = bash_returncode(text) == 0
        else:
            success = True

        out.append({
            "id": call_id,
            "tool_name": tool_name,
            "raw_name": tool_name,
            "arguments": parsed_args if isinstance(parsed_args, dict) else {},
            "raw_arguments": raw_args if isinstance(raw_args, str) else json.dumps(parsed_args or {}),
            "output_text": result_content if isinstance(result_content, str) else "",
            "result_name": result_name,
            "success": success,
            "has_result": has_result,
        })
    return out


_RETURNCODE_LINE = re.compile(r"returncode: (-?\d{1,10})")


def bash_returncode(result: str) -> int | None:
    """The exit code from the final `returncode: N` line of a bash result,
    or None when the result does not end with one."""
    last = result.rstrip().rsplit("\n", 1)[-1]
    m = _RETURNCODE_LINE.fullmatch(last)
    return int(m.group(1)) if m else None


def annotate_messages(
    raw_messages: list[dict],
) -> tuple[list[dict], list[bool]]:
    """Walk messages.jsonl records, classify each into event_kind.

    Returns (annotated_records, injected_flags).
    Tool messages are NOT emitted as separate records: their content is
    attached to the originating tool_call record via match_tool_results.
    """
    tool_results = match_tool_results(raw_messages)
    records: list[dict] = []
    injected_flags: list[bool] = []

    for msg in raw_messages:
        role = msg.get("role")
        if role == "tool":
            # Attached to parent assistant message's tool_call; skip here.
            continue

        content = msg.get("content")
        text_content = content if isinstance(content, str) else (json.dumps(content) if content is not None else "")
        message_id = msg.get("message_id")
        injected = bool(msg.get("injected", False))
        injected_flags.append(injected)

        # Vibe message-level reasoning_* fields (2.9.5+, optional sparse).
        reasoning_content = msg.get("reasoning_content")
        reasoning_state = msg.get("reasoning_state")
        reasoning_signature = msg.get("reasoning_signature")

        if role == "user":
            event_kind = "user_text"
            tool_calls = []
        elif role == "system":
            # 2.9.5 may persist the system message inline in messages.jsonl
            # in addition to meta.system_prompt. Surface as a separate kind;
            # the DSL emits a system-prompt annotation on the trace (D4),
            # not a turn.
            event_kind = "system_prompt_inline"
            tool_calls = []
        elif role == "assistant":
            raw_calls = msg.get("tool_calls")
            tool_calls = extract_tool_calls(raw_calls, tool_results)
            if tool_calls:
                event_kind = "assistant_with_tools"
            elif text_content:
                event_kind = "assistant_text"
            elif reasoning_content:
                event_kind = "assistant_thinking_only"
            else:
                event_kind = "assistant_empty"
        else:
            event_kind = "unknown"
            tool_calls = []

        records.append({
            "event_kind": event_kind,
            "role": role,
            "message_id": message_id,
            "content_text": text_content,
            "injected": injected,
            "reasoning_content": reasoning_content,
            "reasoning_state": reasoning_state,
            "reasoning_signature": reasoning_signature,
            "tool_calls": tool_calls,
            # Vibe doesn't emit per-message timestamps in messages.jsonl;
            # session start/end timestamps live in meta.json.
            "timestamp": None,
        })

    return records, injected_flags


def derive_session_model(meta: dict) -> tuple[str | None, str | None]:
    """Resolve session-level model + provider.

    2.9.5: meta.config.active_model + meta.config.providers[] gives the
    full mapping. The `system_prompt.content` also embeds a "Your model
    name is: `X`" string but config is the authoritative source.

    2.4.2: meta.json doesn't embed config; fall back to deriving from
    stats prices via a heuristic (skipped; just return unknown).
    """
    config = meta.get("config") if isinstance(meta.get("config"), dict) else None
    if not config:
        # 2.4.2 path: no config in meta; provider/model not directly
        # recoverable from meta alone. Adapter returns "unknown" and a
        # downstream pass can fill if needed.
        return None, "mistral"

    active = config.get("active_model")
    if not isinstance(active, str):
        return None, "mistral"

    # Walk config.models[] for a name or alias match
    models = config.get("models") or []
    if isinstance(models, list):
        for m in models:
            if not isinstance(m, dict):
                continue
            if m.get("name") == active or m.get("alias") == active:
                return active, m.get("provider") or "mistral"
    return active, "mistral"


def derive_agent_profile(meta: dict) -> dict | None:
    ap = meta.get("agent_profile")
    if isinstance(ap, dict):
        # Project just name + overrides; ignore internal fields if any.
        return {
            "name": ap.get("name"),
            "overrides": ap.get("overrides") or {},
        }
    return None


def derive_tool_approval_outcomes(meta: dict) -> dict | None:
    stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else None
    if not stats:
        return None
    # D8: tool_calls_{agreed,rejected,failed,succeeded} are session-level
    # counters. If all are zero AND no tool_calls exist in messages.jsonl,
    # the annotation is technically empty; emit anyway for schema clarity
    # (the value is informative about producer-side gating posture).
    keys = ("tool_calls_agreed", "tool_calls_rejected", "tool_calls_failed", "tool_calls_succeeded")
    if not any(k in stats for k in keys):
        return None
    return {
        "agreed": input_count(stats.get("tool_calls_agreed"), "vibe meta.stats.tool_calls_agreed"),
        "rejected": input_count(stats.get("tool_calls_rejected"), "vibe meta.stats.tool_calls_rejected"),
        "failed": input_count(stats.get("tool_calls_failed"), "vibe meta.stats.tool_calls_failed"),
        "succeeded": input_count(stats.get("tool_calls_succeeded"), "vibe meta.stats.tool_calls_succeeded"),
    }


def derive_tools_enabled(meta: dict) -> list[dict]:
    """D10: capture full tool schemas. tools_available[] in meta.json has
    {type, function:{name, description, parameters}} per OpenAI convention.
    Project to a flat list for trace.environment.tools_enabled.
    """
    raw = meta.get("tools_available") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        out.append({
            "name": name,
            "description": fn.get("description"),
            "parameters": fn.get("parameters"),
        })
    return out


def build_container(
    session_dir: Path,
    meta: dict,
    records: list[dict],
    injected_flags: list[bool],
) -> dict:
    # A session id that is not a non-empty string reads as absent (ruled
    # 2026-10-27), never formatted into the trace id or used as a lookup key.
    session_id = meta.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = session_dir.name
    title_raw = meta.get("title") or ""
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    if not title:
        title = f"vibe session {session_id}"

    version_label = detect_version_label(meta)
    model, provider = derive_session_model(meta)
    agent_profile = derive_agent_profile(meta)
    tool_approval = derive_tool_approval_outcomes(meta)
    tools_enabled = derive_tools_enabled(meta)
    loops = meta.get("loops") if isinstance(meta.get("loops"), list) else []

    # D3 / D4: preserve the entire config and system_prompt objects on
    # the container for annotation emission in the DSL.
    # Values under secret-named keys are replaced (redact.py); the count
    # goes into the annotation.
    config_snapshot = meta.get("config") if isinstance(meta.get("config"), dict) else None
    config_redacted = None
    if config_snapshot is not None:
        config_snapshot, config_redacted = redact_by_key(config_snapshot)
    system_prompt = meta.get("system_prompt") if isinstance(meta.get("system_prompt"), dict) else None
    # An inline system message is carried as an annotation; say whether it
    # is the one meta.json holds, since either can be absent or differ.
    meta_sp_text = system_prompt.get("content") if system_prompt else None
    for r in records:
        if r.get("event_kind") == "system_prompt_inline":
            r["matches_meta_system_prompt"] = r.get("content_text") == meta_sp_text

    parent_id = meta.get("parent_session_id")
    if not isinstance(parent_id, str):
        parent_id = None

    stats = meta.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}

    env = meta.get("environment") or {}
    working_directory = env.get("working_directory") if isinstance(env, dict) else None

    started_at = normalise_timestamp(meta.get("start_time"))
    ended_at = normalise_timestamp(meta.get("end_time"))

    # Synthetic session_init record. messages[0] anchor for once-per-trace
    # emissions (annotations, lineage edges, etc.).
    session_init = {
        "event_kind": "session_init",
        "role": None,
        "message_id": None,
        "content_text": "",
        "injected": False,
        "reasoning_content": None,
        "reasoning_state": None,
        "reasoning_signature": None,
        "tool_calls": [],
        "timestamp": started_at,
    }

    any_injected = any(injected_flags)
    has_loops = bool(loops)

    return {
        "session_id": session_id,
        "trace_id": f"vibe-{session_id}",
        "trace_title": title,
        "version_label": version_label,
        "source_format_versioned": f"vibe-session-v1/{version_label}",
        "harness_version": "vibe/multi-version",
        "created_at": started_at,
        "updated_at": ended_at,
        "model_observed": model or "unknown",
        "provider": provider or "mistral",
        "working_directory": working_directory,
        "username": meta.get("username"),
        "git_commit": meta.get("git_commit"),
        "git_branch": meta.get("git_branch"),
        "agent_profile": agent_profile,
        "tools_enabled": tools_enabled,
        "tool_approval_outcomes": tool_approval,
        "config_snapshot": config_snapshot,
        "config_snapshot_redacted": config_redacted,
        "system_prompt": system_prompt,
        "loops": loops,
        "loops_present": has_loops,
        "any_injected_message": any_injected,
        "parent_session_id_from_meta": parent_id,
        "parent_session_id": parent_id,
        "parent_trace_id": f"vibe-{parent_id}" if parent_id else None,
        "spawned_session_ids": [],
        "spawned_session_ids_in_batch": [],
        "spawned_trace_ids_in_batch": [],
        "messages": [session_init] + records,
        # Session-level metric totals (from meta.stats)
        "total_cost": input_amount(stats.get("session_cost"), "vibe meta.stats.session_cost"),
        "total_tokens": input_count(stats.get("session_total_llm_tokens"), "vibe meta.stats.session_total_llm_tokens"),
        "input_tokens": input_count(stats.get("session_prompt_tokens"), "vibe meta.stats.session_prompt_tokens"),
        "output_tokens": input_count(stats.get("session_completion_tokens"), "vibe meta.stats.session_completion_tokens"),
        "context_tokens": input_count(stats.get("context_tokens"), "vibe meta.stats.context_tokens"),
        "session_steps": input_count(stats.get("steps"), "vibe meta.stats.steps"),
        "tokens_per_second": stats.get("tokens_per_second"),
        "input_price_per_million": stats.get("input_price_per_million"),
        "output_price_per_million": stats.get("output_price_per_million"),
    }


def link_parent_child(containers: list[dict]) -> None:
    """Cross-link parent.spawned_session_ids_in_batch ↔ child.parent_session_id.

    Vibe sessions carry parent_session_id in meta.json. The parent side
    has no explicit list of children; we reconstruct it by reverse-lookup
    when both sides are in the input batch. Per cycle 8 process note #7,
    only in-batch children are claimed.
    """
    by_id = {c["session_id"]: c for c in containers}
    for child in containers:
        pid = child.get("parent_session_id")
        if pid and pid in by_id:
            parent = by_id[pid]
            parent["spawned_session_ids"].append(child["session_id"])
            parent["spawned_session_ids_in_batch"].append(child["session_id"])
            parent["spawned_trace_ids_in_batch"].append(f"vibe-{child['session_id']}")
            # Confirm child's parent_trace_id is consistent
            child["parent_trace_id"] = f"vibe-{pid}"


def process_one_session(session_dir: Path) -> dict | None:
    meta_path = session_dir / "meta.json"
    messages_path = session_dir / "messages.jsonl"
    if not meta_path.exists() or not messages_path.exists():
        return None

    meta = load_json_file_reporting(meta_path)
    if meta is None:
        return None
    if not isinstance(meta, dict):
        return None

    raw_messages = parse_jsonl(messages_path)
    take_nested_refused()
    records, injected_flags = annotate_messages(raw_messages)
    if not records:
        # Per cycle 8 process note #6: empty sessions reject schema validation
        # (turns minItems >= 1). Skip them at preprocessor level.
        return None

    container = build_container(session_dir, meta, records, injected_flags)
    refused = take_nested_refused()
    if refused:
        container["nested_json_refused"] = refused
    return container


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="vibe session directory → minitrace DSL container preprocessor",
    )
    p.add_argument("input_path", type=Path,
                   help="A session dir or a parent dir containing session_* subdirs")
    p.add_argument("output_json", type=Path, help="Output JSON array path")
    p.add_argument("--session-id", help="Convert only the session dir matching this id")
    args = p.parse_args(argv[1:])

    if not args.input_path.exists():
        print(f"error: input path not found: {args.input_path}", file=sys.stderr)
        return 2

    session_dirs = discover_sessions(args.input_path)
    if args.session_id:
        session_dirs = [d for d in session_dirs if args.session_id in d.name or d.name == args.session_id]
    if not session_dirs:
        print(f"error: no session dirs found under {args.input_path}", file=sys.stderr)
        return 4

    containers: list[dict] = []
    skipped_empty = 0
    versions_seen: set[str] = set()
    for sdir in session_dirs:
        take_refused()
        container = process_one_session(sdir)
        # A timestamp written as null is lost input (ruled 2026-10-29).
        if nulled := take_refused():
            print(f"warning: vibe: {sdir}: {nulled} timestamp(s) not convertible to the §3.5 form, "
                  f"written as null", file=sys.stderr)
        if container is None:
            skipped_empty += 1
            continue
        versions_seen.add(container["version_label"])
        containers.append(container)

    if not containers:
        print(f"error: no non-empty sessions in {args.input_path}", file=sys.stderr)
        return 5

    link_parent_child(containers)

    args.output_json.write_text(
        json.dumps(containers, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    n_sessions = len(containers)
    n_msgs = sum(max(0, len(c["messages"]) - 1) for c in containers)
    n_tools = sum(
        len(m.get("tool_calls", []))
        for c in containers
        for m in c["messages"]
    )
    n_lineage_parents = sum(1 for c in containers if c.get("spawned_session_ids_in_batch"))
    n_total_cost = sum(c.get("total_cost") or 0.0 for c in containers)
    print(
        f"vibe preprocess: {n_sessions} containers "
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
