# minitrace v0.3.0 — Validation Evidence

> **Non-normative companion** to `spec/minitrace-spec-v0.3.0.md`. This document records
> the empirical grounding of the trace and adapter-DSL specifications: which spec elements
> have been exercised, by which independently-authored adapters, with which outcome and
> evidence. It carries no normative force. The convention-absorption governance that decides
> when an exercised convention becomes spec text is the rule named §16 in
> [`../AGENTS.md`](../AGENTS.md).

A spec element is marked **Exercised** once at least one adapter uses it. The parenthetical
count records how many *independent* adapters have exercised it; two or more independent
adapters against unrelated corpora clears the two-deployment trigger of that rule.

## Exercised elements

| Element | Adapters | Outcome | Evidence |
|---|---|---|---|
| §3.5 timestamp precision | 5 corpora | Exercised (5) | µs-precision UTC across all adapters. |
| §3.5 timestamp-fallback discipline | atbench-claw | Exercised | Publication date for `PolicyTrigger.timestamp` + lineage `authored_at`; Codex uses real per-event timestamps directly — both halves grounded. |
| §4.8 tool-call argument shape | atbench-codex | Exercised | `json_parse` on all 1,195 Codex tool calls, 0 parse failures; Claw args already objects. |
| §4.8 tool_call.id preservation | atbench-codex, otel-halo | Exercised (2) | Codex `function_call.call_id` (1,195); HALO OpenInference `tool_call.id` (2,977, 100% match-back). |
| §5.3 capture_purpose conditional-required | 5 corpora | Exercised (5) | All adapters use non-human `operator.type`, omit `capture_purpose`; 4-paradigm cross-confirmation. |
| §6.7 multi-axis taxonomy semantics | atbench-claw, atbench-codex | Exercised (2) | 3 orthogonal axes (`risk_source`/`failure_mode`/`harm`) + observation = 4 annotations/judge; conditional 5th (`defense_type`), 6th (Codex `injected_tool_descriptions`). |
| §6.7 taxonomy domain qualifier | atbench-claw, atbench-codex | Exercised (2) | `atbench/claw/…`, `atbench/codex/…` namespaces; 10,000+ verified instances. |
| §9.1 schema-tightening on `harm_category` | atbench-claw, atbench-codex | Exercised (2) | Rejects non-AI-Act-aligned `atbench/<variant>/harm/…`, forcing `claim_type: classification` + `taxonomy[]`. |
| §9.5 defense-mechanism classification | atbench-claw, atbench-codex | Exercised (2) | Claw 3 values 204/204; Codex 5 values 221/221 incl. 37 emissions on unsafe trajectories. |
| DSL §13 cross-trace reference rules | atbench-claw, atbench-codex, petri, inspect-swe-cc | Exercised (4) | 2,000 ATBench traces + petri 10 audit-pairs + inspect-swe double-traces; 3 pattern-deployments (label-pair, audit-pair, harness-wrap); 0 round-trip/schema failures. |
| DSL §14 multi-trace declaration | atbench-claw, atbench-codex, petri, inspect-swe-cc | Exercised (4) | Plural `sessions:` across 3 shapes; `emit_lineage`+`emit_annotation` on all four, `emit_policy_trigger` on atbench-claw. |
| DSL §14.7 producer-side discipline (no unresolved `${…}`) | 5 corpora | Exercised (4) | Initial Claw shipped 1,500 unresolved-token strings; runner bug fixed (`_materialise` list recursion); all adapters now 0 token leaks across 4,617-trace cycle. |
| DSL §8.4 `container_index` context variable | atbench-claw | Exercised | Synthesises unique trace ids; Codex uses `container.id` directly (alternative, also valid). |
| DSL §4.1.1 deep-path `record_path` | atbench-claw, atbench-codex | Exercised (2) | Claw `trajectory.events`; Codex `codex_rollout`; both via deep_get. |
| DSL §4.1.2 `record_path: "$self"` sentinel | mast-full, mast-human, otel-halo | Exercised (3) | Iterate-once-per-container singleton for atomic-record corpora. |
| DSL §8.1 `container_field` predicate | atbench-claw, atbench-codex | Exercised (2) | Conditional `defense_type` (204/204, 221/221) + `injected_tool_descriptions` (Codex 34/34). |
| DSL §8.1 `block_field` predicate | mast-full, otel-halo | Exercised (2) | mast-full ON-flag on `dict_items` (17,388 evals, 3,064 ON); otel-halo tool_call.id presence. |
| DSL §9.1 builtin functions | all 5 DSL files | Exercised (5) | All 16 builtins; `dict_items`/`prefix_token` first in MAST, `json_parse` in Codex. |
| §6.7.5 taxonomy version qualifier | mast-human, claude-code | Exercised (2) | mast `v0`/`v1`/`v2` annotation rounds; claude-code per-event `2.1.92`–`2.1.132`, intra-session drift (167 `2.1.97` + 400 `2.1.100` in one trace). |
| §7.1 lineage edge without `target_content_hash` | claude-code | Exercised | Subagent `delegated-by` omits hash to avoid a reference cycle; parent `delegates-to` carries integrity (§3.4 allows omission for non-integrity edges). |
| §7.1 + §13 multi-trace fan-out via N+1 sibling containers | claude-code | Exercised | Pre-processor flattens parent + N subagents into N+1 siblings; 111 traces/76 sessions, 0 cycle errors. |
| §10.5 inspect-AI event taxonomy | petri, inspect-swe-cc | Exercised (2) | Petri 9 declared event types; inspect-swe 5 named + dropped infra events; `ext:inspect_ai:event:<type>` pass-through. |
| §7.4 harness-wrap two-variant | inspect-swe ×3 (cc/opencode/gemini) | Exercised (3) | Double-trace (cc: 4+4, 4/4 cross-trace round-trip) + outer-only ×2 (empty lineage, `flags.wraps_inner: false`). |
| §7.1 `wraps`/`wrapped-by` lineage kinds | inspect-swe ×3 | Exercised (3) | Positive (cc): `wraps`+hash / `wrapped-by` no hash, 4/4 round-trip. Negative-form (opencode/gemini): empty `lineage[]` + explicit `flags.wraps_inner: false`. |
| §7.5 outer-trace `flags.wraps_inner` | inspect-swe ×3 | Exercised (3) | cc `true`; opencode/gemini `false` — explicit boolean across all three, no reliance on flag-absence. |
| §10.6 bridge-intercepted model events | inspect-swe ×3 | Exercised (3) | `outer_model_event_turn` byte-stable across all three (modulo ann-id prefix); Bridge ModelEvent count = outer assistant-turn count; `ext:bridge:*` cross-producer namespace. |

## Single-deployment evidence

These elements have been exercised by exactly one adapter against one corpus. They are not yet
at the two-deployment threshold; the working notes track them as candidates (see below).

| Element | Adapter | Corpus | Evidence |
|---|---|---|---|
| Redaction-by-omission (`method: implicit_omission`) | otel-halo | HALO 57 traces | 57/57 traces emit trace-level Redaction, `covered_field: llm.output_messages`. |
| Multi-inference-per-turn collapse | otel-halo | HALO 57 traces | 600 LLM spans collapsed into 4,489 conversation turns. |
| `tool_use.caller.type` as `invocation_source` source | claude-code | claude-code 2.1.131+ | Single observed value (`direct`); awaits evidence the field is meaningfully variable. |
| Permission-mode change → `segment_marker` | claude-code | claude-code 2.1.116+ | 58–98 `mode_shift` markers per session; behavioural shift between 2.1.104 and 2.1.116 confirms intentional emission. |
| Hooks split: `policy_trigger` vs `annotation` | claude-code | claude-code full corpus | Both surfaces from one producer (108 policy_triggers, 399 hook annotations in one trace). |
| Custom inspect-ai Agent producers | petri, inspect-swe-cc | petri 10 audit-pair + inspect-swe 4-sample | inspect-swe is itself a corpus-exercised custom inspect-ai Agent (`@agent`-decorated factory conforming to the Agent Protocol). |
| `ext:mcp:*` MCP-protocol observations | minimcp | minimcp 5 bridge-session traces | Server/peer vantage: `ext:mcp:capability_negotiation` + `ext:mcp:advertised_vs_serviced`; claude-code advertises `roots`+`elicitation`, services only elicitation. |

The full candidate text, the conditions each single-deployment element is waiting on, and the
anticipated-but-unexercised entries are tracked in development notes that
do not ship with the release, not in this evidence record.
