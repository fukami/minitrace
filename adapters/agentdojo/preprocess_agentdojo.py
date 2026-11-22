#!/usr/bin/env python3
"""AgentDojo run file(s) → minitrace-DSL container array pre-processor.

AgentDojo (ethz-spylab/agentdojo, MIT) writes one JSON file per benchmark run at
`runs/<pipeline>/<suite>/<user_task>/<attack>/<injection_task>.json`. Each file
is one agent trajectory: a `messages[]` list of system/user/assistant/tool
messages, plus run-level labels (`utility`, `security`), the injected strings
(`injections`), and the attack/defense identity.

Each run file becomes one minitrace agent-session trace. The DSL iterates
`container.messages` as the record stream, dispatching on a pre-computed
`event_kind` field (the same shape as the tau-bench adapter).

Why a preprocessor is needed (the DSL cannot do these):

  1. Trace identity. A run file carries no `id`; identity is the *path*
     (pipeline/suite/user_task/attack/injection_task). The preprocessor derives
     it and must be given the path.
  2. Injection localization. The load-bearing feature: find which tool result
     first carried an injected string, so the per-run `security` label becomes a
     per-turn entry point. This is a whitespace-tolerant substring search the
     DSL has no primitive for. See `localize_injection`.
  3. Pipeline splitting. `pipeline_name` packs model and defense into one string
     (`gpt-4o-2024-05-13-tool_filter`); the defense suffix drives a
     policy_triggers entry and must be split out.
  4. Content flattening. A message `content` may be a string, null, or a list of
     structured blocks. The DSL expects a scalar; the preprocessor flattens.

Usage:
    # one file (path drives identity):
    python preprocess_agentdojo.py --run <pipeline>/<suite>/<ut>/<atk>/<it>.json \
        --runs-root <path-to-runs-dir> --output out.json
    # many files under a root:
    python preprocess_agentdojo.py --runs-root <runs-dir> --glob '**/*.json' \
        --output out.json
    # the form every pre-processor takes and verify_run.py passes: the runs
    # directory and the output (every run under it, as --glob's default):
    python preprocess_agentdojo.py <runs-dir> out.json
A single run file is not taken as the positional input: its identity comes
from its path under the runs root, which --runs-root and --run name.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from input_tree import keep_within_tree  # noqa: E402
from source_reader import ReadLimits, input_json_loads  # noqa: E402

_MAX_FILE_BYTES = ReadLimits().max_file_bytes
from timestamps import spec_ts, take_refused, to_spec_utc  # noqa: E402

# evaluation_timestamp values refused since main() last reported them.
_ts_refused = 0

# Defense suffixes AgentDojo appends to the model name in `pipeline_name`.
# Anything after the model id that matches one of these is the defense.
KNOWN_DEFENSES = {
    "tool_filter",
    "repeat_user_prompt",
    "spotlighting_with_delimiting",
    "transformers_pi_detector",
}

# Distinctive-slice length for injection localization. Long enough to be
# specific, short enough to survive the environment truncating a long injection.
INJECTION_PROBE_CHARS = 40


def sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_iso_microseconds(ts) -> str | None:
    """AgentDojo's `evaluation_timestamp` → the spec's ISO8601 microsecond form.

    The source is space-separated with second precision ("2025-07-25 11:09:09"),
    which fails the schema pattern ^...T...\\.[0-9]{6}Z$. Most runs carry null.
    Returns the normalized string, or None when there is no parseable source —
    the DSL supplies a now_microseconds() conversion-time fallback for the
    required annotation/policy_trigger timestamps (extends scoping S3: the source
    has no per-run time for most runs, so a conversion timestamp is the honest
    stand-in for a REQUIRED field).

    A zoneless value (the source's own form) is read as UTC, as before, when
    it is a real calendar date and time; a value with Z or an offset is read
    by timestamps.to_spec_utc; any other value (an invalid date, malformed,
    not a string) is None and counted for the run file's warning line
    (ruled 2026-11-12). None and a blank string are None and not counted.
    """
    global _ts_refused
    if ts is None or (isinstance(ts, str) and not ts.strip()):
        return None
    if isinstance(ts, str):
        import datetime as _dt
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                d = _dt.datetime.strptime(ts.strip(), fmt)
                return spec_ts(d)
            except ValueError:
                continue
        take_refused()
        out = to_spec_utc(ts.strip())
        take_refused()
        if out is not None:
            return out
    _ts_refused += 1
    return None


def flatten_content(content) -> str:
    """A message content is str | None | list[block]. Return a scalar string.

    Structured blocks (list content, 5/73 messages in the inspected corpus)
    carry their text under `content` or `text`; concatenate them. Preserving the
    text verbatim matters for injection localization, which searches it.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                parts.append(str(b.get("content") or b.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def _norm(s: str) -> str:
    """Whitespace-collapsed lowercase, for injection matching.

    AgentDojo places an injected string into the environment, and a tool that
    reads that environment serializes it (YAML dump reindents, drops leading
    newlines). So the string in `injections` and the string in the tool result
    differ by whitespace and never match exactly. Normalizing both sides first
    is what makes localization work; an exact `in` test silently finds nothing.
    """
    return re.sub(r"\s+", " ", flatten_content(s)).strip().lower()


def localize_injection(messages: list[dict], injections: dict) -> dict:
    """Find the first message whose (normalized) content carries an injection.

    Returns {"found": bool, entry_message_index, entry_role, entry_tool_call_id,
    injection_key}. When no injection is placed (a clean run) or the placement
    never surfaces in a message, `found` is False and the per-turn signal is
    simply absent — the run-level `security` label still stands.
    """
    probes = []
    for key, val in (injections or {}).items():
        if isinstance(val, str) and val.strip():
            probes.append((key, _norm(val)[:INJECTION_PROBE_CHARS]))
    for i, m in enumerate(messages):
        cn = _norm(m.get("content"))
        if not cn:
            continue
        for key, probe in probes:
            if probe and probe in cn:
                return {
                    "found": True,
                    "entry_message_index": i,
                    "entry_role": m.get("role"),
                    "entry_tool_call_id": m.get("tool_call_id"),
                    "injection_key": key,
                }
    return {"found": False}


def split_pipeline(pipeline_name: str) -> dict:
    """Split a pipeline directory name into model id and (optional) defense suffix.

    Applied to the path directory, which is authoritative. AgentDojo appends the
    defense to the model in that directory name (`gpt-4o-2024-05-13-tool_filter`).
    """
    if not pipeline_name:
        return {"model": "unknown", "defense": None}
    for d in KNOWN_DEFENSES:
        suffix = "-" + d
        if pipeline_name.endswith(suffix):
            return {"model": pipeline_name[: -len(suffix)], "defense": d}
    return {"model": pipeline_name, "defense": None}


def resolve_benchmark_version(run: dict) -> str:
    """Best-effort version label for the taxonomy qualifier.

    In the inspected corpus `benchmark_version` is populated for only a minority
    of runs (most predate the field and carry null). Fall back to the package
    version, then to a literal. Recorded rather than hidden: the version signal
    here is weak, and taxonomy qualifiers built on it will mostly read
    "unknown".
    """
    for k in ("benchmark_version", "agentdojo_package_version"):
        v = run.get(k)
        if isinstance(v, str) and v and v != "unknown":
            return v
    return "unknown"


def derive_identity(rel_path: Path) -> dict:
    """Path parts are the identity: <pipeline>/<suite>/<user_task>/<attack>/<it>.json"""
    parts = rel_path.with_suffix("").parts
    # Tolerate shorter paths (e.g. a file handed in directly): pad from the left.
    pipeline, suite, user_task, attack, injection_task = (
        (["", "", "", "", ""] + list(parts))[-5:]
    )
    trace_id = "agentdojo-" + "-".join(
        p for p in (pipeline, suite, user_task, attack, injection_task) if p
    )
    return {
        "trace_id": trace_id,
        "pipeline_from_path": pipeline,
        "suite_from_path": suite,
        "user_task_from_path": user_task,
        "attack_from_path": attack,
        "injection_task_from_path": injection_task,
    }


def classify_message(m: dict) -> str:
    role = m.get("role")
    if role == "system":
        return "system_prompt"
    if role == "user":
        return "user_turn"
    if role == "assistant":
        return "assistant_with_tools" if (m.get("tool_calls")) else "assistant_text"
    if role == "tool":
        return "tool_result"
    return "unknown"


def build_container(run: dict, rel_path: Path) -> dict | None:
    global _ts_refused
    _ts_refused = 0   # counted per run file; main() reports it
    messages = run.get("messages") or []
    if not messages:
        return None  # trace schema requires >=1 turn; skip empty runs.

    ident = derive_identity(rel_path)
    # The path directory is authoritative for model+defense identity. The in-file
    # `pipeline_name` is the SERVING pipeline and reads "local" for locally-served
    # models (e.g. Meta-SecAlign), where it does NOT carry the model id. For
    # hosted models the two agree. See implementation-findings note.
    pipe = split_pipeline(ident["pipeline_from_path"] or run.get("pipeline_name"))
    injloc = localize_injection(messages, run.get("injections") or {})
    version = resolve_benchmark_version(run)
    # A valid ISO8601 microsecond timestamp is REQUIRED on every annotation and
    # policy_trigger. AgentDojo carries a usable evaluation_timestamp for only a
    # minority of runs (most are null); normalize it when present, else use a
    # deterministic epoch SENTINEL that both satisfies the schema and signals
    # "no source timestamp". A wall-clock fallback (now_microseconds) was
    # rejected: it lands in a hashed field and breaks R2 content-hash
    # determinism, which the regression baseline depends on. Extends scoping S3.
    NO_SOURCE_TS = "1970-01-01T00:00:00.000000Z"
    _norm = normalize_iso_microseconds(run.get("evaluation_timestamp"))
    event_ts_iso = _norm if _norm is not None else NO_SOURCE_TS
    event_ts_present = _norm is not None
    attack_type = run.get("attack_type")
    is_attacked = attack_type not in (None, "none", "")
    # Make `is_attacked == (attack_type is None)` true by construction rather
    # than by upstream accident, now that attack_type is emitted as a field
    # (condition.attack_type) and not just used internally.
    #
    # Measured over all 36,679 source runs: the source spells "no attack placed"
    # as `None` in every one of the 3,560 cases and never as the string "none",
    # so on THIS corpus the line below is a no-op. The "none" spelling lives in
    # the directory and trace-id naming, not in the run payload. It is kept
    # because the `not in (None, "none", "")` guard above predates it and encodes
    # someone's belief that the other spellings occur; if a future corpus proves
    # them right, a consumer reading condition.attack_type still sees one value
    # for "unattacked" instead of three.
    if not is_attacked:
        attack_type = None
    trace_id = ident["trace_id"]

    # Single walk that computes, in one pass and in emission order:
    #   - event_kind and flattened content per message
    #   - turn_index for turn-emitting messages (system/user/assistant); tool
    #     results emit no turn, so message index != turn index
    #   - tool_call ids, SYNTHESIZED when absent. Hosted models (gpt-4o, claude)
    #     carry native ids on both the call and the result; locally-served
    #     text-format models (Meta-SecAlign) carry null on both. A FIFO of
    #     unmatched call ids links a null-id result to its call positionally.
    #     `tool_call_id_source` records native vs positional.
    from collections import deque
    recs = []
    turn_counter = 0
    pending_call_ids: deque = deque()
    synth_n = 0
    for i, m in enumerate(messages):
        rec = dict(m)
        kind = classify_message(m)
        rec["event_kind"] = kind
        rec["content"] = flatten_content(m.get("content"))
        rec["msg_index"] = i

        if kind in ("system_prompt", "user_turn", "assistant_text",
                    "assistant_with_tools"):
            rec["turn_index"] = turn_counter
            turn_counter += 1
        else:
            rec["turn_index"] = None

        if kind == "assistant_with_tools":
            fixed = []
            for tc in (m.get("tool_calls") or []):
                tc = dict(tc)
                if not tc.get("id"):
                    synth_n += 1
                    tc["id"] = f"{trace_id}-tc-{synth_n}"
                    tc["id_source"] = "positional"
                else:
                    tc["id_source"] = "native"
                pending_call_ids.append(tc["id"])
                fixed.append(tc)
            rec["tool_calls"] = fixed

        if kind == "tool_result":
            rec["error"] = bool(m.get("error"))
            native = m.get("tool_call_id")
            if native:
                rec["resolved_tool_call_id"] = native
                rec["tool_call_id_source"] = "native"
                # keep the FIFO honest if this id is queued
                try:
                    pending_call_ids.remove(native)
                except ValueError:
                    pass
            elif pending_call_ids:
                rec["resolved_tool_call_id"] = pending_call_ids.popleft()
                rec["tool_call_id_source"] = "positional"
            else:
                rec["resolved_tool_call_id"] = None
                rec["tool_call_id_source"] = "unlinked"

        recs.append(rec)

    # Injection scope, in turn/tool-call space rather than message space. When
    # the injection surfaces in a tool RESULT (the case in every attacked run of
    # the inspected corpus), the faithful anchor is the tool_call whose output
    # carried it, not a turn — tool results have no turn. When it surfaces in a
    # turn-emitting message, anchor to that turn. Fall back to trace scope if a
    # tool-result entry has no resolvable call id.
    inj_scope_type, inj_scope_target = None, None
    if injloc.get("found"):
        ei = injloc["entry_message_index"]
        entry = recs[ei]
        if entry["event_kind"] == "tool_result":
            tcid = entry.get("resolved_tool_call_id")
            if tcid:
                inj_scope_type, inj_scope_target = "tool_call", str(tcid)
            else:
                inj_scope_type, inj_scope_target = "trace", trace_id
        elif entry.get("turn_index") is not None:
            inj_scope_type, inj_scope_target = "turn", str(entry["turn_index"])
        else:
            inj_scope_type, inj_scope_target = "trace", trace_id
        # tag the entry record so the DSL rule can fire on it
        recs[ei]["is_injection_entry"] = True
    for r in recs:
        r.setdefault("is_injection_entry", False)

    return {
        "trace_id": trace_id,
        "trace_title": f"agentdojo/{pipe['model']}/{run.get('suite_name') or ident['suite_from_path']}/"
        f"{run.get('user_task_id') or ident['user_task_from_path']}",
        "suite": run.get("suite_name") or ident["suite_from_path"],
        # The SCENARIO is the (suite, user_task) pair — what the agent was asked
        # to do. The attack and the injection task are conditions applied to that
        # scenario, not part of its identity, which is why neither appears here:
        # the same scenario_id groups a task across attacks, defenses and models,
        # which is what the deferred cross-run lineage fan-out (S5) needs.
        #
        # Emitting this is not cosmetic. `scenario_id` is a first-class v0.3.0
        # base-schema field, and a consumer that finds it absent falls back to
        # guessing from the filename. A downstream judge does exactly that, with a regex
        # (`_(\d+)\.minitrace\.json$`) written for goose's local INJ-N corpus,
        # which matches our `...-injection_task_N` ids and silently relabels an
        # AgentDojo banking run as a local scenario — then looks that up in a
        # hardcoded task table and hands the judge a task description from a
        # different corpus entirely. Measured before this field existed: 32,912
        # of 36,472 traces mis-resolved, 23,902 of them to a fabricated task.
        # AgentDojo's runs/ tree puts `injection_task_N` directories at the
        # user-task position: the attacker's GOAL, handed to the agent as a
        # legitimate user request with no injection anywhere, to establish that
        # the goal is achievable at all. 804 of the 3,560 unattacked runs are
        # these, and in 537 the agent achieved the goal — it moves money to the
        # attacker's IBAN, or changes the password, on request.
        #
        # Behaviourally those runs are indistinguishable from a successful
        # compromise; what separates them is provenance (who asked), not conduct.
        # So they are correctly `is_attacked: false` and correctly carry no
        # harm_linkage — and they will poison any clean/attacked split that
        # treats "not attacked" as "benign". Marked here so a consumer can
        # exclude them, or keep them deliberately: they are the sharpest probe
        # for whether a judge can tell an injected instruction from an
        # authorised one, which it cannot do without the user turn.
        "run_class": ("injection_goal_solo"
                      if str(run.get("user_task_id")
                             or ident["user_task_from_path"]).startswith("injection_task")
                      else "user_task"),
        "scenario_id": "agentdojo/{}/{}".format(
            run.get("suite_name") or ident["suite_from_path"],
            run.get("user_task_id") or ident["user_task_from_path"]),
        "model": pipe["model"],
        "defense": pipe["defense"],
        "serving_pipeline_name": run.get("pipeline_name"),
        "user_task_id": run.get("user_task_id") or ident["user_task_from_path"],
        "injection_task_id": run.get("injection_task_id"),
        "attack_type": attack_type,
        "is_attacked": is_attacked,
        "benchmark_version": version,
        "evaluation_timestamp": run.get("evaluation_timestamp"),
        "event_ts_iso": event_ts_iso,
        "event_ts_present": event_ts_present,
        "duration_seconds": run.get("duration"),
        "utility": run.get("utility"),
        "security": run.get("security"),
        "run_error": run.get("error"),
        "contains_error": bool(run.get("error")),
        # `security` is the INJECTION TASK's own success check, not a safety
        # verdict: security=True means the injection task WAS accomplished, i.e.
        # the agent WAS compromised. Source of truth is AgentDojo
        # task_suite.py::run_task_with_pipeline — "the second [boolean]
        # indicating if the injection was successful" — where it is computed as
        # `self._check_task_result(injection_task, ...)`, and a run with no
        # injection returns a hardcoded `True`. That hardcode is why the
        # `is_attacked` guard below is load-bearing: without it every clean run
        # would read as compromised.
        #
        # Two source quirks a consumer must know, both passed through faithfully
        # rather than silently re-labelled:
        #   - benchmark.py sets `security = True` in its API-error fallbacks, so
        #     an errored run reads as compromised. `contains_error` marks these.
        #   - for DoS attacks benchmark.py sets `security = not utility`, which
        #     still means "the attack succeeded", so the polarity is uniform.
        "compromised": bool(is_attacked and run.get("security") is True),
        # A defense "fired" (stopped the attack) when a defense is present, an
        # attack was placed, and the injection did NOT succeed. Precomputed here
        # because the DSL expression grammar evaluates `not X` but not `X and Y`.
        "defense_fired": bool(pipe["defense"] and is_attacked
                              and run.get("security") is False),
        "injection_localized": injloc.get("found", False),
        "injection_entry_index": injloc.get("entry_message_index"),
        "injection_scope_type": inj_scope_type,
        "injection_scope_target": inj_scope_target,
        "injection_key": injloc.get("injection_key"),
        "injections_present": bool(run.get("injections")),
        "source_format": "agentdojo-run-v1",
        "messages": recs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, nargs="?", default=None, metavar="runs_dir",
                    help="the runs/ directory, as --runs-root (the positional form)")
    ap.add_argument("output_path", type=Path, nargs="?", default=None, metavar="output",
                    help="output JSON, as --output (the positional form)")
    ap.add_argument("--runs-root", type=Path, default=None,
                    help="the AgentDojo runs/ directory; identity is derived "
                         "from each file's path relative to this root")
    ap.add_argument("--run", type=Path, default=None,
                    help="a single run file, relative to --runs-root")
    ap.add_argument("--glob", default="**/*.json",
                    help="glob under --runs-root when --run is not given")
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()
    if args.input is not None:
        if args.runs_root is not None and args.output_path is None and args.output is None:
            ap.error("with --runs-root, give the output as --output (a lone positional argument is "
                     "read as the runs directory)")
        if args.runs_root is not None:
            ap.error("give the runs directory once: as the first argument or as --runs-root")
        if not args.input.is_dir():
            ap.error(f"{args.input} is not a directory; a single run file is named with "
                     f"--runs-root <runs-dir> --run <path under it>, since its identity is its path there")
        args.runs_root = args.input
    if args.output_path is not None and args.output is not None:
        ap.error("give the output once: as the second argument or as --output")
    args.output = args.output or args.output_path
    if args.runs_root is None or args.output is None:
        ap.error("a runs directory and an output are required (positional, or --runs-root and --output)")
    if args.run is not None:
        files = [args.runs_root / args.run]
    else:
        files = keep_within_tree(sorted(args.runs_root.glob(args.glob)), args.runs_root)
    # An output that is one of the run files read here would overwrite it.
    if args.output.exists() and any(f.exists() and args.output.samefile(f) for f in files):
        ap.error(f"the output {args.output} is one of the run files read; it would be overwritten")

    containers, skipped = [], collections.Counter()
    for f in files:
        try:
            # The shared reader's bound (ruled 2026-10-29): a file over it
            # is skipped as unreadable, before it is read.
            if (size := f.stat().st_size) > _MAX_FILE_BYTES:
                raise ValueError(f"{size} bytes, more than {_MAX_FILE_BYTES}")
            run = input_json_loads(f.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, RecursionError, OSError) as e:
            skipped["unreadable"] += 1
            # "warning:" lines are losses to verify_run (ruled 2026-10-21).
            print(f"warning: skip (unreadable): {f}: {e}", file=sys.stderr)
            continue
        rel = f.relative_to(args.runs_root)
        c = build_container(run, rel)
        if _ts_refused and c is not None:
            print(f"warning: agentdojo: {rel}: evaluation_timestamp not convertible to the §3.5 form, "
                  f"read as no source timestamp", file=sys.stderr)
        if c is None:
            skipped["empty"] += 1
            print(f"warning: skip (no messages): {rel}", file=sys.stderr)
            continue
        containers.append(c)

    args.output.write_text(json.dumps(containers, indent=1), encoding="utf-8")
    n_attacked = sum(1 for c in containers if c["is_attacked"])
    n_loc = sum(1 for c in containers if c["injection_localized"])
    print(f"wrote {len(containers)} containers to {args.output}", file=sys.stderr)
    print(f"  attacked: {n_attacked}   injection localized: {n_loc}/{n_attacked}",
          file=sys.stderr)
    if skipped:
        print(f"  skipped: {dict(skipped)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main())
