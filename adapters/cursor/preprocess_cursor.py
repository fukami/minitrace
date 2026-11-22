#!/usr/bin/env python3
"""cursor-agent stream-json → minitrace-DSL container preprocessor.

Reads a single `cursor-agent --output-format stream-json` event stream (one
JSONL file, one agent session) and emits a JSON array of exactly one container
consumed by cursor.dsl.yaml.

Why the stdout stream (not a persisted log): cursor-agent leaves three records.

  1. The stdout event stream (THIS input) — the only one carrying tool RESULTS,
     timing and usage.
  2. `$CURSOR_DATA_DIR/projects/<ws>/agent-transcripts/<id>/<id>.jsonl` — carries
     tool_use blocks with arguments, and names tools distinctly where the stream
     collapses them (`Write` and `StrReplace` both appear as `editToolCall`), but
     has no results, timing or usage.
  3. `$CURSOR_CONFIG_DIR/chats/<md5(cwd)>/<sid>/store.db` — SQLite conversation
     store, the source the harness regenerates transcripts from.

The stream is therefore primary. Because it is stdout rather than harness-owned
storage, the frozen rung input is captured INTERFACE output — the same situation
as codex's exec format, and interface drift is what drift_watch polices.

Stream shape (verified across six captures: default mode x4, --mode plan, and a
--resume run inducing a mode switch):

    {"type":"system","subtype":"init","session_id":…,"cwd":…,"model":…,
     "permissionMode":…,"apiKeySource":…}
    {"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]}}
    {"type":"assistant","message":{…},"model_call_id":…,"timestamp_ms":…}
    {"type":"tool_call","subtype":"started","tool_call":{"<kind>ToolCall":{"args":…}}}
    {"type":"interaction_query","subtype":"request","query_type":…,"query":{…}}
    {"type":"interaction_query","subtype":"response","query_type":…,"response":{…}}
    {"type":"tool_call","subtype":"completed","tool_call":{"<kind>ToolCall":{…,"result":…}}}
    {"type":"result","subtype":"success","duration_ms":…,"usage":{…}}

Tool kinds observed: readToolCall, editToolCall, shellToolCall, globToolCall,
createPlanToolCall, switchModeToolCall. THE LIST IS OPEN — globToolCall appears
in no prior inventory and surfaced only under --mode plan. Unknown kinds are
mapped to OTHER and counted, never dropped silently.

Known version drift (2026.06.04-5fd875e -> 2026.07.23-e383d2b): the newer build
adds `tool_call.startedAtMs`, `tool_call.completedAtMs`, `tool_call.toolCallId`
and `shellToolCall.args.conversationId`. Per-tool timing is therefore ABSENT at
06.04 and must not be required.

Usage:
    python preprocess_cursor.py <stream.jsonl> <output.json> \
        [--framework-meta framework-meta.json] [--transcript <transcript.jsonl>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_reader import (  # noqa: E402
    SourceUnreadable, count_sum, decode_reporting, input_count, load_json_file_reporting, input_json_loads,
    jsonl_text_lines, repair_strings, result_as_text)
from timestamps import spec_ts  # noqa: E402

ADAPTER_SOURCE = "cursor-stream-json-v1"

# Tool kinds whose operation_type is fixed by the kind alone.
STATIC_OP_BY_KIND = {
    "readToolCall": "READ",
    "globToolCall": "READ",          # directory/pattern listing — a read surface
    "createPlanToolCall": "OTHER",
    "switchModeToolCall": "OTHER",
}

# Shell verb sets, drawn from
# adapters/codex/preprocess_codex.py::classify_operation_from_command.
#
# THE VERB SETS MATCH; THE DISPATCH DOES NOT — and dispatch determines the
# value. Do NOT claim cross-adapter operation_type comparability on the strength
# of the shared vocabulary. A 10-command differential (2026-07-25, both
# classifiers at HEAD) diverges on 4:
#
#   "ls" / "cat" (bare)          codex EXECUTE, cursor READ
#                                — codex's patterns require trailing whitespace (^cat\s)
#   "echo 'a>b'"                 codex NEW, cursor EXECUTE
#                                — codex scans the raw string for ">", so a quoted
#                                  ">" inside an argument reads as a redirect
#   "python3 -c \"open('f').read()\""  codex READ, cursor EXECUTE
#                                — codex has a python-read pattern cursor lacks
#
# cursor is the more accurate classifier in 3 of the 4 (a bare `ls` IS a read; a
# quoted ">" is NOT a redirect), so the fix is to lift a shared classifier into
# adapters/ — never to degrade this one to match. Until that happens, a
# cross-harness comparison of operation_type between codex and cursor traces
# attributes ADAPTER-DISPATCH differences to harness behaviour, which is exactly
# the confound the drift fleet exists to remove. Recorded as ledger limitation
# `operation-type-not-cross-comparable`. The frozen rungs do not exercise any of
# the divergent cases, so nothing on disk is wrong today.
SHELL_NEW_VERBS = {"touch", "mkdir", "cp", "tee"}
SHELL_MODIFY_VERBS = {"patch", "chmod", "chown"}
SHELL_READ_VERBS = {
    "cat", "head", "tail", "less", "more", "find", "ls", "tree", "wc",
    "grep", "rg", "ag", "ack", "file", "stat", "du", "df",
}
GIT_READ_SUBCOMMANDS = {"log", "show", "diff", "status", "branch", "blame"}
# As codex's SHELL_COMPOUND: a separator (; & && | ||), a line break, command
# substitution ($( or a backtick) or < means more than one verb may run, so
# the command is not READ (ruled 2026-10-21).
SHELL_COMPOUND = re.compile(r"[;&|<\n\r`]|\$\(")


def _iso(ms: Any) -> str | None:
    """Epoch milliseconds (int or numeric string) → ISO-8601 UTC, spec §3.5 form.

    Six fractional digits (§3.5 requires microseconds; the source resolution is
    milliseconds, so the last three are zero). Integer timedelta rather than
    float division, so no float error reaches the microsecond digits.
    """
    if ms is None:
        return None
    try:
        val = int(ms)
        return spec_ts(datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=val))
    except (TypeError, ValueError, OverflowError):
        return None  # e.g. 1e30 ms: no datetime holds it


def _text_of(message: Any) -> str:
    """Flatten a cursor message envelope to plain text.

    message.content is a list of typed blocks; only `text` blocks carry prose.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            parts.append(text if isinstance(text, str) else "")  # non-string: absent
    return "".join(parts)


def classify_shell(args: dict) -> str:
    """Classify operation_type for a shellToolCall.

    Prefers the harness's OWN parse (`parsingResult`) over string matching: it
    exposes executableCommands[].name and redirect flags directly. Falls back to
    a regex on the raw command only when the parse is absent or failed, which is
    what a version bump or an unparseable command would produce.
    """
    parsed = args.get("parsingResult") if isinstance(args, dict) else None
    command = (args or {}).get("command") or ""

    if isinstance(parsed, dict) and not parsed.get("parsingFailed"):
        # Output redirection wins over the verb: a command that writes to a file
        # is a write regardless of what it is called. Append (>>) is a MODIFY of
        # an existing file, truncate (>) creates/replaces it.
        redirects = parsed.get("redirects")
        if parsed.get("hasOutputRedirect") or (isinstance(redirects, list) and redirects):
            for r in redirects if isinstance(redirects, list) else []:
                blob = json.dumps(r).lower()
                if "append" in blob or ">>" in blob:
                    return "MODIFY"
            return "NEW"

        execs = parsed.get("executableCommands")
        if isinstance(execs, list) and execs:
            # Ruled 2026-10-21: READ only for one executable command and no
            # separator in the raw command (the parse may fold $(...) into
            # one); a raw command that is not a non-empty string cannot be
            # checked, so it is never READ.
            compound = len(execs) > 1 or not isinstance(command, str) or not command \
                or bool(SHELL_COMPOUND.search(command))
            first = execs[0] if isinstance(execs[0], dict) else {}
            name = first.get("name")
            name = name.lower() if isinstance(name, str) else ""  # non-string: absent
            if name in SHELL_NEW_VERBS:
                return "NEW"
            if name in SHELL_MODIFY_VERBS:
                return "MODIFY"
            if name in ("sed", "perl"):
                # in-place edit is the -i flag
                argv = [
                    (a.get("value") if isinstance(a, dict) else str(a))
                    for a in (first.get("args") if isinstance(first.get("args"), list) else [])
                ]
                if any(str(a).startswith("-i") for a in argv):
                    return "MODIFY"
                return "EXECUTE"
            if name == "git":
                argv = [
                    (a.get("value") if isinstance(a, dict) else str(a))
                    for a in (first.get("args") if isinstance(first.get("args"), list) else [])
                ]
                if argv and str(argv[0]).lower() in GIT_READ_SUBCOMMANDS:
                    return "EXECUTE" if compound else "READ"
                if argv and str(argv[0]).lower() == "apply":
                    return "MODIFY"
                return "EXECUTE"
            if name in SHELL_READ_VERBS:
                return "EXECUTE" if compound else "READ"
            return "EXECUTE"

    return _classify_shell_fallback(command)


def _classify_shell_fallback(cmd: str) -> str:
    """Regex fallback, mirroring the codex classifier, for streams whose
    parsingResult is missing or failed. A command that is not a string is
    EXECUTE, not a crash."""
    if not cmd or not isinstance(cmd, str):
        return "EXECUTE"
    low = cmd[:1024].strip().lower()
    if ">>" in cmd:
        return "MODIFY"
    if ">" in cmd:
        return "NEW"
    if re.match(r"^(tee|touch|mkdir|cp)\s", low):
        return "NEW"
    if re.match(r"^(sed|perl)\s+-i", low) or re.match(r"^(patch|chmod|chown)\s", low):
        return "MODIFY"
    if re.match(r"^git\s+apply\b", low):
        return "MODIFY"
    if SHELL_COMPOUND.search(cmd):
        return "EXECUTE"   # ruled 2026-10-21: whole string, quoted text too
    if re.match(r"^git\s+(log|show|diff|status|branch|blame)\b", low):
        return "READ"
    if re.match(r"^(cat|head|tail|less|more|find|ls|tree|wc|grep|rg|ag|ack|file|stat|du|df)\s", low):
        return "READ"
    return "EXECUTE"


def classify_edit(result_success: dict | None) -> str:
    """NEW vs MODIFY for an editToolCall.

    Structural, not heuristic: the harness omits `beforeFullFileContent` entirely
    when the file did not previously exist. (A diffString beginning
    `--- /dev/null` says the same thing but is a string test on a rendered diff.)
    """
    if not isinstance(result_success, dict):
        return "MODIFY"
    return "MODIFY" if "beforeFullFileContent" in result_success else "NEW"


def tool_kind_of(tool_call: Any) -> str | None:
    """The single `<name>ToolCall` key inside a tool_call envelope.

    The envelope also carries non-tool fields (toolCallId, startedAtMs,
    completedAtMs, hookAdditionalContexts), so selecting by suffix rather than
    by position is required — key order is not stable across versions and is
    itself the thing under study.
    """
    if not isinstance(tool_call, dict):
        return None
    for key in tool_call:
        if key.endswith("ToolCall"):
            return key
    return None


# Tool results written as JSON text since build_container last reported them.
_results_as_json = 0
# Tool results of an unexpected shape (a result, or a result.success, that is
# not an object) kept as text, counted the same way.
_results_odd_shape = 0


def build_tool_call(kind: str, started: dict | None, completed: dict,
                    seq: int, session_id: str) -> dict:
    """One minitrace tool_call from a started/completed pair."""
    envelope = completed
    payload = completed.get(kind) or {}
    args = payload.get("args") if isinstance(payload, dict) else {}
    args = args if isinstance(args, dict) else {}
    raw_result = payload.get("result") if isinstance(payload, dict) else None
    result = raw_result if isinstance(raw_result, dict) else {}
    # A result that is not an object (a string, a list) names no outcome: the
    # call reads as not successful, as before, and the value is kept below.
    # An empty string holds nothing to keep and is not counted.
    odd_result = raw_result not in (None, "") and not isinstance(raw_result, dict)

    # Presence of the `success` KEY is the signal, not its truthiness:
    # createPlanToolCall returns `"success": {}` — an empty object — on a
    # genuinely successful call, which a truthiness test reads as failure.
    has_success = "success" in result
    success_obj = result.get("success") if isinstance(result.get("success"), dict) else None
    # A success member that is present but not an object (a string, 0, a
    # list) still reports success, as an empty object does; its value is kept.
    odd_success = has_success and result.get("success") not in (None, "") and success_obj is None
    rejected = "rejected" in result
    errored = "error" in result

    # started-side args are the richer ones for shell (the parse lives there);
    # fall back to the completed side when the pair could not be matched.
    started_args = {}
    if started:
        sp = started.get(kind) or {}
        if isinstance(sp, dict) and isinstance(sp.get("args"), dict):
            started_args = sp["args"]
    eff_args = started_args or args

    if kind == "shellToolCall":
        operation_type = classify_shell(eff_args)
    elif kind == "editToolCall":
        operation_type = classify_edit(success_obj)
    else:
        operation_type = STATIC_OP_BY_KIND.get(kind, "OTHER")

    file_path = None
    for src in (success_obj or {}, eff_args):
        if isinstance(src, dict) and src.get("path"):
            file_path = src["path"]
            break

    command = eff_args.get("command") if kind == "shellToolCall" else None

    # Result text: prefer real output, else the harness's own message.
    # output.result is a string or null (v0.2.0): a value that is not a string
    # is written as JSON text and counted for the stream's warning (ruled
    # 2026-11-08).
    # A field is taken when present and neither null nor the empty string, so
    # a falsy value of another type (0, false, [], {}) is written as JSON text
    # and counted, not read as "". The value is kept whole: no length cut.
    global _results_as_json, _results_odd_shape  # noqa: PLW0603

    def first_of(*keys: str) -> Any:
        for k in keys:
            v = success_obj.get(k)
            if v is not None and v != "":
                return v
        return None

    result_text = ""
    if success_obj:
        if kind == "shellToolCall":
            raw = first_of("stdout")
        elif kind == "editToolCall":
            raw = first_of("diffString", "message")
        else:
            raw = first_of("message")
            if raw is None:
                raw, _ = result_as_text(success_obj)   # the harness's whole object, as text
        result_text, structured = result_as_text(raw)
        _results_as_json += structured
        result_text = "" if result_text is None else result_text
    elif odd_success or odd_result:
        # The value of an unexpected shape is the result: a string as written,
        # anything else as JSON text, and counted for the stream's warning.
        value = result.get("success") if odd_success else raw_result
        result_text = result_as_text(value)[0] or ""   # a string is returned as written
        _results_odd_shape += 1

    # A shell command that ran and exited non-zero is a failed call: its
    # stderr is the error, or "exit code N" when stderr is empty. stdout
    # stays the result. (Its full exit status and the stderr of a command
    # that succeeded are not kept.)
    exit_code = success_obj.get("exitCode") if kind == "shellToolCall" and success_obj else None
    failed_exit = isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0

    error = None
    if rejected:
        rej = result.get("rejected")
        reason = rej.get("reason") if isinstance(rej, dict) else None
        error = "rejected: " + (reason if isinstance(reason, str) and reason else "no reason given")
    elif errored:
        err = result.get("error")
        error = err if isinstance(err, str) else "null" if err is None else result_as_text(err)[0]
    elif failed_exit:
        stderr = success_obj.get("stderr")
        error = stderr if isinstance(stderr, str) and stderr else f"exit code {exit_code}"

    started_ms = envelope.get("startedAtMs") or (started or {}).get("startedAtMs")
    completed_ms = envelope.get("completedAtMs")
    duration_ms = None
    if started_ms and completed_ms:
        try:
            duration_ms = int(completed_ms) - int(started_ms)
        except (TypeError, ValueError):
            duration_ms = None
    if duration_ms is None and success_obj:
        duration_ms = success_obj.get("executionTime")

    # toolCallId: envelope-level from 2026.07.23 on, inside shell args before
    # that. Format is BACKEND-derived (tool_214c… under Composer, toolu_bdrk_…
    # under an Anthropic-served model) — treat as an opaque string, never parse.
    call_id = (envelope.get("toolCallId")
               or eff_args.get("toolCallId")
               or f"{session_id}-tool-{seq}")

    return {
        "id": call_id,
        "tool_name": kind[: -len("ToolCall")],
        "operation_type": operation_type,
        "command": command,
        "file_path": file_path,
        "arguments": eff_args,
        "result": result_text,
        "error": error,
        "success": has_success and not rejected and not errored and not failed_exit,
        "duration_ms": duration_ms,
        # cursor's edit tools write content the MODEL produced; shell output is
        # produced by local execution.
        "content_origin": "local_exec" if kind == "shellToolCall" else (
            "model_echo" if kind == "editToolCall" else None),
        "cursor_tool_kind": kind,
        "cursor_rejected": rejected,
    }


def parse_stream(text: str) -> tuple[list[dict], list[str]]:
    """Parse the JSONL stream into event dicts. Returns (events, warnings).

    A trailing unterminated line is tolerated and reported, not fatal: a capture
    truncated mid-write is exactly the kind of artefact a drift rung must still
    describe rather than refuse.
    """
    events: list[dict] = []
    warnings: list[str] = []
    # Framed as the readers frame jsonl (DSL rc3 §15): LF/CR only,
    # and a line of VT, FF or Unicode spaces is not blank but unparseable.
    for lineno, raw in jsonl_text_lines(text):
        try:
            ev = input_json_loads(raw)
        except json.JSONDecodeError as exc:
            warnings.append(f"line {lineno}: unparseable JSON ({exc.msg}) — skipped")
            continue
        except (ValueError, RecursionError):
            warnings.append(f"line {lineno}: unparseable JSON (number or nesting out of range) — skipped")
            continue
        if not isinstance(ev, dict):
            warnings.append(f"line {lineno}: not a JSON object — skipped")
            continue
        # A "\ud83d" escape parses to an unpaired surrogate, which the UTF-8
        # output write refuses; a noncharacter would reach the trace. Both are
        # replaced with U+FFFD, as the shared readers do (DSL §4).
        try:
            ev, repairs, collisions = repair_strings(ev)
        except RecursionError:   # parsed, but deeper than the repair reaches: as the readers rule
            warnings.append(f"line {lineno}: unparseable JSON (nesting too deep to check its strings) — skipped")
            continue
        if repairs:
            warnings.append(f"line {lineno}: {len(repairs)} string(s) held an unpaired surrogate or a "
                            f"noncharacter, replaced with U+FFFD at "
                            + ", ".join(r["path"][:80] or "/" for r in repairs[:5])
                            + (" ..." if len(repairs) > 5 else ""))
        if collisions:
            warnings.append(f"line {lineno}: {len(collisions)} member(s) lost: a repaired name equals "
                            f"another in its object (the later value kept)")
        events.append(ev)
    return events, warnings


def build_container(events: list[dict], warnings: list[str]) -> dict:
    global _results_as_json, _results_odd_shape  # noqa: PLW0603
    _results_as_json = 0
    _results_odd_shape = 0
    init = next((e for e in events if e.get("type") == "system"
                 and e.get("subtype") == "init"), {})
    result_ev = next((e for e in events if e.get("type") == "result"), {})

    # A session id counts only as a non-empty string (ruled 2026-10-27): any
    # other value is absent, never the trace id, a tool id or the title.
    session_id = next((v for v in (init.get("session_id"), result_ev.get("session_id"))
                       if isinstance(v, str) and v), "unknown")
    model = init.get("model")
    cwd = init.get("cwd")

    messages: list[dict] = [{"event_kind": "session_init", "timestamp": None}]
    tools_enabled: set[str] = set()
    unknown_kinds: set[str] = set()
    pending: list[tuple[str, dict]] = []     # unmatched started events
    started_at: dict[int, str | None] = {}   # id(started tool_call) -> its time
    seq = 0
    first_ts: str | None = None
    last_ts: str | None = None

    def current_assistant() -> dict:
        """The assistant turn tool calls attach to, creating a synthetic one if
        the stream opens with a tool call (observed on --resume runs, where the
        first events are user → interaction_query → tool_call)."""
        for msg in reversed(messages):
            if msg["event_kind"] == "assistant_turn":
                return msg
        synthetic = {"event_kind": "assistant_turn", "content_text": "",
                     "thinking": None, "tool_calls": [], "timestamp": None,
                     "cursor_synthetic": True}
        messages.append(synthetic)
        return synthetic

    for ev in events:
        etype = ev.get("type")

        if etype == "user":
            messages.append({"event_kind": "user_text",
                             "content_text": _text_of(ev.get("message")),
                             "timestamp": None})

        elif etype == "assistant":
            ts = _iso(ev.get("timestamp_ms"))
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            messages.append({"event_kind": "assistant_turn",
                             "content_text": _text_of(ev.get("message")),
                             "thinking": None,
                             "tool_calls": [],
                             "timestamp": ts})

        elif etype == "thinking":
            # NOT observed in any of the six reference captures (the June 2026
            # probes saw it under Composer 2.5 Fast). Mapped defensively onto the
            # owning assistant turn so it is never silently dropped if it appears.
            turn = current_assistant()
            existing = turn.get("thinking") or ""
            turn["thinking"] = existing + _text_of(ev.get("message") or ev)

        elif etype == "tool_call":
            tc = ev.get("tool_call") or {}
            kind = tool_kind_of(tc)
            if kind is None:
                warnings.append("tool_call with no <name>ToolCall key — skipped")
                continue
            if kind not in STATIC_OP_BY_KIND and kind not in (
                    "shellToolCall", "editToolCall"):
                unknown_kinds.add(kind)
            tools_enabled.add(kind[: -len("ToolCall")])

            if ev.get("subtype") == "started":
                pending.append((kind, tc))
                started_at[id(tc)] = _iso(ev.get("timestamp_ms"))
            elif ev.get("subtype") == "completed":
                started = None
                cid = tc.get("toolCallId")
                for idx, (pkind, pev) in enumerate(pending):
                    # Pair on toolCallId when the version emits it, else FIFO
                    # within the same tool kind.
                    if pkind != kind:
                        continue
                    if cid and pev.get("toolCallId") and pev["toolCallId"] != cid:
                        continue
                    started = pev
                    pending.pop(idx)
                    break
                seq += 1
                owner = current_assistant()
                if owner.get("cursor_synthetic") and not owner.get("annotation_timestamp"):
                    # The adapter's note on a turn it synthesised is dated by
                    # the first tool call it owns (started, else completed).
                    owner["annotation_timestamp"] = (
                        (started_at.get(id(started)) if started is not None else None)
                        or _iso(ev.get("timestamp_ms")))
                owner["tool_calls"].append(
                    build_tool_call(kind, started, tc, seq, session_id))

        elif etype == "interaction_query":
            # Approval handshake. Request and response use DIFFERENT key names
            # (createPlanRequestQuery -> createPlanRequestResponse), so they are
            # paired on `id`, never on key name.
            #
            # The payload BODY is deliberately not carried. For the observed
            # query types it duplicates the tool call it accompanies: a
            # createPlanRequestQuery's args.plan/args.todos are the same values
            # already captured as createPlanToolCall.arguments, so carrying both
            # would record the plan twice and inflate the trace. Only the
            # correlation fields are kept. Revisit if a query type appears whose
            # payload is NOT mirrored by a tool call.
            body = ev.get("query") or ev.get("response") or {}
            messages.append({
                "event_kind": "interaction_query",
                "subtype": ev.get("subtype"),
                "query_type": ev.get("query_type"),
                "interaction_id": body.get("id") if isinstance(body, dict) else None,
                "timestamp": _iso(ev.get("timestamp_ms")),
            })

    # Annotations need a time (spec v0.3.0 §3.5): the recorded one where the
    # stream has it, else the session start.
    for m in messages:
        if m["event_kind"] == "interaction_query" or m.get("cursor_synthetic"):
            m["annotation_timestamp"] = (m.get("annotation_timestamp")
                                         or m.get("timestamp") or first_ts)

    if pending:
        warnings.append(
            f"{len(pending)} tool_call started event(s) never completed "
            f"({', '.join(sorted({k for k, _ in pending}))})")
    if unknown_kinds:
        warnings.append(
            "unmapped tool kind(s) classified OTHER: " + ", ".join(sorted(unknown_kinds)))

    usage = result_ev.get("usage")
    if not isinstance(usage, dict):
        usage = {}  # a usage that is not an object reads as absent
    if _results_as_json:
        warnings.append(f"{_results_as_json} tool result(s) not a string, written as JSON text "
                        f"(output.result is a string or null)")
    if _results_odd_shape:
        warnings.append(f"{_results_odd_shape} tool result(s) whose result or result.success is not an "
                        f"object, kept as text in output.result")
    tok_in = input_count(usage.get("inputTokens"), "cursor usage.inputTokens", warnings)
    tok_out = input_count(usage.get("outputTokens"), "cursor usage.outputTokens", warnings)

    first_user = next((m for m in messages
                       if m["event_kind"] == "user_text" and m.get("content_text")), None)
    title = (first_user["content_text"].split("\n", 1)[0][:120]
             if first_user else f"cursor session {session_id[:12]}")

    return {
        "session_id": session_id,
        "trace_id": session_id,
        "trace_title": title,
        "source_format_versioned": ADAPTER_SOURCE,
        "cli_version": None,          # not in the stream; supplied by framework-meta
        "version_label": "unknown",
        "model": model,
        "model_provider": None,
        "provider_hint": "cursor-cloud",
        "cwd": cwd,
        "permission_mode": init.get("permissionMode"),
        "api_key_source": init.get("apiKeySource"),
        "created_at": first_ts,
        "updated_at": last_ts,
        "duration_ms": result_ev.get("duration_ms"),
        "result_subtype": result_ev.get("subtype"),
        "result_is_error": result_ev.get("is_error"),
        "tools_enabled": sorted(tools_enabled),
        "total_input_tokens": tok_in,
        "total_output_tokens": tok_out,
        "total_tokens": count_sum(tok_in, tok_out, "cursor total tokens", warnings),
        "cache_read_tokens": input_count(usage.get("cacheReadTokens"), "cursor usage.cacheReadTokens", warnings),
        "cache_write_tokens": input_count(usage.get("cacheWriteTokens"), "cursor usage.cacheWriteTokens", warnings),
        "preprocessor_warnings": warnings,
        "messages": messages,
    }


def apply_framework_meta(container: dict, fm_path: Path) -> None:
    """Overlay capture-time facts the stream does not carry (harness version)."""
    fm = load_json_file_reporting(fm_path)
    if not isinstance(fm, dict):
        return
    for field, keys in {
        "model": ("model", "model_id"),
        "cli_version": ("cli_version", "cursor_version", "version"),
    }.items():
        for key in keys:
            if fm.get(key):
                container[field] = fm[key]
                break
    if container.get("cli_version"):
        container["version_label"] = container["cli_version"]
        container["source_format_versioned"] = (
            f"{ADAPTER_SOURCE}/{container['cli_version']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("input", type=Path, help="cursor-agent stream-json JSONL")
    ap.add_argument("output", type=Path, help="container JSON to write")
    ap.add_argument("--framework-meta", type=Path, default=None)
    args = ap.parse_args()

    if not args.input.is_file():
        print(f"preprocess_cursor: no such file: {args.input}", file=sys.stderr)
        return 66

    try:
        text = decode_reporting(args.input)
    except SourceUnreadable as e:   # not a regular file, or over the shared bound
        print(f"preprocess_cursor: {e}", file=sys.stderr)
        return 66
    events, warnings = parse_stream(text)
    if not events:
        print("preprocess_cursor: stream contains no parseable events",
              file=sys.stderr)
        return 65

    container = build_container(events, warnings)
    if args.framework_meta and args.framework_meta.is_file():
        apply_framework_meta(container, args.framework_meta)

    args.output.write_text(
        json.dumps([container], indent=2, ensure_ascii=False), encoding="utf-8")

    n_turns = sum(1 for m in container["messages"]
                  if m["event_kind"] in ("user_text", "assistant_turn"))
    n_tools = sum(len(m.get("tool_calls", [])) for m in container["messages"]
                  if m["event_kind"] == "assistant_turn")
    print(f"preprocess_cursor: {len(events)} events → {n_turns} turns, "
          f"{n_tools} tool_calls, {len(container['preprocessor_warnings'])} warning(s)",
          file=sys.stderr)
    for w in container["preprocessor_warnings"]:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main())
