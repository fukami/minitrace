# Adapter-authoring guide

> **Status: non-normative.** This guide collects build-discipline lessons
> earned across adapter cycles. The normative homes are the four specs
> (trace, DSL, envelope, scenario) and AGENTS.md (the §16 convention-absorption rule
> and "Recurring patterns across adapter cycles"). Everything here is a
> recommendation from experience, not a requirement.

AGENTS.md tells you the architecture and the eight phases of building an
adapter ("How to build a new adapter"). This guide is its companion: the
recurring traps and disciplines that the eight phases don't spell out but
that have cost real time across cycles. Read AGENTS.md first; reach for
this when you start a new adapter.

## How to read this

Each item is a directive plus its evidence: the cycle it first surfaced
in, and `2-dep` when two independent adapters hit it (the same
second-deployment bar §16 uses for spec amendments). Items are grouped by
the build phase where they bite, not by when they were discovered.

**Provenance and numbering.** These notes were accumulated as per-cycle
process notes during adapter development (cycles 7–15). The source
corpus was numbered incrementally per cycle, but the sequence is
imperfect: the earliest entries (cycles 7–8) were counted but never
individually enumerated, and several later entries are second deployments
of earlier ones rather than new observations. This guide therefore
organises by build phase rather than preserving the original numbering.

---

## 1. Native-format deep dive

- **Don't trust docstrings or a prior version's adapter comments as
  schema-authoritative.** Verify every claimed key set and tool surface
  against the actual corpus. *(cycles 7→10, 2-dep — a v0.2.0 vibe
  docstring claimed 10 top-level meta keys and 8 tools; the actual corpus
  had 14 and 10.)*
- **Enumerate the distinct values of every discriminator field up
  front.** A 1-in-95 `info` message type was nearly missed in the cycle-7
  deep dive because sessions were inspected without first listing the
  distinct `messages[].type` values. *(cycle 7)*
- **Check the installed binary version against the corpus version before
  scoping.** Cycle 9 nearly missed a 12-minor-version drift (corpus
  1.2.20 vs installed 1.14.40); a five-minute probe of the live install
  surfaces it. *(cycle 9)*
- **Confirm lazy-migration safety before committing to a multi-version
  adapter:** empirically check that newer binaries don't auto-mutate
  older-version stores when read read-only. *(cycle 9)*

## 2. Scoping

- **Assume schema evolution is bidirectional.** Producer version bumps
  add tables *and* remove fields; design version-conditional gating that
  accommodates both, in one preprocessor, not separate binaries.
  *(cycle 9)*
- **Watch for silent capability decay via model-alias rewrites.** A
  producer's `<alias> → model` mapping can change across versions with no
  user action — vibe's `mistral-vibe-cli-latest` moved devstral-2 to
  mistral-medium-3.5 with a 4× price change between corpus and probe.
  Treat session-captured stats as authoritative over config files.
  *(cycle 10)*

## 3. Preprocessor and DSL authoring

- **`foreach_block` over a list of primitives with no per-block field to
  test: default `when: { has_any: true }`.** An empty `when:` is not a
  predicate form DSL §8.1 defines, so it is a structural failure and the
  runner rejects the adapter at load (DSL §15). `{ has_any: true }` always
  matches, and is the form for an unconditional sub-rule. *(cycle 8)*
- **Skip empty sessions in the preprocessor.** The trace schema requires
  `turns` to have at least one entry; a session with zero records (an
  aborted or test run) produces an invalid trace. Log the skip count for
  visibility. *(cycle 8)*
- **For sliceable cross-session graphs, split full-detected from in-batch
  lineage.** Keep the full detected relation list for diagnostics, but
  emit lineage edges only for members present in the current batch.
  Otherwise a user who processes a subset gets edges whose target is not
  in the batch, which the runner omits and reports as losses (DSL §13.2,
  R4). *(cycle 8)*
- **Coalesce within-message streamed thinking blocks.** This is
  load-bearing for cross-provider comparability and trace size, not a
  cleanliness preference: emitting thousands of one-token blocks
  individually inflates a trace by three to four orders of magnitude.
  *(cycle 8)*
- **Multi-trace gating** — the requirement that every emit rule in a
  multi-trace adapter gate on the session/actor discriminator has
  graduated to a normative recurring pattern. See AGENTS.md → "Recurring
  patterns across adapter cycles" → *Multi-trace adapters gate emit rules
  on the trace discriminator*. *(cycles 12→13, 2-dep)*

## 4. Corpus generation

- **Verify producer write-timing (eager at session-start vs lazy at
  session-end) before claiming completeness.** A producer that writes
  lazily (vibe writes at end) leaves no on-disk trace for killed
  sessions, so the on-disk session count understates what actually ran.
  *(cycle 10)*
- **Budget for probe-failure hangs.** Sequential probes against some
  provider APIs hang after an initial silent failure; plan for it when
  generating fresh corpus rather than assuming a clean batch. *(cycle 10)*
- **Bridged cross-provider routing needs an explicit param-filter.** When
  a wrapped agent speaks one provider's API at the bridge ingress and the
  bridge routes to a different provider at the wire layer, parameter
  combinations valid at the source can be invalid at the destination —
  gemini-cli sends both `temperature` and `top_p` (both valid at Google),
  and the Anthropic API 400s on the pair. Inspect outgoing-request params
  against the *destination* provider's rules. Symptom: a destination-
  provider 400 mid-run. This is a corpus-generation concern, not adapter
  behaviour — the DSL and preprocessor see only the post-filter events.
  *(cycle 15)*

## 5. Validation and regression

- **Use the producer's own blessed export as a Phase 2/3 validation
  oracle, not as primary input.** Diff adapter output against it to verify
  fidelity. It works even on partial or killed runs — streaming output
  still reveals the full uniform schema. *(cycles 9→10, 2-dep)*
- **Verify that regeneration is byte-stable with `diff -rq` over two parallel run
  trees,** not a hash-list comparison. `diff -rq` catches
  canonicalisation-equivalent-but-byte-different drift that a hash list
  can miss. *(cycle 13)*
- **Run a cross-adapter timestamp-precision sweep at cycle close.**
  Cycle 11 hit 52 first-run errors from millisecond-precision ISO-8601
  timestamps against the microsecond-precision regex; the fix
  right-pads to six digits. The class of issue is likely silent in older
  adapters that targeted a looser constraint. *(cycle 11)*

## 6. Claim verification — counts, deep dives, and handovers

- **Re-verify counts with a fresh grep against the corpus, never from an
  earlier bash-output paste-through.** A stale "8 distinct functions"
  count carried through from earlier session output; a fresh grep
  resolved it to 9 observed plus 4 available-but-unfired. *(cycle 12)*
- **Verify handover and next-session assertions against the referenced
  source at writing time,** not from session memory. A cycle-close
  handover named reusability claims (file paths, prior cycles, adapter
  formats) that turned out to be three substitutions plus an
  architectural barrier; they were caught only by a pre-implementation
  read at the next cycle's start. Naming a specific source is a promise to
  have looked at it. *(cycle 14)*

## 7. Naming and record shape

- **Prefer the upstream factory name over the display name for stable
  identifiers** such as `wrapped_agent` in `framework_config`. The factory
  name (`gemini_cli`) survives upstream display-string churn
  (`Gemini CLI`); display strings are presentation-layer and shift between
  versions without notice. *(cycle 15)*
- **Emit a non-turn record to preserve session-level metrics for
  empty-content assistant messages** (`assistant_empty_with_metrics`)
  when a producer bills at step granularity rather than message
  granularity. Otherwise the session-level metric is lost with the empty
  message. *(cycle 9)*
