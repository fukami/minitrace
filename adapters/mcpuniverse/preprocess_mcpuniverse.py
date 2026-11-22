#!/usr/bin/env python3
"""mcpuniverse tracer output → minitrace-DSL container pre-processor.

Reads one captured mcpuniverse cell (a miniobserve run directory, or a bare
tracer file) and emits a JSON array with ONE container — one minitrace
agent-session trace via mcpuniverse.dsl.yaml.

Source shape (mcpuniverse fork 7872437b, MCP-SafetyBench campaign):

  - `raw/raw/trace.jsonl` is NOT JSON Lines despite the name. FileCollector
    writes pretty-printed JSON span objects separated by lines of dashes.
    We split on `^-{10,}$` and parse each chunk; unparseable chunks (a cell
    killed mid-flush) are counted, not dropped silently.
  - Span keys: id, parent_id, trace_id, span_index, timestamp (float epoch),
    running_time (float), records[]. Record keys: data, timestamp.
  - `data.type` ∈ {llm, agent, tool} — set in mcpuniverse BASE classes
    (llm/base.py, agent/base.py, mcp/client.py), so the dispatch covers all
    eight agent classes and all provider classes, not just ReAct/Ollama.
  - The agent record is flushed LAST, at agent cleanup. This code identifies
    it purely by data.type == "agent" (chronologically last such record
    wins); parent_id is NOT read or checked anywhere here, so root-ness is
    not validated — in the native format the root span happens to satisfy
    parent_id == id, but this adapter does not rely on that. A killed cell
    has NO agent record — that absence is a fact the trace must carry, so it
    is surfaced as container.agent_record_present = false, never papered
    over.
  - agent-record `config` / `llm_config` are Python-repr strings (single
    quotes), not JSON: parsed with ast.literal_eval, never json.loads.
  - The tool record written on the json.JSONDecodeError path omits `server`
    (mcpuniverse/agent/base.py:463); all tool-record reads tolerate absence.

Cell context (present when the input is a miniobserve run directory):

  - raw/output.json     — binding (model/seed/repo_commit), cell verdicts,
                          task metadata, wall_seconds. ABSENT on killed cells.
  - run-record.json     — run_id, outcome, framework image + exit code.

Failure honesty: a cell where every LLM call errored (dead endpoint) or that
the watchdog killed still converts — with contains_error, session_status,
error annotations, and empty assistant turns carried verbatim. A converter
that drops empty responses would manufacture a short well-behaved session out
of a total apparatus failure: the false-green class.

Usage:
    python preprocess_mcpuniverse.py <cell-dir-or-trace-file> <output.json>
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from timestamps import spec_ts  # noqa: E402

_SPAN_SEPARATOR_RE = re.compile(r"(?m)^-{10,}\s*$")


def iso_from_epoch(s: float | int | None) -> str | None:
    if s is None:
        return None
    try:
        d = dt.datetime.fromtimestamp(float(s), tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return spec_ts(d)


def parse_spans(text: str) -> tuple[list[dict], int]:
    """Split the dash-separated tracer file into span dicts.

    Returns (spans, unparseable_chunk_count). A truncated tail chunk (cell
    killed mid-flush) counts as unparseable rather than raising.
    """
    spans: list[dict] = []
    unparseable = 0
    for chunk in _SPAN_SEPARATOR_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            unparseable += 1
            continue
        if isinstance(obj, dict):
            spans.append(obj)
        else:
            unparseable += 1
    return spans, unparseable


def parse_pyrepr(raw: Any) -> dict:
    """Parse the agent record's Python-repr config strings.

    dump_config() str()s nested config objects, so these are repr strings
    with single quotes — ast.literal_eval territory, not json.loads. An
    already-parsed dict passes through; anything unparseable returns {}.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        val = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    return val if isinstance(val, dict) else {}


def extract_result_text(response: Any) -> tuple[str, bool, bool]:
    """Flatten a tool-record response into text.

    Returns (text, is_error_flag, structured_present). The response is
    usually an MCP CallToolResult dump: {content: [{type, text}...], isError,
    meta}; a bare string or anything else is stringified verbatim.
    """
    if isinstance(response, dict):
        parts = [
            b.get("text", "")
            for b in response.get("content") or []
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        is_error = bool(response.get("isError", False))
        structured = "structuredContent" in response
        return "\n".join(parts), is_error, structured
    if response is None:
        return "", False, False
    return str(response), False, False


def load_cell_context(cell_dir: Path) -> dict:
    """Read output.json / run-record.json when the input is a run directory.

    Every field defaults to None (absent measurement ≠ zero / empty)."""
    ctx: dict[str, Any] = {
        "output_json_present": False,
        "binding": {},
        "verdicts": None,
        "task": None,
        "task_category": None,
        "attack_category": None,
        "question": None,
        "carrier_keys_present": None,
        "wall_seconds": None,
        "apparatus_failure": None,
        "run_id": None,
        "run_outcome": None,
        "framework_image": None,
        "framework_exit_code": None,
        "framework_started_at": None,
        "framework_exited_at": None,
    }
    out_path = cell_dir / "raw" / "output.json"
    if out_path.exists():
        try:
            out = json.loads(out_path.read_text())
        except json.JSONDecodeError:
            out = None
        if isinstance(out, dict):
            ctx["output_json_present"] = True
            ctx["binding"] = out.get("binding") or {}
            ctx["wall_seconds"] = out.get("wall_seconds")
            ctx["apparatus_failure"] = out.get("apparatus_failure")
            cells = out.get("cells") or []
            if cells and isinstance(cells[0], dict):
                cell = cells[0]
                ctx["task"] = cell.get("task")
                ctx["verdicts"] = cell.get("verdicts")
                meta = cell.get("metadata") or {}
                ctx["task_category"] = meta.get("category")
                ctx["attack_category"] = meta.get("attack_category")
                ctx["question"] = meta.get("question")
                ctx["carrier_keys_present"] = meta.get("carrier_keys_present")
    rr_path = cell_dir / "run-record.json"
    if rr_path.exists():
        try:
            rr = json.loads(rr_path.read_text())
        except json.JSONDecodeError:
            rr = None
        if isinstance(rr, dict):
            ctx["run_id"] = rr.get("run_id")
            ctx["run_outcome"] = rr.get("outcome")
            fw = rr.get("framework") or {}
            ctx["framework_image"] = fw.get("image")
            ctx["framework_exit_code"] = fw.get("exit_code")
            ctx["framework_started_at"] = fw.get("started_at")
            ctx["framework_exited_at"] = fw.get("exited_at")
    return ctx


def flatten_events(spans: list[dict]) -> tuple[list[dict], dict]:
    """Walk spans chronologically and emit DSL-ready event records.

    Chronology comes from record timestamps, not file order: the agent
    record is flushed last and is identified here by data.type == "agent"
    alone (last one chronologically wins; parent_id is not read). Each span
    in the observed corpus carries exactly one record; multi-record spans
    are handled by iterating records anyway.
    """
    flat: list[tuple[float, int, dict, dict]] = []
    order = 0
    for span in spans:
        for rec in span.get("records") or []:
            data = rec.get("data")
            if not isinstance(data, dict):
                continue
            ts = rec.get("timestamp") or span.get("timestamp")
            flat.append((float(ts or 0), order, span, data))
            order += 1
    flat.sort(key=lambda t: (t[0], t[1]))

    events: list[dict] = []
    stats = {
        "llm_call_count": 0,
        "llm_error_count": 0,
        "empty_response_count": 0,
        "tool_call_count": 0,
        "tool_error_count": 0,
        "agent_record": None,
        "agent_class": None,
        "llm_class": None,
        "llm_config": {},
        "native_trace_id": None,
        "first_ts": None,
        "last_ts": None,
    }
    for ts, _order, span, data in flat:
        if stats["native_trace_id"] is None and span.get("trace_id"):
            stats["native_trace_id"] = span.get("trace_id")
        if ts:
            stats["first_ts"] = ts if stats["first_ts"] is None else min(stats["first_ts"], ts)
            stats["last_ts"] = ts if stats["last_ts"] is None else max(stats["last_ts"], ts)
        dtype = data.get("type")

        if dtype == "llm":
            stats["llm_call_count"] += 1
            call_index = stats["llm_call_count"]
            error = data.get("error") or ""
            response = data.get("response")
            response_text = response if isinstance(response, str) else (
                json.dumps(response, ensure_ascii=False) if response is not None else ""
            )
            if stats["llm_class"] is None:
                stats["llm_class"] = data.get("class")
            if not stats["llm_config"] and isinstance(data.get("config"), dict):
                stats["llm_config"] = data["config"]
            for msg in data.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                events.append({
                    "event_kind": "input_message",
                    "role": msg.get("role") or "user",
                    "content": msg.get("content") or "",
                    "llm_call_index": call_index,
                    "timestamp": iso_from_epoch(ts),
                    "span_id": span.get("id"),
                })
            events.append({
                "event_kind": "assistant_response",
                "content": response_text,
                "response_empty": response_text == "",
                "error": error,
                "has_error": error != "",
                "llm_class": data.get("class"),
                "llm_call_index": call_index,
                "timestamp": iso_from_epoch(ts),
                "span_id": span.get("id"),
                "span_index": span.get("span_index"),
                "running_time": span.get("running_time"),
            })
            if error:
                stats["llm_error_count"] += 1
            if response_text == "":
                stats["empty_response_count"] += 1

        elif dtype == "tool":
            stats["tool_call_count"] += 1
            if stats["agent_class"] is None:
                stats["agent_class"] = data.get("class")
            error = data.get("error") or ""
            result_text, is_error, structured = extract_result_text(data.get("response"))
            success = (error == "") and not is_error
            if not success:
                stats["tool_error_count"] += 1
            server = data.get("server")  # absent on the JSONDecodeError path
            tool_name = data.get("tool_name") or "unknown"
            events.append({
                "event_kind": "tool_call",
                "id": span.get("id"),
                "server": server,
                "tool_name": tool_name,
                "canonical_name": f"{server}__{tool_name}" if server else tool_name,
                "arguments": data.get("arguments"),
                "result_text": result_text,
                "is_error": is_error,
                "error": error,
                "success": success,
                "structured_content_present": structured,
                "agent_class": data.get("class"),
                "timestamp": iso_from_epoch(ts),
                "span_index": span.get("span_index"),
                "running_time": span.get("running_time"),
            })

        elif dtype == "agent":
            stats["agent_record"] = data
            stats["agent_class"] = data.get("class") or stats["agent_class"]
            response = data.get("response")
            response_text = response if isinstance(response, str) else (
                json.dumps(response, ensure_ascii=False) if response is not None else ""
            )
            events.append({
                "event_kind": "agent_final",
                "response": response_text,
                "response_type": data.get("response_type"),
                "error": data.get("error") or "",
                "has_error": bool(data.get("error")),
                "agent_class": data.get("class"),
                "agent_name": data.get("name"),
                "timestamp": iso_from_epoch(ts),
            })

        else:
            events.append({
                "event_kind": "unknown",
                "native_type": dtype,
                "timestamp": iso_from_epoch(ts),
                "span_id": span.get("id"),
            })
    return events, stats


def build_container(input_arg: str, trace_path: Path, cell_dir: Path | None) -> dict:
    text = trace_path.read_text(encoding="utf-8")
    spans, unparseable = parse_spans(text)
    if not spans:
        print(f"error: no parseable spans in {trace_path}", file=sys.stderr)
        raise SystemExit(3)
    events, stats = flatten_events(spans)
    ctx = load_cell_context(cell_dir) if cell_dir else load_cell_context(Path("/nonexistent"))

    agent = stats["agent_record"]
    agent_cfg = parse_pyrepr(agent.get("config")) if agent else {}
    llm_cfg_outer = parse_pyrepr(agent.get("llm_config")) if agent else {}
    llm_cfg = stats["llm_config"] or (
        llm_cfg_outer.get("config") if isinstance(llm_cfg_outer.get("config"), dict) else {}
    )
    binding = ctx["binding"]

    model = binding.get("model") or llm_cfg.get("model_name")
    provider = binding.get("provider")
    if provider is None and stats["llm_class"] == "OllamaModel":
        provider = "ollama"

    # A killed cell has no output.json (and so no binding.repo_commit), but
    # the pin is still observed in run-record.json's framework image tag
    # (capture/mcpsafety:<commit>). Fall back only when the tag is hex.
    # The two sources differ in granularity (binding carries the full 40-hex
    # SHA, the image tag an 8-char short form), so the join-key field
    # repo_commit is normalized to the short form on EVERY cell, and the
    # full SHA is kept separately only where it was actually observed —
    # null elsewhere, absent measurement not zero. (Review finding
    # of the adapter review: without this, one capture run emitted
    # two pin identities.)
    repo_commit_full = binding.get("repo_commit")
    observed = repo_commit_full
    if observed is None and isinstance(ctx["framework_image"], str):
        tag = ctx["framework_image"].rsplit(":", 1)[-1]
        if re.fullmatch(r"[0-9a-f]{7,40}", tag):
            observed = tag
    repo_commit = observed[:8] if observed else None

    cell_name = cell_dir.name if cell_dir else trace_path.stem
    native_trace_id = stats["native_trace_id"]
    agent_present = agent is not None
    session_status = (
        "completed" if agent_present and ctx["output_json_present"] else "truncated"
    )
    question = ctx["question"]
    if question is None and agent and isinstance(agent.get("messages"), str):
        question = agent["messages"]

    any_error = (
        stats["llm_error_count"] > 0
        or stats["tool_error_count"] > 0
        or bool(agent and agent.get("error"))
        or session_status != "completed"
        or unparseable > 0
    )

    # Synthetic anchor for once-per-trace emission (verdict annotations,
    # status annotation) — the goose/claude-code session_init pattern.
    session_init = {
        "event_kind": "session_init",
        "timestamp": iso_from_epoch(stats["first_ts"]),
    }

    return {
        "cell_name": cell_name,
        "trace_id": f"mcpuniverse-{native_trace_id}",
        "trace_title": f"mcpuniverse {stats['agent_class'] or 'unknown-agent'} / "
                       f"{model or 'unknown-model'} — {cell_name}",
        "native_trace_id": native_trace_id,
        "source_path": input_arg,
        "source_format_versioned": "mcpuniverse-tracer-json",
        "span_count": len(spans),
        "unparseable_chunk_count": unparseable,
        "agent_class": stats["agent_class"],
        "agent_name": (agent or {}).get("name"),
        "agent_instruction": agent_cfg.get("instruction"),
        "max_iterations": agent_cfg.get("max_iterations"),
        "agent_record_present": agent_present,
        "session_status": session_status,
        "llm_class": stats["llm_class"],
        "model_observed": model,
        "provider": provider,
        "seed": llm_cfg.get("seed"),
        "temperature": llm_cfg.get("temperature"),
        "num_ctx": llm_cfg.get("num_ctx"),
        "max_completion_tokens": llm_cfg.get("max_completion_tokens"),
        "request_timeout": llm_cfg.get("request_timeout"),
        "ollama_url": llm_cfg.get("ollama_url"),
        "repo_commit": repo_commit,
        "repo_commit_full": repo_commit_full,
        "harness_version_observed": f"mcpuniverse/{repo_commit or 'unpinned'}",
        "question": question,
        "task": ctx["task"],
        "task_category": ctx["task_category"],
        "attack_category": ctx["attack_category"],
        "carrier_keys_present": ctx["carrier_keys_present"],
        "verdicts": ctx["verdicts"],
        "wall_seconds": ctx["wall_seconds"],
        "apparatus_failure": ctx["apparatus_failure"],
        "output_json_present": ctx["output_json_present"],
        "run_id": ctx["run_id"],
        "run_outcome": ctx["run_outcome"],
        "framework_image": ctx["framework_image"],
        "framework_exit_code": ctx["framework_exit_code"],
        "llm_call_count": stats["llm_call_count"],
        "llm_error_count": stats["llm_error_count"],
        "empty_response_count": stats["empty_response_count"],
        "tool_call_count": stats["tool_call_count"],
        "tool_error_count": stats["tool_error_count"],
        "any_error": any_error,
        "started_at": iso_from_epoch(stats["first_ts"]),
        "ended_at": iso_from_epoch(stats["last_ts"]),
        "events": [session_init] + events,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="mcpuniverse tracer output → minitrace DSL container preprocessor")
    p.add_argument("input", type=Path,
                   help="miniobserve cell dir (containing raw/raw/trace.jsonl) or a tracer file")
    p.add_argument("output_json", type=Path, help="Output JSON array path")
    args = p.parse_args(argv[1:])

    if args.input.is_dir():
        cell_dir = args.input
        trace_path = cell_dir / "raw" / "raw" / "trace.jsonl"
        if not trace_path.exists():
            print(f"error: no raw/raw/trace.jsonl under {cell_dir}", file=sys.stderr)
            return 2
    elif args.input.exists():
        cell_dir = None
        trace_path = args.input
    else:
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    container = build_container(str(args.input), trace_path, cell_dir)
    args.output_json.write_text(json.dumps([container], indent=2, ensure_ascii=False))

    print(
        f"mcpuniverse preprocess: 1 container ({container['cell_name']}: "
        f"spans={container['span_count']}, llm_calls={container['llm_call_count']}, "
        f"tool_calls={container['tool_call_count']}, "
        f"llm_errors={container['llm_error_count']}, "
        f"status={container['session_status']}, "
        f"unparseable_chunks={container['unparseable_chunk_count']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
