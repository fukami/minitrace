# AGENTS.md — Working with the minitrace repo

AGENTS.md is the contributor-and-agent guide. It explains how to
*build*, *extend*, and *evolve* minitrace; the README explains how to
*use* it.

The realistic audience for this file is mostly LLMs helping a human
contributor, and the framing is deliberately agent-first. Human
contributors who prefer a human-primary entry should read
[CONTRIBUTING.md](CONTRIBUTING.md) first, then return here for the
governance and architecture material.

If you're an LLM that just opened this repo to help with a task, read
this whole file before touching anything. Then read the relevant spec.

> **Normative sections.** The §16 convention-absorption rule,
> "The temptation pattern", "Things to NOT do", and the closing note
> are normative. They encode discipline earned through ten adapter
> cycles; edits that soften them for tone consistency or readability
> are regressions. Treat them like spec text — propose changes, don't
> rewrite for flow.

---

## What minitrace is (60 seconds)

minitrace is a vendor-neutral interchange format for agent traces. It has four
normative specifications:

| Spec | Filename | What it defines |
|---|---|---|
| **Trace** | `spec/minitrace-spec-v0.3.0.md` | The on-disk shape of a trace (turns, tool calls, annotations, lineage, ...) |
| **DSL** | `spec/minitrace-adapter-dsl-spec-v0.1.0.md` | A YAML language for declaring adapters that convert a native session format into a trace |
| **Envelope** | `spec/minitrace-envelope-spec-v0.1.1.md` | A container format that bundles traces with attestation, witnesses, and provenance |
| **Scenario** | `spec/minitrace-scenario-spec-v0.1.0.md` | A framework- and model-agnostic scenario definition plus the binding record that pins a run to its `(model, harness, scenario)` triple |

These four are released together as the v0.3 bundle. They reference each
other; do not modify one without checking the cross-references in the
others. `VERSIONING.md` covers how specification versions and filenames
are managed. `spec/minitrace-spec-v0.2.0.md` is the previous trace
specification, kept byte-for-byte as the baseline the v0.3.0 trace spec
states its changes against.

There are also **adapters** under `adapters/<name>/`. Each adapter is a
DSL file plus an optional Python preprocessor that converts a specific
framework's native format into a trace.

---

## Architecture you must hold in your head

```
native session format
       │
       │  (1) preprocessor — Python, optional, normalises native quirks
       ▼
canonical event container — JSON, one record_path
       │
       │  (2) DSL runner — applies <adapter>.dsl.yaml
       ▼
trace document — JSON, conforming to trace spec
       │
       │  (3) cross-trace validator — checks lineage, hashes, integrity
       ▼
validated trace
       │
       │  (4) optional: envelope wrapper — manifest, attestation, witnesses
       ▼
shippable artifact
```

Three layers: **input** (preprocessor + DSL → trace), **trace** (the
document), **envelope** (the wrapper).

---

## The §16 convention-absorption rule (READ THIS)

This is the single most important governance discipline in the project.
The rule is defined here; `VERSIONING.md` refers to it by this name.
"§16" here is the rule's name, not a section reference to any current
specification. It governs the methodology recorded in this file and what
may enter any specification, except the adapter DSL's primitives, which the
DSL specification's own §16 (Versioning) admits (see `VERSIONING.md`, Path 1).

When an adapter discovers that the native format expresses something the
trace spec doesn't currently model, the adapter author does NOT silently
extend the spec. Instead:

1. **First deployment**: the adapter implements the convention locally and
   files a candidate amendment as an issue, with the evidence of need.
2. **Second deployment**: a different adapter independently encounters the
   same need. The candidate is promoted to a "second-deployment confirmed"
   amendment.
3. **Absorption**: the amendment is reviewed and absorbed. **Empirical
   amendments** (those that introduce a new on-disk field, change shape,
   or add a normative constraint to existing fields) absorb into the
   trace spec. **Methodology amendments** (those that change how adapter
   authors should approach a recurring situation but leave the on-disk
   trace shape unchanged) absorb into AGENTS.md under *Recurring
   patterns across adapter cycles*. The two-deployment trigger applies
   to both kinds.

This rule prevents premature fossilisation (one adapter's quirk becomes
spec) and premature laxity (every adapter invents its own field). If you
are an LLM proposing a spec change, check first whether the proposed
change has *two* independent adapter motivations. If it has one, your
proposal is a **first-deployment candidate**, not an amendment. A new
primitive of the adapter DSL is the exception (above).

Check the open issues for parked candidates before suggesting any spec
change.

Once a candidate is second-deployment-confirmed (or registered as a provisional
contract), the mechanics of landing it — strictly-additive revision against a
frozen predecessor, dual-validation, and when this is *not* a release — are in
`VERSIONING.md` under *Advancing a specification between releases*. §16 governs
*what* may enter a spec; VERSIONING governs *how* the revision is cut.

---

## How to build a new adapter

> For the recurring traps and disciplines the steps below don't spell out
> — deep-dive checks, corpus-generation gotchas, validation habits — see
> [`docs/adapter-authoring-guide.md`](docs/adapter-authoring-guide.md).
> It is a non-normative companion to this section.

The pattern that has held across the adapter cycles so far is:

### 1. Native-format deep dive (~2-3 hours)

Before writing one line of DSL or Python: read the native session files
yourself. Document:

- File layout (single JSON, JSONL, SQLite, ZIP, multiple files)
- Per-record shape; identify event kinds
- Tool-call representation
- Annotation/judge representation if present
- Timestamp source (per-record, file-level, derived?)
- Identity (session ID, trace ID, parent links)
- Native multi-trace structure (subagents, judges, witnesses)
- Anything that *looks* like one minitrace concept but isn't (false
  cognates are the #1 source of adapter bugs)

Output: a deep-dive write-up, kept with the adapter's pull request. The
native format it describes goes into the adapter's `format-reference/`
directory.

### 2. Scoping note (~1 hour)

Decide what's in scope for the first version. Mark deferred items
explicitly. This is the document you'll cite when an out-of-scope item
shows up later.

Output: a scoping note, kept with the adapter's pull request.

### 3. Preprocessor (Python, often required)

If the native format has cross-record references, hierarchical
structure, multi-file linkage, or anything else the DSL can't express
declaratively: write a preprocessor.

The preprocessor produces a canonical JSON container with a single
`record_path` (often `events`) that the DSL can iterate. It does *not*
emit traces directly.

Convention: `adapters/<name>/preprocess_<name>.py`.

### 4. DSL file

Declare the adapter in `adapters/<name>/<name>.dsl.yaml`. The DSL spec
covers the structure; copy a working adapter (claude-code, codex, goose)
and adapt rather than starting blank.

Required blocks: `adapter`, `input`, `session`, `emit`. Optional: `state`,
`constants`, `finalize`.

### 5. Smoke test on one session

Run `adapters/verify_run.py <adapter.dsl.yaml> <input> [<preprocess.py>|-] --operator-type <type>`
(a human operator also needs `--operator-aware`, `--operator-capture-mode full` and the three `--operator-capture-purpose-*` options; a delegated operator needs `--operator-capture-mode full`. The reference runner accepts only the capture mode `full` of the four in DSL §7.1; see `--help`)
against one session. It runs the preprocessor and `dsl_runner.py`, the
cross-trace checks in `cross_trace.py`, and verifies the result. Validate
the output against the schemas in `schemas/`. Round-trip if applicable.

If anything is unexpected: **surface it, don't fix it silently**. The
diff between expected and observed is research signal. Update the
deep-dive write-up.

### 6. Stratified sample / corpus run

Run against a stratified sample (or full corpus for small benchmarks).
Aim for 100% schema validation. Coverage report should be clean or each
flagged field documented.

### 7. Regression rung

Pin a content-hash baseline at `adapters/<name>/regression/<version>/`:
the captured input, a `manifest.json` freezing the expected content
hashes, and a `verify.py` that re-runs the adapter. Subsequent changes
that drift from this baseline must be justified, and the reason is
recorded in the manifest when the rung is re-frozen. The fleet sweep,
`adapters/run_fleet_sweep.py`, re-runs every rung.

### 8. §16 candidates

If your adapter needed to express something the spec doesn't model,
file the candidates as issues. Don't extend the spec yet.
**First-deployment is a candidate, not an amendment.** A new primitive of
the adapter DSL (an action, built-in or predicate) is the exception: the
DSL specification's own §16 (Versioning) admits it on one adapter
author's demonstration (see `VERSIONING.md`, Path 1).

---

## Recurring patterns across adapter cycles

Conventions absorbed under the §16 rule after multiple deployments across
distinct producer formats. These describe structural choices that have
proven sound across more than one adapter and are recommended for future
adapters that encounter the same situation.

### Schema-version-aware ingestion

When the producer tracks a schema version (an integer column, a
semver string, a migrations table, or any equivalent), dispatch on
that version inside a single preprocessor rather than forking into
separate adapter binaries per version. The dispatch key type is
incidental: integers, semver strings, and field-presence sentinels
on optional fields all work. A single preprocessor that branches on
version handles both additive and subtractive schema drift.

The pattern accommodates corpus durability across producer migrations:
a session captured under producer version N remains processable when
the binary advances to N+k. Demonstrated across four adapters covering
integer-keyed, semver-keyed, field-presence dispatch on optional fields,
AND tool-vocabulary renames across producer versions (the fourth
mechanism shape: producer tool names change between releases, and the
adapter dispatches the rename).

### Subagent cross-links: prefer structured over textual

When a producer exposes parent-child session relationships, look first
for structured cross-link fields. Top-level fields like
`parent_session_id` are cleanest. Child IDs nested in tool-result
metadata are second-best. Regex extraction from textual tool-response
payloads works but is fragile.

The lineage edge types `delegates-to` and `delegated-by` (already in
the spec) are reused across all such surfaces. Only the producer-side
extraction mechanism varies. If only a textual cross-link is
available, the regex extraction works but should be flagged in the
adapter's deep-dive write-up as a candidate for revisit if the producer
adds structured fields later.

### Preserve producer-side per-event structure verbatim

When the producer exposes structured data at any scope (per-tool,
per-event, per-message, per-thinking-block, per-span, per-anchor, or
producer-substrate), preserve it verbatim through the adapter rather
than compressing or summarising. Producer-emitted structure is signal,
not noise: the producer chose to emit it, and downstream consumers may
query it.

Producers progressively expose more structured surfaces across
versions. Verbatim pass-through accommodates whatever's exposed
without requiring adapter updates per producer revision. The
extension namespaces in §10 of the trace spec (`ext:<vendor>:<name>`,
`x-*`) are the canonical carriers for this kind of pass-through.

The pattern has now demonstrated across multiple adapters at nine
distinct scope dimensions: per-tool-call, per-tool-definition (on
`environment.tools_enabled[]`), per-tool-input, per-message,
per-thinking-block, per-event, per-span, per-anchor, and
producer-substrate (reference fields in audit-style producers). The
verbatim-preservation principle, not a single field or scope, is what
unifies them; future producers exposing structure at new scopes should
follow the same principle on whichever scope is appropriate.

### User-role frame-shift: dispatch on producer discriminator

When a producer emits a record carrying `message.role: user` that is
structurally identical to a real operator prompt but semantically a
harness-authored message (billing notification, token-limit message,
framework injection), the adapter MUST check for a producer-side
discriminator field BEFORE classifying the record as an operator turn.
Misclassification emits the harness-authored content as if the operator
had typed it.

The discriminator field varies by producer: a boolean meta-flag, a
visibility enum, a role-source field, or any equivalent. The shape is
incidental; what matters is that the producer's own answer to "did the
operator type this?" is honoured rather than inferred from content. When
no discriminator is exposed, the adapter SHOULD flag the gap in its
deep-dive write-up as a candidate for revisit if the producer adds the
field later. The frame-shifted content itself is preserved verbatim under
the extension namespaces; the discriminator-based dispatch separates
classification from preservation.

### Multi-trace adapters gate emit rules on the trace discriminator

When an adapter produces more than one trace from a single input (a
multi-trace adapter: an audit producer with auditor and target sessions,
a harness-wrap producer with outer and inner traces, or any future
double-trace shape), every emit rule MUST gate on the producer-side
discriminator that marks which trace the record belongs to, in addition
to its event-kind predicate.

The reason is structural: the DSL runner applies every shape view to the
*same* record stream — it iterates the records once and calls
`_build_trace` for each shape. A rule that matches only on event-kind
fires for both shapes and leaks records from one trace into the other.
The discriminator field is preprocessor-assigned and its name is
incidental; what matters is that each emit rule carries
`{field: <discriminator>, value: <trace-marker>}` in its `when` clause.

The pattern has demonstrated across two adapters with independent
discriminator shapes: an audit producer marking records with `_actor`,
and the inspect-swe harness wrap (`adapters/inspect-swe/`) marking them
with `_session`.

---

## Repository layout

```
spec/                              normative specifications (the four v0.3 specifications
                                   and the v0.2.0 baseline)
schemas/                           JSON schemas for trace, DSL, envelope, scenario,
                                   and their validators
adapters/                          adapter implementations and the reference tooling
  <name>/
    <name>.dsl.yaml                  DSL declaration
    preprocess_<name>.py             optional preprocessor
    format-reference/                the native format the adapter was written against
    regression/<version>/            regression rungs: input, manifest.json, verify.py
  dsl_runner.py                    DSL interpreter
  cross_trace.py                   lineage / integrity validator
  verify_run.py                    the full adapter pipeline for one input
  run_fleet_sweep.py               re-runs every rung and every test
  tests/                           the test suite the fleet sweep runs
  minitrace-verify/                trace verifier (Rust)
  jcs-oracle/                      RFC 8785 canonicalisation oracle (Rust)
examples/                          example traces, envelopes and scenarios
docs/                              adapter-authoring guide, validation evidence
```

---

## Working patterns we follow (and you should)

### Surface, decide, commit — don't bundle

When you find a problem, do **one** atomic thing per commit. Don't bundle a
bug fix with a refactor with a doc edit.

### Deep-dive before scoping; scoping before implementation

Implementations that skip the deep dive produce adapters that pass smoke
tests but have wrong structural assumptions. Implementations that skip
scoping produce out-of-scope creep mid-build.

### The diff between expected and observed is data

If the preprocessor's coverage report shows unexpected event kinds, or
the DSL output has unexplained fields, that's not a glitch to smooth
over. It's signal. Investigate, write it up, decide if it's a §16
candidate.

### One step at a time in conversational work

If you (LLM or human) are walking the contributor through a multi-step
process, do one step + the likely failure mode + a recovery strategy.
Then wait. Bundling steps loses the contributor when one fails.

### No completion pressure

The project has had completion pressure produce premature releases
before (v0.1 publication; some early adapter shipping). Pace adapter
work to the second-deployment trigger; pace spec work to evidence; pace
release to readiness, not to calendar.

---

## The temptation pattern

An LLM helping on minitrace will feel pressure to ship, to smooth over
surfaced anomalies, to propose feature flags, and to bundle changes.
These are not lazy errors. They are the same instinct expressed in
different ways: **resolve uncertainty by acting.**

This project resolves uncertainty by waiting for evidence. Spec changes
wait for second-deployment triggers (§16). Surfaced anomalies wait for
investigation. Adapter cycles take 2-5 days, not 2-5 hours. Releases
wait for the readiness review.

The two stances are incompatible. If you feel the pull to act — to
unblock, to clean up, to "just" patch through — that pull is the signal
to **stop**. Slow down. Surface what you've found. Let the maintainer
decide whether to act on it.

This is the same epistemic stance the project applies elsewhere: no
forced resolution; wait for evidence; surface what's surfacing. The
form changes by domain; the discipline doesn't.

The "Things to NOT do" list below is a list of instances of this
pattern. Treat the pattern as primary; the list is mnemonic.

---

## Things to NOT do

- **Don't add dependencies.** The adapter tooling runs on Python 3.13+
  (the oldest version on which the test suite passes) with the standard library and
  the pinned packages in `adapters/requirements.txt` (PyYAML, rfc8785,
  jsonschema, pytest).
  This is a hard constraint: an adapter or preprocessor needs nothing
  beyond them.
- **Don't extend the spec to match your adapter.** §16 governs spec
  evolution. First-deployment is a candidate, except for a primitive of
  the adapter DSL, which the DSL specification's own §16 admits.
- **Don't silently fix surfaced problems.** Surface them, decide
  together, then commit one fix.
- **Don't commit data with PII** (API keys, real emails, real local
  paths, customer information). Sanitise sample data before committing.
- **Don't rename specs or modules cosmetically.** External code may
  reference paths.
- **Don't add new top-level directories** without checking the layout
  table above.
- **Don't bypass the preprocessor → DSL split.** If you think a
  preprocessor "would be cleaner if it just wrote the trace
  directly", you've lost the architectural property that lets every
  adapter share `dsl_runner.py`.
- **Don't use `--no-verify` or skip pre-commit hooks** unless explicitly
  asked.
- **Don't propose a feature flag for a thing you're unsure about.**
  Either the change is right (commit) or it isn't (don't). Spec churn
  is more expensive than a missing feature.

---

## When you don't know what to do

1. Read the trace spec section that's nearest your question.
2. Check the open issues to see if your question is already a parked
   §16 candidate.
3. Look at how the closest existing adapter (claude-code is the most
   recent and best-documented) handled the analogous case.
4. Ask the maintainer rather than guess.

If you are an LLM and the contributor seems uncertain: ask which layer
(preprocessor / DSL / trace / envelope) the question is about. Layer
confusion is the most common source of stuck conversations.

---

## Glossary (single authoritative source)

- **Trace**: a JSON document conforming to the trace spec. One trace
  describes one session (agent-session, judge-session, witness-session).
- **Adapter**: a DSL file plus optional preprocessor that converts a
  native format into a trace.
- **Preprocessor**: Python program that normalises native data into a
  canonical event container the DSL can iterate.
- **DSL**: declarative YAML language for converting an event container
  into a trace.
- **Envelope**: a container format wrapping one or more traces with a
  manifest, attestation, and optional witnesses.
- **Witness**: an external observation included in an envelope (e.g., an
  evaluator's note attached to a trace).
- **Operator**: the entity running the agent; `operator.type` is `human`,
  `ai_overseer`, `scripted` or `delegated` (trace §5.1).
- **Producer**: the system or person that produced a trace.
- **Attestor**: the entity asserting (signing) an envelope.
- **Annotator / Judge**: an entity adding annotations to a trace; "judge"
  is shorthand for an annotator producing a judgement annotation.
- **Convention-absorption (§16)**: the spec-evolution discipline where
  adapter-discovered conventions are promoted to spec only after a
  second-deployment trigger. A primitive of the adapter DSL is admitted
  under the DSL specification's own §16 instead.
- **First-deployment candidate**: a §16 entry awaiting second-deployment
  confirmation.
- **Regression rung**: a content-hash baseline that pins an adapter's
  output at one harness version, used to detect unintended drift across
  spec or adapter changes.

---

## Project rhythm

The project moves in **adapter cycles**: each adapter cycle is a
deep-dive + scope + implementation + smoke-test + corpus run + §16-yield
arc that takes 2-5 days of focused work. Each cycle so far has produced
spec yield, as amendments or as candidates; that yield is why the spec
looks the way it does.

---

## Closing note for AI assistants

Re-read "The temptation pattern" if you find yourself wanting to bundle
changes, smooth over a surfaced anomaly, propose a feature flag, or
push toward shipping. The pull is the signal; the discipline is to
stop and surface. The diff between expected and observed is what this
project is about.
