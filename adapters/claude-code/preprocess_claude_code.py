#!/usr/bin/env python3
"""Claude Code JSONL → minitrace-DSL container pre-processor.

Reads one parent session JSONL (`<workspace>/<session-id>.jsonl`) plus its
optional sibling subagents directory (`<workspace>/<session-id>/subagents/*.jsonl`)
and emits a JSON array with ONE container per parent session.

Design per the Claude Code adapter scoping note (2026-05-07):
  Q1: multi-trace (parent + N subagents)
  Q2: container mode with stitching at pre-processor time
  Q3: harness version pinned per record (taken from each record's `version`)
  Q4: attachment.attachment.type hoisted to event.attachment_subtype
  Q5: user.isMeta=true → event_kind=user_meta
  Q6: system.subtype=stop_hook_summary kept on event for hook-policy emission
  record `timestamp` read into the §3.5 form at read (timestamps.to_spec_utc);
  a value it cannot read is null and counted in a warning line
  tool_use `input` an object or null (trace §4.8.1): a JSON string holding an
  object parsed, any other non-object null, both counted in a warning line

Discards `file-history-snapshot`, `last-prompt`, `queue-operation` per the
scoping note's port-from-v0.2.0 decisions. The CC 2.1.x `mode` record (paired
with `permission-mode` at each interaction boundary) is captured as a
`mode_change` event, not discarded — see scoping note addendum 2026-06-05.

Usage:
    python preprocess_claude_code.py <input.jsonl> <output.json>

Subagent discovery is automatic: if `<input.jsonl_stem>/subagents/` exists
next to the input, every *.jsonl in it is loaded as a sibling subagent.

Reads through the shared `source_reader` module (converter input contract,
2026-07-30, §2), so a damaged line is skipped, counted and recorded rather
than aborting the conversion. This is the
first conformer; the counts it produces are written to a sidecar
`<output>.fidelity.json` rather than into the containers, because a value the
DSL passes through is adapter-declared, and the fidelity record's roster must
be interpreter-supplied to mean anything (source-fidelity proposal §2.4).
Wiring it into the trace waits on that DSL-spec change.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from input_tree import field, keep_within_tree  # noqa: E402
from redact import redact_by_key  # noqa: E402
from source_reader import (  # noqa: E402
    SourceRead,
    SourceUnreadable,
    asserted_at_now,
    build_assertion,
    nested_json_loads,
    read_jsonl,
    result_as_text,
)
from timestamps import take_refused, to_spec_utc  # noqa: E402

DISCARD_TYPES = {"file-history-snapshot", "last-prompt", "queue-operation", "progress"}

# Identity this preprocessor asserts fidelity under. Versioned independently of
# the DSL's `converter_version`: the preprocessor is the component that reads
# the raw log, and the counts are claims about that read.
ASSERTED_BY = "minitrace-adapter/claude-code"
PREPROCESSOR_VERSION = "preprocess-claude-code/0.1.0"


# The session log decides every value's type. A record whose message is a
# boolean, whose content is a number or whose uuid is a list must not abort
# the export of the parent and every subagent; these read such a value as
# absent where a dict, a list or a key was expected.
def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _key(v):
    """A uuid usable as a dict key: a string, else absent. Also the gate that
    lets only a string sessionId or agentId become part of a trace id."""
    return v if isinstance(v, str) else None


def _order_key(e: dict) -> tuple[str, str]:
    """Total order for sorting: a numeric timestamp beside string ones raised."""
    t, u = e.get("timestamp"), e.get("uuid")
    return (t if isinstance(t, str) else "", u if isinstance(u, str) else "")


# tool_use inputs normalised since the last _report_tool_inputs():
# "parsed" a JSON string holding an object, "nulled" any other value that
# is not an object (ruled 2026-11-08); "results" tool_result contents that
# were not a string, written as JSON text (ruled 2026-11-08).
_tool_input_counts = {"parsed": 0, "nulled": 0, "results": 0}


def _tool_input(v):
    """tool_use.input as trace §4.8.1 has input.arguments: an object, or
    null. Absent or null stays null; a string that parses to a JSON object
    is that object (the rule for string-encoded sources); any other value
    (another string, a list, a number, a boolean) is null. Ruled
    2026-11-08; counted for the file's warning line."""
    if v is None or isinstance(v, dict):
        return v
    if isinstance(v, str):
        parsed = nested_json_loads(v, None, report_malformed=False)
        if isinstance(parsed, dict):
            _tool_input_counts["parsed"] += 1
            return parsed
    _tool_input_counts["nulled"] += 1
    return None


def _report_tool_inputs(path: Path) -> None:
    parsed, nulled = _tool_input_counts["parsed"], _tool_input_counts["nulled"]
    results = _tool_input_counts["results"]
    _tool_input_counts.update(parsed=0, nulled=0, results=0)
    if parsed or nulled:
        print(f"warning: claude-code: {field(str(path))}: tool_use input not an object: "
              f"{parsed} JSON string(s) parsed to an object, {nulled} written as null (trace §4.8.1)",
              file=sys.stderr)
    if results:
        print(f"warning: claude-code: {field(str(path))}: {results} tool result(s) not a string, "
              f"written as JSON text (output.result is a string or null)", file=sys.stderr)


def _text(blk: dict) -> str:
    t = blk.get("text", "")
    return t if isinstance(t, str) else ""


def classify_event_kind(record: dict) -> str | None:
    """Assign a stable event_kind discriminator. Returns None for discards."""
    rtype = record.get("type")
    if not isinstance(rtype, str):
        # A crafted record ({"type": []}) must stay visible, not crash the
        # set lookup below; kept as an unknown event like any unrecognised type.
        return "unknown:non-string-type"
    if rtype in DISCARD_TYPES:
        return None
    if rtype == "user":
        if record.get("isMeta") is True:
            return "user_meta"
        content = _obj(record.get("message")).get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    return "user_tool_result"
        return "user_prompt"
    if rtype == "assistant":
        return "assistant_turn"
    if rtype == "system":
        return "system_event"
    if rtype == "attachment":
        return "attachment"
    if rtype == "permission-mode":
        # Untimestamped mode snapshots; segment markers come from the
        # timestamped `permissionMode` on user prompts instead (see
        # build_session_block). Discarded by policy, counted by type.
        return None
    if rtype == "mode":
        return "mode_change"
    if rtype == "ai-title":
        return None  # consumed as container metadata, not an event
    return f"unknown:{rtype}"


def mcp_framework_metadata(tool_name: str | None) -> dict | None:
    """Parse a claude-code MCP tool name `mcp__<server>__<tool>` into structured
    `framework_metadata`. Returns None for non-MCP tool names.

    The native JSONL records an MCP invocation only as a `tool_use` block whose
    `name` is `mcp__<server>__<tool>` (there is no structured server field); the
    bare name otherwise falls through `operation_types: default -> OTHER` with
    server/tool unparsed. Splitting them here lets the DSL surface them in the
    ToolCall's `framework_metadata` slot without changing `tool_name` (which stays
    the framework-specific name per §4) or the OTHER classification (the name
    carries no read/write/execute semantics). `tool` keeps any trailing `__`
    (server never does — it is the first segment after the `mcp__` prefix)."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    if len(parts) < 3 or not parts[1] or not parts[2]:
        return None  # malformed (e.g. `mcp__server` with no tool) -> leave as OTHER
    return {"mcp_server": parts[1], "mcp_tool": "__".join(parts[2:])}


def reshape_event(record: dict, harness_version: str | None,
                  spawn_map: dict[str, str] | None = None) -> dict | None:
    """Project a JSONL record into the pre-processor's event shape.

    Drops records whose event_kind is None. Hoists nested fields the DSL would
    otherwise have to dig into (attachment_subtype, content_blocks, etc.).

    `spawn_map` maps subagent spawn-prompt strings → subagent trace_id. When
    set, assistant_turn tool_use blocks with name in {Agent, Task, TaskCreate}
    get a `spawned_agent_payload` attached for emitting v0.2.x-compatible
    `ToolCall.spawned_agent` per draft-4 §12.1.5."""
    kind = classify_event_kind(record)
    if kind is None:
        return None

    base = {
        "event_kind": kind,
        "uuid": record.get("uuid"),
        "parent_uuid": record.get("parentUuid"),
        "timestamp": record.get("timestamp"),
        "is_sidechain": record.get("isSidechain", False),
        "harness_version": record.get("version") or harness_version,
    }

    if kind == "user_prompt":
        msg = _obj(record.get("message"))
        content = msg.get("content")
        # When content is a list of text blocks (rare for user_prompt), join.
        if isinstance(content, list):
            text_parts = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text_parts.append(_text(blk))
            content = "\n".join(text_parts) if text_parts else json.dumps(content)
        base["content"] = content
        base["prompt_id"] = record.get("promptId")
        base["permission_mode"] = record.get("permissionMode")

    elif kind == "user_meta":
        msg = _obj(record.get("message"))
        content = msg.get("content")
        if isinstance(content, list):
            text_parts = [
                _text(blk)
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            content = "\n".join(text_parts) if text_parts else json.dumps(content)
        base["content"] = content

    elif kind == "user_tool_result":
        msg = _obj(record.get("message"))
        blocks = _list(msg.get("content"))
        results = []
        texts = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text" and isinstance(blk.get("text"), str) and blk["text"]:
                texts.append(blk["text"])
            if blk.get("type") != "tool_result":
                continue
            # output.result is a string or null (v0.2.0): content given as a
            # list of blocks (or any other structure) is written as JSON text
            # and counted (ruled 2026-11-08).
            content, structured = result_as_text(blk.get("content"))
            if structured:
                _tool_input_counts["results"] += 1
            results.append({
                "tool_use_id": blk.get("tool_use_id"),
                "is_error": bool(blk.get("is_error", False)),
                "content": content,
            })
        base["tool_results"] = results
        # Text in the same message as the results (an interruption note, or
        # a message typed while the tool ran) would otherwise be lost.
        base["accompanying_text"] = "\n".join(texts) if texts else None
        base["tool_use_result_raw"] = record.get("toolUseResult")
        base["source_assistant_uuid"] = record.get("sourceToolAssistantUUID")

    elif kind == "assistant_turn":
        msg = _obj(record.get("message"))
        content_blocks = []
        for blk in _list(msg.get("content")):
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                content_blocks.append({"type": "text", "text": blk.get("text", "")})
            elif btype == "thinking":
                content_blocks.append({
                    "type": "thinking",
                    "thinking": blk.get("thinking", ""),
                    "signature": blk.get("signature"),
                })
            elif btype == "tool_use":
                tool_input = _tool_input(blk.get("input"))
                tu_block: dict = {
                    "type": "tool_use",
                    "id": blk.get("id"),
                    "name": blk.get("name"),
                    "input": tool_input,
                    "caller": blk.get("caller"),
                    "spawned_agent_payload": None,
                    "framework_metadata": mcp_framework_metadata(blk.get("name")),
                }
                # Attach v0.2.x-compatible spawned_agent payload for delegation
                # tools whose prompt matches a subagent's spawn prompt.
                if spawn_map and blk.get("name") in ("Agent", "Task", "TaskCreate"):
                    tool_input = _obj(tool_input)
                    prompt = tool_input.get("prompt") or ""
                    sub_trace_id = spawn_map.get(prompt) if isinstance(prompt, str) else None
                    tu_block["spawned_agent_payload"] = {
                        "agent_type": (
                            tool_input.get("subagent_type")
                            or tool_input.get("type")
                            or "general"
                        ),
                        "task_scope": prompt[:200] if isinstance(prompt, str) else None,
                        "sub_session_id": sub_trace_id,
                        "outcome_summary": None,
                    }
                content_blocks.append(tu_block)
            else:
                content_blocks.append({"type": btype, "raw": blk})
        base["content_blocks"] = content_blocks
        base["model"] = msg.get("model")
        base["stop_reason"] = msg.get("stop_reason")
        base["usage"] = msg.get("usage")

    elif kind == "system_event":
        base["subtype"] = record.get("subtype")
        base["level"] = record.get("level")
        base["content"] = record.get("content")
        base["hook_infos"] = record.get("hookInfos")
        base["hook_count"] = record.get("hookCount")
        base["hook_errors"] = record.get("hookErrors")
        base["prevented_continuation"] = record.get("preventedContinuation")
        base["stop_reason"] = record.get("stopReason")
        base["tool_use_id"] = record.get("toolUseID")
        base["duration_ms"] = record.get("durationMs")
        base["message_count"] = record.get("messageCount")

    elif kind == "attachment":
        raw = record.get("attachment")
        att = _obj(raw)
        base["attachment_subtype"] = att.get("type")
        # A non-object attachment stays visible as recorded.
        base["attachment"] = raw if raw and not isinstance(raw, dict) else att

    elif kind == "mode_change":
        # Paired with permission-mode at each interaction boundary; second
        # dimension of the same state snapshot. Always "normal" in our corpus
        # so far, but a real state marker (potential independent variable) —
        # captured, not discarded, for behavioural-variation research.
        base["mode"] = record.get("mode")

    return base


def order_events(events: list[dict]) -> list[dict]:
    """Order events by parent_uuid linked-list when possible, falling back to
    timestamp order. State-marker events (no parent_uuid, no timestamp) sort to
    the end of any contiguous gap to keep the conversation thread coherent."""
    children: dict[str | None, list[dict]] = {}
    for ev in events:
        children.setdefault(_key(ev.get("parent_uuid")), []).append(ev)

    # Stable timestamp sort within each child bucket.
    for bucket in children.values():
        bucket.sort(key=_order_key)

    ordered: list[dict] = []
    emitted: set[int] = set()

    def emit(ev: dict) -> None:
        if id(ev) in emitted:
            return
        emitted.add(id(ev))
        ordered.append(ev)

    # Iterative DFS pre-order. Recursive form overflowed Python's default
    # recursion limit on sessions with >1000 records along the linear
    # parentUuid chain (Phase 2: 2.1.97 / 2.1.119 / 2.1.126 sessions).
    # Stack entries are (uuid_key, next_child_index): we re-push the parent
    # at index+1 before descending into the current child's subtree, so
    # peak stack depth is O(branching factor) not O(chain length).
    stack: list[tuple[str | None, int]] = [(None, 0)]
    while stack:
        uuid_key, idx = stack.pop()
        bucket = children.get(uuid_key, [])
        if idx >= len(bucket):
            continue
        stack.append((uuid_key, idx + 1))
        ev = bucket[idx]
        if id(ev) in emitted:
            continue
        emit(ev)
        ev_uuid = _key(ev.get("uuid"))
        if ev_uuid:
            stack.append((ev_uuid, 0))

    # Pick up orphans (parent_uuid points to a record we never saw) by timestamp.
    orphans = [ev for ev in events if id(ev) not in emitted]
    orphans.sort(key=_order_key)
    for ev in orphans:
        emit(ev)

    return ordered


# Attachment subtypes that describe the harness context rather than an event
# in the conversation (CC >= 2.1.260). They are hoisted into the container by
# collect_harness_context() and leave the event stream.
# message.model of assistant records the harness writes itself (API errors,
# interrupts); not a model the session switched to.
SYNTHETIC_MODEL = "<synthetic>"

HOISTED_ATTACHMENTS = frozenset({
    "prompt_snapshot", "deferred_tools_record", "environment", "instructions",
})


def collect_harness_context(records: list[dict]) -> dict:
    """System prompt, tool set and environment the harness recorded for a session.

    CC >= 2.1.260 writes `prompt_snapshot` attachments: an early one with the
    system prompt, a later one adding `tools` and `cliPrefix` (measured over
    165 sessions: the system prompt never differs between them, the second
    tool list is always a superset). `deferred_tools_record` lists tools loaded
    later (mostly MCP), `environment` an environment snapshot, `instructions`
    the instruction files loaded (e.g. CLAUDE.md).

    Spec fields: system_prompt, tools_enabled (null when not in the source).
    Everything without a spec field goes to the `ext:claude-code` extension,
    which always states whether the source recorded any of this. Instruction
    file content is referenced by sha256 and size, not copied into every trace;
    it stays in the raw file named by provenance.source_path.
    """
    system_prompts: list = []   # each a str or a list of str blocks, as recorded
    host_prompt = cli_prefix = None
    tool_defs: list[dict] = []
    tool_names: list[str] = []
    env_snaps: list[dict] = []
    instr: list[dict] = []
    seen_instr: set[tuple] = set()

    def add_tool(t: dict, deferred: bool) -> None:
        name = t.get("name")
        if not isinstance(name, str) or name in tool_names:
            return
        tool_names.append(name)
        tool_defs.append({
            "name": name,
            "description": t.get("description"),
            "input_schema": t.get("schema", t.get("input_schema")),
            "deferred": deferred,
        })

    for r in records:
        if r.get("type") != "attachment":
            continue
        att = _obj(r.get("attachment"))
        sub = att.get("type")
        if sub == "prompt_snapshot":
            sp = att.get("systemPrompt")
            ok = isinstance(sp, str) or (isinstance(sp, list) and all(isinstance(b, str) for b in sp))
            if ok and sp not in system_prompts:
                system_prompts.append(sp)
            host_prompt = host_prompt or att.get("hostPrompt") or None
            cli_prefix = cli_prefix or att.get("cliPrefix") or None
            for t in _list(att.get("tools")):
                if isinstance(t, dict):
                    add_tool(t, deferred=False)
        elif sub == "deferred_tools_record":
            for t in _list(att.get("entries")):
                if isinstance(t, dict):
                    add_tool(t, deferred=True)
        elif sub == "environment":
            snap = att.get("snapshot")
            if isinstance(snap, dict) and (not env_snaps or env_snaps[-1] != snap):
                env_snaps.append(snap)
        elif sub == "instructions":
            for f in _list(att.get("files")):
                if not isinstance(f, dict):
                    continue
                body = f.get("content")
                if not isinstance(body, str):
                    body = ""
                digest = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
                path = f.get("path")
                key = (path if isinstance(path, str) else json.dumps(path, sort_keys=True), digest)
                if key in seen_instr:
                    continue
                seen_instr.add(key)
                instr.append({"path": f.get("path"), "type": f.get("type"),
                              "bytes": len(body.encode("utf-8")), "sha256": digest})

    recorded = bool(system_prompts or tool_names or env_snaps or instr)
    ext: dict = {"snapshot_status": "recorded" if recorded else "not_in_source"}
    if recorded:
        # Values under secret-named keys replaced (redact.py), and counted.
        env_snaps, env_redacted = redact_by_key(env_snaps)
        ext.update({
            "host_prompt": host_prompt,
            "cli_prefix": cli_prefix,
            "tool_definitions": tool_defs,
            "environment_snapshots": env_snaps,
            "environment_redacted": env_redacted,
            "instruction_files": instr,
        })
        if len(system_prompts) > 1:
            # never observed (0 of 165); keep the first, say so instead of choosing silently
            ext["system_prompt_variants"] = len(system_prompts)
    # CC records the system prompt as a list of text blocks (13 in the sample).
    # The spec field is a string, so blocks are joined with "\n\n"; the block
    # lengths make the join reversible without storing the prompt twice.
    system_prompt = None
    if system_prompts:
        first = system_prompts[0]
        if isinstance(first, list):
            system_prompt = "\n\n".join(first)
            ext["system_prompt_block_lengths"] = [len(b) for b in first]
            ext["system_prompt_join"] = "\n\n"
        else:
            system_prompt = first
    return {
        "system_prompt": system_prompt,
        "tools_enabled": tool_names or None,
        "ext_claude_code": ext,
    }


def read_source(path: Path, role: str = "primary") -> SourceRead:
    """Read one session JSONL under the input contract.

    Contributing sources (subagent logs) tolerate a zero-record file: an empty
    subagent log is a real thing a killed harness leaves behind, and refusing to
    convert the parent because of it would fail closed on the wrong artefact.
    The empty source still appears in the record, with zero counts.

    Every emitted timestamp (event, session/agent start+end) derives from the
    record-level `timestamp`, so it is put in the §3.5 form once here, by
    timestamps.to_spec_utc (ruled 2026-11-08, as for the pre-processors of
    2026-10-29): Claude Code's milliseconds padded to six digits, an offset
    converted to UTC, a leap second read as 23:59:59.999999; a value it cannot
    read (an invalid calendar date, malformed, not a string) is null and
    counted in one warning line for the file. The shared reader's own
    contract stays a faithful read.
    """
    src = read_jsonl(path, role=role, require_records=(role == "primary"))
    take_refused()
    for rec in src.records:
        if isinstance(rec, dict) and "timestamp" in rec:
            rec["timestamp"] = to_spec_utc(rec["timestamp"])
    if (n := take_refused()):
        print(f"warning: claude-code: {field(str(path))}: {n} timestamp(s) not convertible to the "
              f"§3.5 form, written as null", file=sys.stderr)
    return src


def build_session_block(records: list[dict], session_path: Path,
                        spawn_map: dict[str, str] | None = None,
                        source: SourceRead | None = None) -> dict:
    """Convert a list of raw JSONL records into a session-shaped block.

    Used for both the parent (as the container scaffold) and each subagent
    (as an entry in container.subagents[])."""
    # Pull session-level metadata from the first record carrying it.
    cwd = git_branch = harness_version = ai_title = model_observed = None
    session_id = None
    for r in records:
        if session_id is None and _key(r.get("sessionId")):
            session_id = r["sessionId"]
        if cwd is None and r.get("cwd"):
            cwd = r["cwd"]
        if git_branch is None and r.get("gitBranch"):
            git_branch = r["gitBranch"]
        if harness_version is None and r.get("version"):
            harness_version = r["version"]
        if r.get("type") == "ai-title" and ai_title is None:
            ai_title = r.get("aiTitle")
    # Models: environment.model is the first real model; a trace
    # whose assistant records are all `<synthetic>` keeps that value. The
    # per-switch markers are flagged in the event loop below.
    models = [m for r in records if r.get("type") == "assistant"
              if isinstance(m := _obj(r.get("message")).get("model"), str)]
    real_models = [m for m in models if m and m != SYNTHETIC_MODEL]
    model_observed = real_models[0] if real_models else next((m for m in models if m), None)
    models_in_order = list(dict.fromkeys(real_models))
    synthetic_count = models.count(SYNTHETIC_MODEL)

    # Derive start/end from observed timestamps.
    timestamps = sorted(
        t for r in records if isinstance(t := r.get("timestamp"), str) and t
    )
    started_at = timestamps[0] if timestamps else None
    ended_at = timestamps[-1] if timestamps else None

    # Reshape every record (drops discards + ai-title). Records dropped here
    # parsed cleanly and are discarded by adapter policy, which is a different
    # statement from a record that could not be read; the fidelity record keeps
    # the two apart (source-fidelity proposal §3.2).
    # Mode segments (spec §4.5) come from the `permissionMode` a user prompt
    # was submitted under: it is observed and carries the prompt's timestamp.
    # The standalone `permission-mode` records have no timestamp and their
    # file position does not track when a mode took effect, so they are
    # discarded above. A prompt whose mode differs from the previous prompt's
    # (the first prompt included) is flagged; the DSL emits the marker there.
    # File order, because that is the order the prompts were submitted in.
    # Model switches, in file order like the modes: an assistant turn whose
    # real model differs from the previous real model carries the previous
    # one as `model_shift_from`; the DSL emits an x-model_shift marker there.
    # `<synthetic>` turns neither open a segment nor become the tracked model.
    events = []
    last_permission_mode = None
    last_real_model = None
    # One API response written as N records (one per content block) repeats
    # its message.usage on each; the DSL sums usage per assistant_turn, so it
    # would count that call N times. Usage stays on the last record of
    # each message.id in file order and is removed from the others, so their
    # usage paths read as absent, which accumulate adds as nothing (DSL rc3
    # §8.2.5); a null left in place would show as an unmapped `usage` path.
    # A record without message.id keeps its usage: nothing to pair it with.
    usage_holder: dict[str, dict] = {}
    for r in records:
        ev = reshape_event(r, harness_version, spawn_map=spawn_map)
        if ev is not None and ev["event_kind"] == "assistant_turn":
            mid = _obj(r.get("message")).get("id")
            if isinstance(mid, str) and mid:
                prev = usage_holder.get(mid)
                if prev is not None:
                    prev.pop("usage", None)
                usage_holder[mid] = ev
        if ev is None:
            if source is not None:
                source.note_discard(r.get("type"))
            continue
        if ev["event_kind"] == "attachment" and _key(ev.get("attachment_subtype")) in HOISTED_ATTACHMENTS:
            # consumed by collect_harness_context(), not a conversation event
            if source is not None:
                source.note_discard(f"attachment.{ev['attachment_subtype']}.hoisted")
            continue
        if ev["event_kind"] == "user_prompt" and ev.get("permission_mode"):
            if ev["permission_mode"] != last_permission_mode:
                ev["permission_mode_changed"] = True
            last_permission_mode = ev["permission_mode"]
        if ev["event_kind"] == "assistant_turn":
            model = ev.get("model")
            if isinstance(model, str) and model and model != SYNTHETIC_MODEL:
                if last_real_model is not None and model != last_real_model:
                    ev["model_shift_from"] = last_real_model
                last_real_model = model
        events.append(ev)
    if source is not None:
        source.note_emitted(len(events))
    events = order_events(events)

    # Subagent identity, when present.
    agent_id = None
    slug = None
    # The first string agentId, as the spawn map in build_containers_with_fidelity
    # takes it, so the parent's Agent-call link names this container's trace.
    for r in records:
        if agent_id is None and _key(r.get("agentId")):
            agent_id = r["agentId"]
        if r.get("slug"):
            slug = r["slug"]
        if agent_id and slug:
            break

    context = collect_harness_context(records)
    # Named only when there is something to say, so single-model traces keep
    # their ext block (and content_hash) unchanged: absent = one real model
    # and no synthetic records.
    if len(models_in_order) > 1 or synthetic_count:
        context["ext_claude_code"] = {**context["ext_claude_code"],
                                      "models_observed": models_in_order,
                                      "synthetic_assistant_records": synthetic_count}

    return {
        "session_id": session_id,
        "session_path": str(session_path),
        "harness_version": harness_version,
        "harness_format": "claude-code-jsonl-v2",
        "cwd": cwd,
        "git_branch": git_branch,
        "ai_title": ai_title,
        "model_observed": model_observed,
        "session_started_at": started_at,
        "session_ended_at": ended_at,
        "events": events,
        "agent_id": agent_id,
        "slug": slug,
        **context,
    }


def parent_trace_id(session_id: str) -> str:
    return f"claude-code-agent-{session_id}"


def subagent_trace_id(session_id: str, agent_id: str) -> str:
    return f"claude-code-subagent-{session_id}-{agent_id}"


def _extract_spawn_prompt(records: list[dict]) -> str | None:
    """Return the first user record's content (the spawn prompt). Subagent
    JSONLs always begin with a user record carrying the prompt that spawned
    the subagent; matches verbatim against the parent's Agent tool_use's
    `input.prompt`."""
    for r in records:
        if r.get("type") != "user":
            continue
        content = _obj(r.get("message")).get("content")
        if isinstance(content, str):
            return content
        return None
    return None


def build_containers(parent_path: Path) -> list[dict]:
    """Build N+1 sibling containers for one parent JSONL session.

    Kept as the container-only entry point; `build_containers_with_fidelity`
    returns the same containers plus the source-fidelity assertion.
    """
    return build_containers_with_fidelity(parent_path)[0]


def build_containers_with_fidelity(parent_path: Path) -> tuple[list[dict], dict]:
    """Build N+1 sibling containers plus the fidelity assertion for the read.

    Output is one parent container plus one container per discovered subagent
    (i.e. per file in `<stem>/subagents/*.jsonl`). All containers share the
    parent's session_id; subagent containers carry an agent_id that disambiguates
    them. The DSL discriminates on container.kind.

    The parent JSONL is the `primary` source; each subagent JSONL is a
    `contributing` one, since the parent trace is not a function of the parent
    file alone — the spawn map built from the subagent files feeds the parent's
    reshape (source-fidelity proposal §3.1).
    """
    parent_source = read_source(parent_path, role="primary")
    parent_records = parent_source.records
    # A session file can hold records and still no conversation, e.g. only
    # file-history-snapshot records. Everything would be discarded, and a
    # container with no session_id crashes later in the pipeline; fail closed
    # here with a declared reason instead (converter input contract §2).
    types: dict[str, int] = {}
    for r in parent_records:
        t = r.get("type") if isinstance(r.get("type"), str) else "other"
        types[t] = types.get(t, 0) + 1
    detail = ", ".join(f"{t}={n}" for t, n in sorted(types.items()))[:200]
    if not any(classify_event_kind(r) is not None for r in parent_records):
        raise SourceUnreadable("no-emittable-records", parent_path, detail)
    # Ids count only as strings (ruled 2026-10-27): a number, boolean, list or
    # object read as absent, never formatted into a trace id.
    parent_sid = next(
        (r["sessionId"] for r in parent_records if _key(r.get("sessionId"))),
        None,
    )
    # Records that do not identify the session (older versions wrote files
    # holding only `summary` records, or no string sessionId) give no trace
    # id; same failure path.
    if parent_sid is None:
        raise SourceUnreadable("no-session-id", parent_path, detail)
    parent_tid = parent_trace_id(parent_sid)

    # Build subagent containers first so the parent's subagents[] summary can
    # cite each subagent's trace_id, AND so we can build the spawn-map for
    # parent's tool_use blocks before reshaping parent records.
    subagents_dir = parent_path.with_suffix("") / "subagents"
    subagent_containers: list[dict] = []
    subagents_summary: list[dict] = []
    spawn_map: dict[str, str] = {}
    sa_raw_records: list[tuple[Path, SourceRead, str | None]] = []
    if subagents_dir.is_dir():
        # Root = the directory holding the session file, so a symlinked
        # <session>/ or subagents/ directory is refused too, not only a
        # symlinked file inside it.
        for sa_path in keep_within_tree(sorted(subagents_dir.glob("*.jsonl")), parent_path.parent):
            sa_source = read_source(sa_path, role="contributing")
            spawn_prompt = _extract_spawn_prompt(sa_source.records)
            sa_raw_records.append((sa_path, sa_source, spawn_prompt))

        # First pass: derive each subagent's trace_id and build the spawn_map.
        for sa_path, sa_source, spawn_prompt in sa_raw_records:
            sa_records = sa_source.records
            sa_agent_id = next(
                (r["agentId"] for r in sa_records if _key(r.get("agentId"))),
                sa_path.stem,
            )
            sa_tid = subagent_trace_id(parent_sid, sa_agent_id) if parent_sid else None
            if spawn_prompt and sa_tid:
                spawn_map[spawn_prompt] = sa_tid

        # Second pass: reshape subagent records (with spawn_map for their own
        # nested Agent calls, if any) and build subagent containers.
        for sa_path, sa_source, _ in sa_raw_records:
            sa_block = build_session_block(sa_source.records, sa_path,
                                           spawn_map=spawn_map, source=sa_source)
            _report_tool_inputs(sa_path)
            if not sa_block.get("agent_id"):
                sa_block["agent_id"] = sa_path.stem
            sa_agent_id = sa_block["agent_id"]
            sa_tid = subagent_trace_id(parent_sid, sa_agent_id) if parent_sid else None
            sa_container = {
                "kind": "subagent",
                "session_id": parent_sid,
                "agent_id": sa_agent_id,
                "slug": sa_block.get("slug"),
                "trace_id": sa_tid,
                "trace_title": f"[subagent] {sa_block.get('slug') or sa_agent_id}",
                "parent_trace_id": parent_tid,
                "session_path": sa_block["session_path"],
                "harness_version": sa_block["harness_version"],
                "harness_format": sa_block["harness_format"],
                "cwd": sa_block["cwd"],
                "git_branch": sa_block["git_branch"],
                "ai_title": None,
                "model_observed": sa_block["model_observed"],
                "system_prompt": sa_block["system_prompt"],
                "tools_enabled": sa_block["tools_enabled"],
                "ext_claude_code": sa_block["ext_claude_code"],
                "session_started_at": sa_block["session_started_at"],
                "session_ended_at": sa_block["session_ended_at"],
                "events": sa_block["events"],
                "subagents": [],
                "subagents_present": False,
            }
            subagent_containers.append(sa_container)
            subagents_summary.append({
                "agent_id": sa_agent_id,
                "slug": sa_block.get("slug"),
                "trace_id": sa_tid,
                "harness_version": sa_block["harness_version"],
                "agent_started_at": sa_block["session_started_at"],
                "agent_ended_at": sa_block["session_ended_at"],
            })

    # NOW build the parent block, with the populated spawn_map so parent's
    # Agent tool_use blocks get linked back to their subagent trace_ids.
    parent_block = build_session_block(parent_records, parent_path,
                                       spawn_map=spawn_map, source=parent_source)
    _report_tool_inputs(parent_path)

    # Synthetic session_init event prepended to each container's events list:
    # gives the DSL a stable anchor to fire trace-once rules (lineage edges)
    # without relying on whichever real record happens to land first.
    parent_init = [{"event_kind": "session_init", "kind": "parent"}]
    parent_container = {
        "kind": "parent",
        "session_id": parent_sid,
        "trace_id": parent_tid,
        "trace_title": parent_block.get("ai_title") or parent_sid,
        "parent_trace_id": None,
        "session_path": parent_block["session_path"],
        "harness_version": parent_block["harness_version"],
        "harness_format": parent_block["harness_format"],
        "cwd": parent_block["cwd"],
        "git_branch": parent_block["git_branch"],
        "ai_title": parent_block["ai_title"],
        "model_observed": parent_block["model_observed"],
        "system_prompt": parent_block["system_prompt"],
        "tools_enabled": parent_block["tools_enabled"],
        "ext_claude_code": parent_block["ext_claude_code"],
        "session_started_at": parent_block["session_started_at"],
        "session_ended_at": parent_block["session_ended_at"],
        "events": parent_init + parent_block["events"],
        "subagents": subagents_summary,
        "subagents_present": bool(subagents_summary),
    }
    for sa_c in subagent_containers:
        sa_c["events"] = [
            {"event_kind": "session_init", "kind": "subagent"}
        ] + sa_c["events"]

    # The roster names only checks the reader actually ran. The chain checks are
    # not implemented here yet, and saying so is the point: an omitted check and
    # a clean one must not be byte-indistinguishable (proposal §2.1).
    assertion = build_assertion(
        [parent_source] + [src for _, src, _ in sa_raw_records],
        asserted_by=ASSERTED_BY,
        converter_version=PREPROCESSOR_VERSION,
        asserted_at=asserted_at_now(),
        extra_checks=[
            {"check": "parent-ref", "status": "not-implemented"},
            {"check": "acyclicity", "status": "not-implemented"},
            {"check": "tool-call-pairing", "status": "not-implemented"},
            {"check": "session-identity", "status": "not-implemented"},
        ],
    )
    return [parent_container, *subagent_containers], assertion


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <input.jsonl> <output.json>", file=sys.stderr)
        return 1
    inp = Path(sys.argv[1])
    outp = Path(sys.argv[2])
    try:
        containers, fidelity = build_containers_with_fidelity(inp)
    except SourceUnreadable as exc:
        # Fail closed with a declared reason rather than a traceback: an
        # unreadable source is the absence of a trace, not a damaged one.
        print(json.dumps({"error": "source-unreadable", "reason": exc.reason,
                          "path": exc.path, "detail": exc.detail}),
              file=sys.stderr)
        return 2

    outp.write_text(json.dumps(containers, indent=2, ensure_ascii=False))
    fidelity_path = outp.with_suffix(".fidelity.json")
    fidelity_path.write_text(json.dumps(fidelity, indent=2, ensure_ascii=False))

    parent = containers[0]
    subagents = containers[1:]
    totals = {k: sum(s[k] for s in fidelity["sources"]) for k in
              ("record_count_read", "record_count_skipped",
               "record_count_discarded", "record_count_emitted")}
    print(
        f"wrote {len(containers)} container(s): parent session_id={field(parent['session_id'])} "
        f"events={len(parent['events'])} "
        f"+ {len(subagents)} subagent container(s) "
        f"version={field(parent['harness_version'])} "
        f"to {outp}",
        file=sys.stderr,
    )
    print(
        f"fidelity: {len(fidelity['sources'])} source(s) "
        f"read={totals['record_count_read']} "
        f"skipped={totals['record_count_skipped']} "
        f"discarded={totals['record_count_discarded']} "
        f"emitted={totals['record_count_emitted']} "
        f"read_findings={len(fidelity['read_findings'])} "
        f"to {fidelity_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main())
