#!/usr/bin/env python3
"""aider chat-history (.aider.chat.history.md) → minitrace-DSL container preprocessor.

Reads a single aider markdown chat transcript and emits a JSON array of
exactly one container (one agent-session trace) consumed by aider.dsl.yaml.

Why the markdown transcript (not a JSON log): aider has no structured session
log. Its three history artifacts are `.aider.input.history` (user inputs only),
`.aider.llm.history` (the request side — role-prefixed messages INCLUDING
aider's built-in few-shot examples, and no response block in --message runs),
and `.aider.chat.history.md` (the complete transcript: user turns, model
reasoning, file-listing edits, applied-edit confirmations, per-response token
counts, and a session banner with the aider version + model). The markdown
transcript is the only COMPLETE source, so it is the adapter input. This makes
aider a heuristic, version-fragile adapter — precisely what drift_watch polices.

Transcript shape (aider 0.86.x, `whole` edit format):

    # aider chat started at <iso-ish ts>

    > Aider v<version>
    > Model: <model> with <edit-format> edit format
    > Git repo: .git with N files
    > Added <file> to the chat.
    ...
    #### <user message>

    <thinking-content-HASH>
    <model reasoning>
    </thinking-content-HASH>

    <assistant prose>

    <path/to/file>
    ```
    <entire new file content>
    ```

    > Tokens: <sent> sent, <recv> received.
    > Create new file? (Y)es/(N)o [Yes]: y      # only for NEW files
    > Applied edit to <file>

aider's "tool calls" are whole-file edits: the model echoes the entire file
(content_origin = model_echo), applied as NEW (a `Create new file?` prompt
precedes the apply) or MODIFY. aider has no discrete READ tool — files enter
context via the repo-map, not a tool call.

Shell commands map to EXECUTE, read off the `> Running <cmd>` console line —
the line aider writes when it ACTUALLY runs the command. Do not anchor on
`> Run shell command?`: that is the confirmation PROMPT, which is emitted even
when the answer is no, so it records an offer rather than an execution.

Two conditions gate whether a transcript can contain a shell execution at all,
both structural in aider itself (verified against aider 0.86.2 source):

  * EDIT FORMAT. `shell_commands` is appended in exactly one place,
    aider/coders/editblock_coder.py; aider/coders/wholefile_coder.py has no
    shell handling. Under the `whole` edit format — which aider auto-selects
    for small ollama models — `run_shell_commands()` is a guaranteed no-op.
  * `--yes-always`. aider/coders/base_coder.py confirms the shell prompt with
    `explicit_yes_required=True`, and aider/io.py answers such prompts "n"
    whenever `--yes-always` is set. Blanket-yes therefore auto-DECLINES shell,
    by deliberate aider design.

So a transcript with no EXECUTE is not evidence the harness never runs shell —
check the capture's edit format and yes-always first. The command's STDOUT is
NOT in the transcript (aider records only `> Added N lines of output to the
chat`), so an EXECUTE call carries the command with an empty result.

Usage:
    python preprocess_aider.py <input.chat.history.md> <output.json> \
        [--framework-meta framework-meta.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_reader import (  # noqa: E402
    SourceUnreadable, count_sum, decode_reporting, input_count, load_json_file_reporting)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timestamps import naive_local_to_utc  # noqa: E402

ADAPTER_SOURCE = "aider-chat-history-md-v1"

# The three patterns below are matched against line.rstrip(): a lazy capture
# followed by \s*$ rescans every trailing-whitespace run (quadratic on a
# crafted line). Same captures, except that a whitespace-only tail no longer
# yields a one-space "value".
RE_SESSION_START = re.compile(r"^#\s+aider chat started at\s+(.+)$")
RE_VERSION = re.compile(r"^>\s*Aider v(\S+)")
_RE_MODEL_HEAD = re.compile(r">\s*Model:\s*(\S+)\s+with")


def match_model_line(line: str) -> tuple[str, str] | None:
    r"""(model, edit_format) from a "> Model: <m> with <fmt> edit format" line.

    Same result as the regex r"^>\s*Model:\s*(\S+)\s+with\s+(.+?)\s+edit format"
    (checked by fuzzing against it), without its backtracking: that regex was
    quadratic in runs of whitespace, so one crafted history line took seconds.
    """
    m = _RE_MODEL_HEAD.match(line)
    if not m:
        return None
    t = line[m.end():]
    w = len(t) - len(t.lstrip())
    if w == 0:
        return None
    rest = t[w:]
    k = rest.find("edit format")
    while k != -1:
        if k >= 2 and rest[k - 1].isspace():
            g = rest[:k].rstrip()
            if g:
                return m.group(1), g
        k = rest.find("edit format", k + 1)
    if w >= 3 and rest.startswith("edit format"):
        return m.group(1), t[w - 2:w - 1]
    return None
RE_USER = re.compile(r"^####\s+(.*)$")
RE_THINK_OPEN = re.compile(r"^<thinking-content-[0-9a-f]+>\s*$")
RE_THINK_CLOSE = re.compile(r"^</thinking-content-[0-9a-f]+>\s*$")
# The [kKmM] suffix is REQUIRED here, not cosmetic: aider abbreviates counts once
# they pass ~1000 ("3.8k sent"), and _num() has always handled the suffix — but the
# character class did not admit it, so an abbreviated line simply failed to match
# and the turn silently scored 0 tokens, leaving cell 26 dark for a parse reason
# rather than a real absence. Small-context transcripts never hit it.
RE_TOKENS = re.compile(r"^>\s*Tokens:\s*([\d.,]+[kKmM]?)\s*sent,\s*([\d.,]+[kKmM]?)\s*received")
RE_APPLIED = re.compile(r"^>\s*Applied edit to\s+(.+)$")
RE_CREATE_NEW = re.compile(r"^>\s*Create new file\?")
RE_RUN_SHELL = re.compile(r"^>\s*Run shell command")
# The EXECUTION, not the offer. `> Run shell command?` above is the confirmation
# prompt and appears even when declined; `> Running <cmd>` is written only after
# aider actually runs it. Anchored so `> Running` alone (no command) does not match.
RE_RUNNING = re.compile(r"^>\s*Running\s+(\S.*)$")
RE_FENCE = re.compile(r"^```")


def _num(s: str) -> int | None:
    """Parse aider's token counts, which may carry thousands separators or a
    'k' or 'M' suffix (e.g. '1.2k', '1.2M'; RE_TOKENS admits both). A count
    that does not parse or leaves the §3.4 domain is null and reported, never
    0 (DSL spec §4). An abbreviated count is the nearest whole number, in
    decimal (a float gave '4.1M' as 4099999); an unabbreviated count that is
    not whole ('3.7') is null and reported like any other (input_count), not
    truncated."""
    t = s.strip().replace(",", "")
    mult = {"k": 1000, "m": 1_000_000}.get(t[-1:].lower(), 1)
    try:
        n = Decimal(t[:-1] if mult > 1 else t)
    except InvalidOperation:
        n = None
    if n is None or not n.is_finite() or abs(n) * mult > 2**53 - 1:   # reported as written
        return input_count(s, "aider token count")
    if mult > 1:
        return int((n * mult).to_integral_value())
    # Compared before any arithmetic, which would round to the context's 28 digits.
    return int(n) if n == n.to_integral_value() else input_count(s, "aider token count")


def parse_transcript(text: str) -> dict:
    """Parse an aider chat.history.md into container meta + turn records.

    Returns a dict with meta fields and a `turns` list of
    {role, content, thinking, edits:[...], tokens:{sent,recv}}.
    """
    lines = text.splitlines()
    meta: dict[str, Any] = {}
    turns: list[dict] = []

    cur: dict | None = None          # current assistant turn being built
    in_think = False
    think_buf: list[str] = []
    prose_buf: list[str] = []
    in_fence = False
    fence_buf: list[str] = []
    fence_name: str | None = None
    new_files: set[str] = set()      # files aider prompted `Create new file?` for
    prev_gt = ""                     # body of the previous `>` console line

    def flush_prose_into(turn: dict) -> None:
        # Assistant prose = non-code, non-console text. A file-name line
        # immediately preceding a fence is consumed by the fence handler, so
        # what remains here is genuine prose.
        txt = "\n".join(l for l in prose_buf).strip()
        if txt:
            turn["content"] = (turn.get("content", "") + ("\n" if turn.get("content") else "") + txt)
        prose_buf.clear()

    def close_assistant() -> None:
        nonlocal cur
        if cur is not None:
            flush_prose_into(cur)
            if cur.get("thinking_pending"):
                cur["thinking"] = cur.pop("thinking_pending")
            turns.append(cur)
            cur = None

    i = 0
    while i < len(lines):
        line = lines[i]

        m = RE_SESSION_START.match(line.rstrip())
        if m:
            meta.setdefault("started_at", m.group(1).strip())
            i += 1
            continue

        m = RE_VERSION.match(line)
        if m:
            meta.setdefault("version", m.group(1))
            i += 1
            continue

        mm = match_model_line(line)
        if mm:
            meta.setdefault("model", mm[0])
            meta.setdefault("edit_format", mm[1].strip())
            i += 1
            continue

        # Fenced code block (a file listing when preceded by a filename line).
        if RE_FENCE.match(line):
            if not in_fence:
                in_fence = True
                fence_buf = []
                # the filename is the last non-blank prose line
                fence_name = None
                for pl in reversed(prose_buf):
                    if pl.strip():
                        fence_name = pl.strip()
                        break
                # drop that filename line from prose
                if fence_name and prose_buf and prose_buf[-1].strip() == fence_name:
                    prose_buf.pop()
            else:
                in_fence = False
                if cur is not None and fence_name:
                    cur.setdefault("listings", {})[fence_name] = "\n".join(fence_buf)
                fence_name = None
            i += 1
            continue
        if in_fence:
            fence_buf.append(line)
            i += 1
            continue

        # Thinking block.
        if RE_THINK_OPEN.match(line):
            in_think = True
            think_buf = []
            i += 1
            continue
        if RE_THINK_CLOSE.match(line):
            in_think = False
            if cur is not None:
                cur["thinking_pending"] = "\n".join(think_buf).strip()
            i += 1
            continue
        if in_think:
            think_buf.append(line)
            i += 1
            continue

        # User turn.
        m = RE_USER.match(line)
        if m:
            close_assistant()
            turns.append({"role": "user", "content": m.group(1).strip(),
                          "thinking": None, "edits": [], "tokens": None})
            cur = {"role": "assistant", "content": "", "thinking": None,
                   "edits": [], "tokens": None, "listings": {}}
            i += 1
            continue

        # Console (`>`-prefixed) lines: tokens / applied-edit / create-new / shell.
        # aider announces a NEW file as two lines — `> <filename>` then
        # `> Create new file? …` — BEFORE the batch of `> Applied edit to <f>`
        # lines. So a create-new prompt names the file on the PREVIOUS `>` line,
        # not the next applied-edit; we collect those into new_files and classify
        # each applied-edit as NEW iff its file is in that set (else MODIFY).
        if line.startswith(">"):
            body = line.lstrip(">").strip()
            mt = RE_TOKENS.match(line)
            if mt and cur is not None:
                cur["tokens"] = {"sent": _num(mt.group(1)), "recv": _num(mt.group(2))}
                prev_gt = body
                i += 1
                continue
            if RE_CREATE_NEW.match(line):
                if prev_gt:
                    new_files.add(prev_gt)
                prev_gt = body
                i += 1
                continue
            ma = RE_APPLIED.match(line.rstrip())
            if ma and cur is not None:
                fname = ma.group(1).strip()
                cur["edits"].append({
                    "file": fname,
                    "op": "NEW" if fname in new_files else "MODIFY",
                    "content": (cur.get("listings") or {}).get(fname, ""),
                })
                prev_gt = body
                i += 1
                continue
            mr = RE_RUNNING.match(line.rstrip())
            if mr and cur is not None:
                cur.setdefault("shell_commands", []).append(mr.group(1))
                prev_gt = body
                i += 1
                continue
            if RE_RUN_SHELL.match(line) and cur is not None:
                # The offer. Kept as a flag only: a declined offer must NOT
                # become an EXECUTE, so the tool_call is driven by RE_RUNNING.
                cur.setdefault("shell", True)
                prev_gt = body
                i += 1
                continue
            # other console noise (banner remnants, prompts) — carry the body
            # forward as the potential filename for a following create-new prompt
            prev_gt = body
            i += 1
            continue

        # Plain text: assistant prose (only meaningful inside an assistant turn).
        if cur is not None:
            prose_buf.append(line)
        i += 1

    close_assistant()
    return {"meta": meta, "turns": turns}


def build_container(parsed: dict, source_tz: str | None = None) -> dict:
    meta = parsed["meta"]
    turns = parsed["turns"]

    started_raw = meta.get("started_at")
    version = meta.get("version")
    model = meta.get("model")           # e.g. ollama_chat/qwen3:8b
    edit_format = meta.get("edit_format")

    # aider has no session id; synthesise a stable one from the start banner.
    # The seed keeps the banner's raw text, so session ids do not move with
    # the timestamp normalisation below.
    seed = f"{started_raw}|{version}|{model}"

    # The banner is the host's local wall time with no zone (datetime.now()),
    # and the only timestamp aider writes. Spec §3.5 needs UTC. The zone is an
    # assumption: UTC by default (the container captures run in UTC; 0.86.2
    # s1 reads 03:11:02 where the vibe 2.22.0 capture of the same batch
    # records 03:11:03+00:00), or the host zone via --source-tz. Either way the
    # basis goes into provenance so the value is never read as recorded UTC.
    started = naive_local_to_utc(started_raw, source_tz or "UTC")
    if started_raw is None:
        created_at_basis = "absent"
    elif started == started_raw:
        created_at_basis = "unparsed"
    else:
        created_at_basis = f"naive_local:{source_tz}" if source_tz else "naive_local:UTC-assumed"
    session_id = "aider-" + hashlib.sha256(seed.encode()).hexdigest()[:16]

    # provider hint from the model prefix (ollama_chat/, openai/, anthropic/…).
    provider = None
    if isinstance(model, str) and "/" in model:
        provider = model.split("/", 1)[0]
    provider_hint = "openai-compatible" if provider in ("ollama", "ollama_chat", "openai") else provider

    messages: list[dict] = [{"event_kind": "session_init", "timestamp": started}]
    tok_in = tok_out = 0
    tools_enabled: set[str] = set()

    for t in turns:
        if t["role"] == "user":
            messages.append({"event_kind": "user_text",
                             "content_text": t["content"], "timestamp": None})
            continue
        # assistant
        tool_calls = []
        for idx, e in enumerate(t.get("edits") or []):
            tools_enabled.add("edit")
            tool_calls.append({
                "id": f"{session_id}-edit-{len(messages)}-{idx}",
                "tool_name": "edit",
                "operation_type": e["op"],           # NEW / MODIFY
                "command": None,
                "file_path": e["file"],
                "arguments": {"file": e["file"], "edit_format": edit_format},
                "result": e.get("content", ""),
                "error": None,
                "success": True,
                "duration_ms": None,
                # aider edits are whole-file listings the MODEL emits, then
                # aider writes them: the content originates from the model.
                "content_origin": "model_echo",
                "aider_edit_format": edit_format,
            })
        # Shell executions, emitted AFTER the edits of the same turn: aider runs
        # suggested commands once the reply (and any edits in it) is processed.
        # Sourced from `> Running <cmd>`, so a declined offer yields nothing.
        for idx, cmd in enumerate(t.get("shell_commands") or []):
            tools_enabled.add("shell")
            tool_calls.append({
                "id": f"{session_id}-shell-{len(messages)}-{idx}",
                "tool_name": "shell",
                "operation_type": "EXECUTE",
                "command": cmd,
                "file_path": None,
                "arguments": {"command": cmd},
                # aider records only "Added N lines of output to the chat", never
                # the output itself, so the result is structurally unavailable.
                "result": "",
                "error": None,
                "success": True,
                "duration_ms": None,
                # The command text is the MODEL's, echoed by aider when running it.
                "content_origin": "model_echo",
                "aider_edit_format": edit_format,
            })
        if t.get("tokens"):
            tok_in = count_sum(tok_in, t["tokens"]["sent"], "aider tokens sent")
            tok_out = count_sum(tok_out, t["tokens"]["recv"], "aider tokens received")
        messages.append({
            "event_kind": "assistant_turn",
            "content_text": t.get("content", "") or "",
            "thinking": t.get("thinking"),
            "tool_calls": tool_calls,
            "timestamp": None,
        })

    title = None
    for t in turns:
        if t["role"] == "user" and t["content"]:
            title = t["content"].split("\n", 1)[0][:120]
            break
    if not title:
        title = f"aider session {session_id[:12]}"

    return {
        "session_id": session_id,
        "trace_id": session_id,
        "trace_title": title,
        "source_format": ADAPTER_SOURCE,
        "source_format_versioned": f"{ADAPTER_SOURCE}/{version or 'unknown'}",
        "cli_version": version,
        "version_label": version or "unknown",
        "model": model,
        "model_provider": provider,
        "provider_hint": provider_hint,
        "edit_format": edit_format,
        "cwd": None,
        "created_at": started,
        "created_at_basis": created_at_basis,
        "updated_at": started,
        "tools_enabled": sorted(tools_enabled),
        "total_input_tokens": tok_in,
        "total_output_tokens": tok_out,
        "total_tokens": count_sum(tok_in, tok_out, "aider total tokens"),
        "messages": messages,
    }


def apply_framework_meta(container: dict, fm_path: Path) -> None:
    fm = load_json_file_reporting(fm_path)
    if not isinstance(fm, dict):
        return
    for field, keys in {
        "model": ("model", "model_id"),
        "cli_version": ("cli_version", "aider_version", "version"),
        "cwd": ("cwd", "working_directory"),
    }.items():
        if container.get(field):
            continue
        for k in keys:
            if fm.get(k):
                container[field] = fm[k]
                break
    if container.get("cli_version") and container.get("version_label") in (None, "unknown"):
        container["version_label"] = container["cli_version"]
        container["source_format_versioned"] = f"{ADAPTER_SOURCE}/{container['cli_version']}"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="aider chat.history.md → minitrace DSL container preprocessor")
    p.add_argument("input_path", type=Path, help="aider .aider.chat.history.md")
    p.add_argument("output_json", type=Path, help="Output JSON array path")
    p.add_argument("--framework-meta", type=Path, default=None,
                   help="framework-meta.json for env overrides (model/version/cwd)")
    p.add_argument("--source-tz", default=None, metavar="IANA_ZONE",
                   help="zone of the host that wrote the history (aider records "
                        "local time with no zone), e.g. Europe/Brussels; default: "
                        "assume UTC, as in container captures")
    args = p.parse_args(argv[1:])

    if args.source_tz and args.source_tz != "UTC":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(args.source_tz)
        except Exception as e:  # noqa: BLE001 — unknown zone, missing tz database
            print(f"error: --source-tz {args.source_tz!r} not usable: {e}", file=sys.stderr)
            return 2

    if not args.input_path.exists():
        print(f"error: input not found: {args.input_path}", file=sys.stderr)
        return 2
    try:
        text = decode_reporting(args.input_path)
    except SourceUnreadable as e:   # not a regular file, or over the shared bound
        print(f"error: {e}", file=sys.stderr)
        return 2
    parsed = parse_transcript(text)
    if not any(t["role"] == "user" for t in parsed["turns"]):
        print(f"error: no user turns parsed from {args.input_path}", file=sys.stderr)
        return 3
    container = build_container(parsed, args.source_tz)
    if args.framework_meta:
        apply_framework_meta(container, args.framework_meta)

    args.output_json.write_text(
        json.dumps([container], indent=2, ensure_ascii=False), encoding="utf-8")

    n_turns = sum(1 for m in container["messages"]
                  if m.get("event_kind") in ("user_text", "assistant_turn"))
    n_tools = sum(len(m.get("tool_calls", [])) for m in container["messages"]
                  if m.get("event_kind") == "assistant_turn")
    print(f"aider preprocess: 1 container (session={container['session_id'][:20]}, "
          f"aider={container['version_label']}, model={container['model']}, "
          f"turns={n_turns}, tools={n_tools}, tokens={container['total_tokens']})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main(sys.argv))
