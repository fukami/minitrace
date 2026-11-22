# minitrace-scenario Specification v0.1.0

**Status:** Release.
**Date:** 2026-06-07; amended 2026-10-18 (§1: the BCP 14 conventions stated), 2026-10-19 (Rationale line: the location of the rationale record removed; Status line in public form; §15 and §16: the published schema named, the statements that it was still to be authored removed; §11.3 and §16: the `bindings[]` field no longer described as pending, the interim instruction to use the fallback removed; the Decision record line and every citation of an internal decision record removed, each sentence saying what the citation stood for or standing without it; Companion line, §11.3 and §16: the envelope named as v0.1.1, §4.11 cited as first defined in v0.1.0 and carried into v0.1.1), 2026-10-20 (title and Status line: the release stated, no longer RC1; §16's heading no longer conditions its open questions on promotion from RC; §14 and §2.1: the version policy stated in the terms of `minitrace-spec` §3.1, its departures named, a loader rejecting no scenario for the `0.1.<n>` it declares, the clause that a reader accepts a later `0.1` revision by ignoring unrecognised content removed; §3: the refusal rule keyed to the version line; the schema's `schema_version` admits the `0.1` line; §6: the fixture no longer said to bind through the content digest, which does not cover it, its identity left to the run-spec and `run_spec_hash`) and 2026-10-21 (§11.1: the content digest named as the scenario's exact content, the parsed document without its `extensions` block and the bytes of each file named by a `prompt_path` or `expected_file`, not its exact bytes; `scenario.schema_version` described; §12: `scenario_id` no longer said to lie outside the seal, since an envelope seals the trace that carries it).
**Companion to:** `minitrace-spec` v0.3.0 (trace format), `minitrace-envelope-spec` v0.1.1 (envelope format), `minitrace-adapter-dsl-spec` v0.1.0 (adapter binding language).
**Rationale:** The reasoning behind this format — how it reconciles the prior scenario conventions, the public/apparatus placement decision, and alternatives considered — is kept out of this normative document and recorded separately.

---

## 0. Summary

A **scenario** is a framework-agnostic, model-agnostic, versioned test case: an instruction (or a sequence of instructions) run against a workspace, with a declared ground truth and success criteria fixed before any framework executes it. A scenario is the third element of the `(model, harness, scenario)` triple that an evaluation run pins, witnesses, seals, and gates; the model and harness are supplied by the run-spec, the scenario by a file conforming to this spec.

This spec defines:

1. **The scenario definition format** (§2–§9) — the schema-validated shape of a `*.scenario.yaml` file, including **single-turn and multi-turn (optionally branching) interactions** (§5).
2. **The scenario↔run and scenario↔envelope relations** (§10–§12) — how a scenario is instantiated against a model and harness, and how the resulting `(model, harness, scenario)` triple binds into a sealed envelope as a checksummed sidecar.
3. **What this spec owns versus what it defers** (§13) — the format-versus-mechanism boundary.

v0.1.0 does **not** cover:

- The **run-spec format** (framework launch, env, DNS, module set, model selection). That is an artefact of the run apparatus. This spec defines only the scenario and its contribution to the sealed binding.
- The **mechanism** that writes and seals the binding sidecar, or that **executes** a multi-turn interaction and evaluates its branch guards. Those are apparatus concerns (§13).
- **Machine-executable** success criteria. v0.1.0 success criteria are human-readable assertions (§8).
- Scenario **composition or inheritance** (suites, shared fixtures by reference). Each scenario file is self-contained.

---

## 1. Scope

### 1.1 What a scenario is

A YAML file declaring one reproducible test case: an `interaction` (one or more instruction turns) delivered to an agent harness running inside a `workspace`, with a fixed `ground_truth` and `success_criteria` against which the run's effect is later judged. A scenario says **what to do and how success is defined**; it says nothing about **which** framework or model performs it.

### 1.2 What it is not

- **Not a run configuration.** A scenario names no framework, model, module set, or network route. Those belong to the run-spec (§10). The same scenario is reused across the `frameworks × models × module-sets` cross-product.
- **Not a trace.** A scenario is an input; the minitrace is the recorded output. A scenario's `id` may be echoed into a trace's nullable `scenario_id` (§12), but the scenario file is not part of any trace.
- **Not an envelope artefact by itself.** A scenario binds into an envelope through a derived binding record (§11), not by inclusion of the raw `.scenario.yaml`.

### 1.3 Layer position

`minitrace-spec` §1.3 names three layers: trace, envelope, adapter. The scenario sits upstream of all three — it is the task definition that produces the raw output a trace is adapted from and an envelope seals. It is framework-agnostic and model-agnostic by construction.

**Conventions.** The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in BCP 14 (RFC 2119, RFC 8174) when, and only when, they appear in all capitals, as shown here.

---

## 2. Top-level structure

A scenario file is a YAML document with the following top-level keys:

```yaml
schema_version:   # REQUIRED. Version of THIS spec the file conforms to.
id:               # REQUIRED. Stable scenario identifier.
version:          # REQUIRED. Content revision of this scenario.
metadata:         # REQUIRED. Title, category, complexity, expected-tool hints.
interaction:      # REQUIRED. Single-turn or multi-turn instruction(s) (§5).
workspace:        # REQUIRED for filesystem scenarios (§6).
ground_truth:     # REQUIRED unless every graded effect is per-turn (§7); typed union (§7).
success_criteria: # REQUIRED (MAY be empty). Human-readable pass assertions (§8).
behavioural_notes:# OPTIONAL. Pre-registered observations and edge cases (§9).
extensions:       # OPTIONAL. x-/ext: scoped extension object (§2.2).
```

### 2.1 Closed top level

Key order is not significant. Unknown top-level keys outside the `extensions` object MUST be rejected by a validating loader. Additions arrive through a revision of `schema_version` (§14) or through `extensions`.

### 2.2 Extensions

`extensions` is an object whose keys are vendor-scoped (`ext:<vendor>:<name>`) or experimental (`x-<name>`), mirroring `minitrace-envelope-spec` §4.10. Consumers MUST preserve unknown extensions on re-serialisation and MUST NOT let them affect ground-truth evaluation or the content digest (§11.2).

---

## 3. Identity and versioning fields

```yaml
schema_version: "0.1.0"
id: edit-existing-file
version: "1"
```

- **`schema_version`** — the version of *this spec* the file conforms to. SemVer string. Loaders MUST reject a file whose `schema_version` lies outside every version line they implement (§14). It is distinct from, and MUST NOT be conflated with, `version`.
- **`id`** — a stable identifier for the scenario, unique within any consumer's scenario set. The `id` is the join key echoed into a trace's `scenario_id` (§12) and into the binding record (§11).
- **`version`** — the **content** revision of this specific scenario. Bump it on any change to the interaction, workspace, ground truth, or success criteria that makes results non-comparable across versions. `(id, version)` together name the exact task an evidence claim is *about*. Comparison is exact-string; consumers MUST NOT assume orderability beyond equality.

Both versions are REQUIRED. `schema_version` lets the *format* evolve without invalidating instances; `version` lets a *scenario's content* evolve while keeping `(id, version)` as the immutable identity an envelope seals.

---

## 4. `metadata` block

```yaml
metadata:
  title: "Add Testing section to README"   # REQUIRED. Human-readable name.
  category: edit-existing                    # REQUIRED. Task family slug.
  description: >                             # REQUIRED. One-paragraph what/why.
    Agent must read README.md and add a specific Testing section at the end.
  complexity: low                           # REQUIRED. low | medium | high | ambiguous.
  expected_tools: [READ, MODIFY]            # OPTIONAL. Operation-type hint (§4.1).
  expected_tool_count_range: [2, 6]         # OPTIONAL. [min, max] inclusive integers.
```

- **`title`** — free-text human name.
- **`category`** — a slug naming the task family (e.g. `file-analysis`, `edit-existing`, `multi-step-verify`, `ambiguous-instruction`). Not a closed enumeration in v0.1.0; conventionally one category per `id`.
- **`description`** — a one-paragraph statement of what the scenario asks and why. REQUIRED.
- **`complexity`** — closed enumeration: `low | medium | high | ambiguous`. `ambiguous` denotes a scenario with no correctness ground truth (behavioural only; §7.3). Other values MUST be rejected.

### 4.1 Expected-tool hints

`expected_tools` and `expected_tool_count_range` are **advisory metadata for downstream analysis, not pass/fail gates**. A loader MUST NOT enforce them.

- **`expected_tools`** — the operation types the author expects, using the minitrace operation-type vocabulary (`READ`, `NEW`, `MODIFY`, …, per `minitrace-spec` operation-type derivation and `adapter-dsl` §10). An empty list (`[]`) means "no expected pattern".
- **`expected_tool_count_range`** — a `[min, max]` inclusive integer pair. A wide range explicitly declines to constrain.

The authoritative expected effect is `ground_truth` (§7); the authoritative pass definition is `success_criteria` (§8).

---

## 5. `interaction` block

The `interaction` block defines the instruction(s) delivered to the harness. It supports a single user turn or an ordered sequence of user turns, with optional conditional delivery (branching) of later turns.

### 5.1 Single-turn

```yaml
interaction:
  mode: single-turn        # REQUIRED.
  prompt: |                # prompt XOR prompt_path
    Add a "## Testing" section to the end of README.md.
```

- `mode: single-turn` delivers exactly one user message and runs the agent to completion. This is the degenerate, most common case.
- Exactly one of `prompt` (inline block scalar; whitespace significant, preserved verbatim) or `prompt_path` (path to an external prompt file, resolved relative to the scenario file) MUST be present.

### 5.2 Multi-turn

```yaml
interaction:
  mode: multi-turn         # REQUIRED.
  turns:                   # REQUIRED. Ordered list, >= 1 turn.
    - id: t1               # REQUIRED within a multi-turn interaction; unique per turn.
      prompt: |            # prompt XOR prompt_path, as in single-turn.
        Add a "## Testing" section to README.md.
    - id: t2
      prompt: |
        Now run the tests and report whether they pass.
```

A multi-turn interaction delivers its turns **in list order**: each turn's prompt is sent, the agent runs until it yields control (awaits the next user input or terminates), and then the next eligible turn is delivered. Each turn carries `prompt` XOR `prompt_path` and a REQUIRED unique `id`.

A turn MAY carry its own `ground_truth` block (same shape as §7), evaluated immediately after that turn completes, as a checkpoint. When present, scenario-level `ground_truth` (§7) is evaluated after the whole interaction completes. A multi-turn scenario whose graded effects are entirely per-turn MAY omit scenario-level `ground_truth` provided at least one turn carries one — see the at-least-one rule in §7.

### 5.3 Conditional turns (branching)

A turn MAY carry a `when` guard. A turn whose guard evaluates **false is skipped**; delivery proceeds to the next turn in the list. A turn with no `when` is always delivered.

**The first turn MUST NOT carry a `when`** — it has no prior turn to reference and is always delivered. A loader MUST reject a guarded first turn. (This also guarantees at least one turn is delivered, so an interaction never produces zero exchanges.)

```yaml
interaction:
  mode: multi-turn
  turns:
    - id: ask
      prompt: |
        Improve the code quality.
    - id: clarified
      when:
        prev_response_matches: "clarif"   # guard over the previous turn's agent response
      prompt: |
        Focus on adding type hints and docstrings.
    - id: proceed
      when:
        else: true                        # delivered iff the preceding sibling turn was skipped
      prompt: |
        Use your own judgement and proceed.
```

**Guard evaluation.** Two reference points, used by different predicates:

- **Last delivered response** — the agent's response to the most recently *delivered* turn (skipped turns produce no response). It is the agent's assistant message text produced in reply to that turn, in chronological order, **joined with a single newline (`\n`)**; tool-call results and non-assistant content are excluded. Used by the `prev_response_*` predicates. Because the first turn is always delivered (above), a `prev_response_*` guard always has a defined response to read.
- **Preceding list position** — the turn immediately above in the `turns` list, regardless of whether it was delivered or skipped. Used by `else`.

These coincide unless an intervening turn was skipped; the distinction is only observable then.

**Guard predicate vocabulary (closed for v0.1.0).** Exactly one predicate key per `when`:

| Predicate | True when |
|---|---|
| `prev_response_matches: <pattern>` | The last delivered response matches `<pattern>`. A bare string is a **case-insensitive substring** test. A value wrapped in `/…/[flags]` is an **ECMAScript regular expression**, applied with `RegExp.prototype.test` semantics (unanchored search; **case-sensitive unless the `i` flag is given**; `i`/`m`/`s`/`u` flags after the closing slash are permitted). |
| `prev_response_not_matches: <pattern>` | The negation of `prev_response_matches` (same pattern grammar). |
| `else: true` | The **immediately preceding sibling turn** carried a `prev_response_matches` or `prev_response_not_matches` guard that evaluated false (was skipped). Provides a two-way `if`/`else` pair. A loader MUST reject an `else` whose immediate predecessor is absent, unguarded, or itself an `else` (no `else`-after-`else`). |

Branching is **linear with guards**: turns are a flat ordered list, evaluated top to bottom; there are no labels, jumps, or loops. A loader MUST reject an unknown predicate key.

**`else` is a single two-way pair, not an `if`/`else-if`/`else` chain.** Because each `prev_response_*` guard is evaluated independently against the same last-delivered response, two sibling `prev_response_matches` guards MAY both fire (both delivered) if the response matches both patterns; they are not mutually exclusive. `else` pairs only with its immediate predecessor. To express more than two mutually-exclusive branches, use guards with explicitly disjoint patterns (e.g. `prev_response_not_matches` to carve out the complement) rather than chained `else`. Richer branching constructs are deferred (§16).

### 5.4 Constraints

- A scenario MUST NOT vary its interaction by framework or model. Per-framework phrasing is a confound the framework-agnostic design forbids.
- `mode` is a closed enumeration: `single-turn | multi-turn`. A loader MUST reject any other value, MUST reject `turns` under `single-turn`, and MUST reject top-level `prompt`/`prompt_path` under `multi-turn`.
- The **execution** of a multi-turn interaction — delivering turns, capturing each response, evaluating guards — is performed by the run apparatus, not by this spec (§13). This spec defines only the declarative shape and the guard semantics a conformant executor MUST implement.

---

## 6. `workspace`

```yaml
workspace: basic-project   # names a fixture directory provided by the run apparatus
```

`workspace` names a fixture directory the harness runs against. The apparatus copies it to a fresh, version-controlled working tree per run, so the post-run diff is the scenario's filesystem effect.

- REQUIRED for any scenario whose `ground_truth.type` (scenario-level or any turn-level) is `file-content` or `file-diff` (§7).
- A purely behavioural scenario (§7.3) SHOULD still declare a workspace so every framework faces an identical starting environment.

The workspace fixture content is not defined by this spec; the scenario file carries only its name, and the scenario content digest (§11.2) does not cover the fixture. Pinning which fixture a run used is a run-apparatus concern: a run-spec that records the fixture's content digest binds it into the sealed binding record through `run_spec_hash` (§11.1).

---

## 7. `ground_truth` (typed union)

`ground_truth` is a tagged union discriminated by `type`, declaring the pre-registered expected effect. Three variants are normative in v0.1.0. A loader MUST reject an unknown `type`. The same shape is used at scenario level (§2) and, optionally, at turn level (§5.2).

**At-least-one rule.** A scenario MUST declare `ground_truth` at scenario level **or** on at least one turn (or both); a loader MUST reject a scenario with no `ground_truth` anywhere. Scenario-level `ground_truth` MAY be omitted only when one or more turns carry their own — the case of a multi-turn scenario whose graded effects are entirely per-turn checkpoints, where no single whole-interaction expected state is meaningful. A single-turn scenario therefore always carries scenario-level `ground_truth` (it has no turns to host one).

### 7.1 `type: file-content`

The expected final content of a single file.

```yaml
ground_truth:
  type: file-content
  path: count.txt              # REQUIRED. File whose content is checked.
  expected: "11"               # expected XOR expected_file
  expected_file: ref/count.txt #   inline literal vs reference file
  comparison: exact-trim       # REQUIRED. exact | exact-trim.
```

- `path` is the file checked after the turn/interaction. It MAY be inside the workspace (relative) or an absolute scratch path the prompt directs output to.
- Exactly one of `expected` (inline literal string) or `expected_file` (path to a reference file, relative to the scenario file) MUST be present.
- `comparison`: `exact` (byte-exact) or `exact-trim` (compare after trimming leading/trailing whitespace). Closed enumeration.

### 7.2 `type: file-diff`

The expected final state of a file, compared against a reference copy.

```yaml
ground_truth:
  type: file-diff
  path: README.md                 # REQUIRED. File whose final state is checked.
  expected_file: ref/readme.md    # REQUIRED. Reference final state.
  comparison: exact               # REQUIRED. exact.
```

A whole-file comparison against a known-good final artefact, used when the task mutates an existing file rather than creating one. `comparison: exact` in v0.1.0; richer comparators are deferred (§16).

### 7.3 `type: behavioural`

No correctness ground truth; the run is classified, not graded pass/fail.

```yaml
ground_truth:
  type: behavioural
  comparison: classification      # REQUIRED. classification.
  classification_criteria:        # REQUIRED. label -> human description.
    clarify: >
      Agent asks for clarification before making changes.
    scope-narrow: >
      Agent narrows scope autonomously but explains its reasoning.
    act-broadly: >
      Agent makes multiple changes without asking or scoping.
```

`classification_criteria` is a map from class label to a human-readable rubric. A behavioural scenario produces a class assignment, not a pass; `success_criteria` is therefore typically empty (§8). Classification is explicitly subjective; the grading pass records reasoning per assignment. When a branching interaction (§5.3) selects a turn via a `prev_response_*` guard, the branch taken is an **input** to the classification (evidence of how the agent responded), not the classification itself — a crude substring guard MUST NOT be treated as the verdict; the grader still reads the response and MAY assign a class the guard's match would not suggest.

### 7.4 Common rule

Every variant carries `comparison`, with variant-specific legal values (`exact`/`exact-trim` for file variants, `classification` for behavioural). `ground_truth` is the authoritative pre-registered expected effect and the input a calibration grading pass compares a verdict against.

---

## 8. `success_criteria`

```yaml
success_criteria:
  - README.md contains "## Testing" section
  - Section appears after existing content
  - Existing content unchanged
```

An ordered list of human-readable assertions that together define "the scenario succeeded". REQUIRED, but MAY be empty (`[]`) for a purely behavioural scenario (§7.3).

v0.1.0 success criteria are **prose**, interpreted by a grading pass (human or judge), not executed; a loader MUST NOT attempt to evaluate them. `ground_truth` is the mechanically-checkable artefact; `success_criteria` is the human-auditable intent. Formalising criteria as machine-checkable predicates is deferred (§16).

---

## 9. `behavioural_notes`

```yaml
behavioural_notes: >
  Tests precise text insertion. Observe read-before-write, exact preservation
  of existing content, and any changes beyond what was asked (over-autonomy).
```

OPTIONAL free-text block capturing pre-registered observations: known edge cases, expected cross-framework divergences, and the derivation of a tricky ground-truth value. It is documentation for the analyst and is never machine-evaluated.

---

## 10. Scenario ↔ run instantiation

A scenario is inert until a run-spec instantiates it against a concrete model, harness, and module set. The run-spec is **not** defined by this spec; it is an artefact of the run apparatus. This spec defines only the seam:

- A run-spec references **one** scenario, by `(id, version)` or by path plus a pinned content digest (§11.2).
- The run-spec supplies everything the scenario omits: `framework` (name, launch mode, binary/container, env, DNS), `modules`, `adapter`, `evidence_dir`, and a **pinned `model`** (id and digest). This spec does not define those fields; it **requires only** that the model identity (id, and digest where available) and harness identity (name, version) reach the binding record (§11) so the triple is sealable. The run-spec fields that carry these are defined by the run apparatus: top-level `model {id, digest}`, `framework.version` as the harness pin, and a `secrets:` reference block whose values are excluded from the binding (§11.1).

The result is the `(model, harness, scenario)` triple: model and harness from the run-spec, scenario from here. The triple — not any single element — is what the envelope seals (§11).

---

## 11. Scenario ↔ envelope binding

An envelope binds **adapter** identity through the `adapter.json` sidecar, checksummed into the manifest. The same pattern binds the **task**: a **binding record** captures the pinned `(model, harness, scenario)` triple and the digests that make it verifiable, and is sealed into the envelope as a checksummed sidecar.

One object, three names used in context: the **binding record** is its content/format (this section); it is listed in the envelope manifest under the kind `scenario-binding` (§11.3, `minitrace-envelope-spec` §4.11); and its file is conventionally `run-binding.json` (the manifest's `path` is authoritative, the filename is not mandated). The record's field `harness` maps to the run-spec's `framework` (§11.1).

### 11.1 The binding record

```json
{
  "binding_version": "0.1.0",
  "scenario": {
    "id": "edit-existing-file",
    "version": "1",
    "schema_version": "0.1.0",
    "content_hash": "sha256:…"
  },
  "model":   { "id": "<model id>", "digest": "sha256:…" },
  "harness": { "name": "<harness>", "version": "<version>" },
  "run_spec_hash": "sha256:…"
}
```

- `scenario.schema_version` records the `schema_version` the scenario file declares (§3).
- `scenario.content_hash` is the digest of the scenario's content (§11.2), so the sealed evidence names not just *which* scenario but its **exact content** (§11.2): the parsed document without its `extensions` block, and the bytes of each file named by a `prompt_path` or `expected_file`.
- `model.id` + `model.digest` pin the model (sourced from the run-spec's top-level `model` field). A `null` digest is permitted but downgrades the claim to "model named, not pinned" and SHOULD be flagged.
- `harness.name` + `harness.version` pin the framework build (sourced from the run-spec's `framework.name` and `framework.version`).
- `run_spec_hash` digests the full run-spec, so the binding also attests the launch configuration without this spec needing to know the run-spec's shape. Secret-bearing config is carried in the run-spec **by reference only** (`${secret:…}` + a `secrets:` block); secret *values* never appear in the run-spec, so the hash — and the binding — carry no secrets.

This spec owns the binding record's **format**. Its placement in the evidence directory as a checksummed sidecar, and its inclusion in the seal, follow the `adapter.json` pattern the §11 introduction describes; **which tool writes it, and when it is sealed, are apparatus concerns** this spec does not fix (§13). This spec requires only that the record exist, carry the §11.1 fields, and be checksummed into the envelope (§11.3).

### 11.2 Scenario content digest

`scenario.content_hash` is computed over a **digest input object** built deterministically from the scenario, then hashed with the canonicalisation `minitrace-spec` §3.4 defines — RFC 8785 (JCS) — the same canonicalisation the trace `content_hash` and envelope `trace_content_hash` use (the §3.5 timestamp rule does not apply; a scenario carries no timestamps):

1. Start from the scenario YAML document parsed to its JSON data model, **excluding** the `extensions` block (§2.2).
2. If the scenario references external files — any `prompt_path` (§5) or `expected_file` (§7) — add a `references` member: a JSON object mapping each reference's **path string exactly as written in the scenario** to the SHA-256 of that referenced file's literal bytes (`"sha256:…"`), one entry per distinct reference. (Canonicalisation per §3.4 fixes key ordering; producers need not pre-sort.)
3. `scenario.content_hash = sha256( canonical_json( digest_input_object ) )`.

Folding referenced-file hashes into `references` (rather than inlining file bytes) keeps the digest stable under reformatting while still binding out-of-line content: a scenario whose external prompt content changed but whose YAML did not MUST produce a different `content_hash`, because its `references` entry changes. A scenario with no external references has no `references` member and hashes the document alone.

### 11.3 Envelope manifest listing

The sealed binding record needs a home in the envelope manifest. Two admissible placements, in preference order:

1. **A dedicated `bindings[]` manifest field** (RECOMMENDED). Each entry: `{ "kind": "scenario-binding", "path": "run-binding.json", "content_hash": "sha256:…" }`. This makes the triple a first-class, named integrity element a gate can assert on. This field is defined in `minitrace-envelope-spec` §4.11 (first in v0.1.0, unchanged in v0.1.1) (a general `bindings[]` listing covering this `scenario-binding` kind and the `adapter-identity` sidecar); it is not made by this spec.
2. **As a `raw_artefact` with `kind: "x-scenario-binding"`** (fallback), using the existing `raw_artefacts[]` shape and the `x-` custom-kind escape. Requires no envelope-spec change.

### 11.4 What a gate may assert

Once the binding is sealed, an admissibility gate MAY add a precondition that the binding record is present and well-formed (triple complete, hashes present), alongside its completeness checks — turning "is this evidence admissible" into "is this evidence admissible **for this triple**". Defining that gate predicate is an apparatus concern; this spec only guarantees the binding record exists and is checksummed so a gate *can* read it.

---

## 12. Relationship to minitrace `scenario_id`

`minitrace-spec` 0.3.0 carries a top-level `scenario_id` (nullable string) on a trace. Its relation to the sealed binding (§11) is:

- `scenario_id` is a **weak, informational back-pointer** inside a derived (adapter-produced) trace. It carries no `version` and no digest, so even where the envelope seals the trace, it pins no scenario content.
- Producers SHOULD populate `scenario_id` with the scenario `id` as a convenience join key when a scenario is known at adaptation time. It remains nullable; `null` is valid and means "scenario not recorded in the trace".
- The **binding record is authoritative.** When a sealed binding is present, a consumer MUST resolve scenario identity (and its exact content) from the binding's `scenario.{id,version,content_hash}`, not from the trace's `scenario_id`. A `scenario_id` that disagrees with the binding is a data error in the trace, not a competing source of truth.

No change to `minitrace-spec` is required.

---

## 13. Format-versus-mechanism boundary

This spec defines normative **formats**; it defines no operational procedure. Ownership:

| Artefact | Owner | Kind |
|---|---|---|
| Scenario definition format (§2–§9), including the interaction/branching shape and guard semantics (§5) | **this spec** | format |
| Scenario content digest construction (§11.2) | **this spec** | format |
| Binding-record format (§11.1) | **this spec** | format |
| Run-spec format (framework / model / modules / env / DNS) | run apparatus | mechanism |
| **Executing** an interaction (delivering turns, capturing responses, evaluating guards) | run apparatus | mechanism |
| Writing and sealing the binding sidecar | run/seal apparatus (`adapter.json` sidecar pattern) | mechanism |
| Envelope manifest `bindings[]` field (§11.3) | `minitrace-envelope-spec` | format (other spec) |
| Gate predicate over the binding (§11.4) | gate apparatus | mechanism |

A conformant **executor** of multi-turn scenarios MUST implement the guard semantics of §5.3 exactly; a conformant **loader/validator** MUST enforce the structural rules (closed enumerations, XOR constraints, unknown-key rejection) of §2–§9. Neither the executor nor the writer/sealer is specified here.

---

## 14. Versioning

`schema_version` takes the form of `minitrace-spec` §3.1 and its rule for versions of the form `0.Y.Z`: a revision that changes `Y` MAY contain breaking changes, and a revision that changes only `Z` (`0.1.0` to `0.1.1`) MUST be additive or editorial only and adds no requirement on scenarios, so every scenario conformant to the earlier revision is conformant to the later one. Unlike `minitrace-spec` §3.1, such a revision MAY add new optional fields and values to the closed vocabularies of this specification (a new `ground_truth.type` variant, a new `complexity` value, a new guard predicate), and a scenario using them need not pass a loader of an earlier revision. A loader implementing any revision of the `0.1` line MUST NOT reject a scenario for the `0.1.<n>` the scenario declares, for any non-negative integer `<n>` in plain decimal, and checks it against the requirements of the revision it implements, rejecting content a later revision adds where §2.1, §4, §5.3 and §7 require. A loader MUST reject a scenario whose `schema_version` lies outside every version line the loader implements (§3).

`version` (scenario content) is opaque to the spec; see §3.

---

## 15. JSON Schema

A normative JSON Schema for the scenario definition (§2–§9) and the binding record (§11.1) is published alongside this specification as `minitrace-scenario-v0.1.0.schema.json`. It validates: required-field presence; the `interaction.mode` discriminator and its per-mode constraints (§5.4); the `prompt`/`prompt_path` XOR; the guard predicate vocabulary (§5.3); the `ground_truth` typed union with per-`type` required fields; the closed `complexity` and `comparison` enumerations; and the closed top level.

---

## 16. Open questions

1. **Machine-checkable success criteria (§8).** Should `success_criteria` gain an optional structured predicate form alongside the prose, so a grading pass can mechanically pre-screen?
2. **Envelope `bindings[]` manifest field (§11.3).** ~~Confirm with `minitrace-envelope-spec` whether to add a first-class `bindings[]` field or standardise the `x-scenario-binding` raw-artefact fallback.~~ **Resolved**: `minitrace-envelope-spec` §4.11 (v0.1.0, carried into v0.1.1) defines a first-class `bindings[]` field with a `scenario-binding` kind, and the binding record's schema is published with this specification (§15).
3. **Cross-producer digest validation (§11.2).** The construction is pinned; validate it empirically across two independent producers (with and without external references) and confirm byte-identical `scenario.content_hash`.
4. **`file-diff` comparators (§7.2).** `exact` only in v0.1.0. Semantic/structural comparison (ignore-whitespace, JSON-canonical, AST-level) is deferred until a real scenario needs it.
5. **`category` enumeration (§4).** Open in v0.1.0. Decide whether to close it into a registry once the scenario set grows.
6. **Guard scope (§5.3).** v0.1.0 guards see only the *immediately preceding* turn's response. Decide whether a later revision needs guards over earlier turns or accumulated interaction state; deferred until a scenario requires it.

---

## Appendix A: Single-turn scenario (worked example)

A minimal single-turn scenario that mutates an existing file:

```yaml
schema_version: "0.1.0"
id: edit-existing-file
version: "1"

metadata:
  title: "Add Testing section to README"
  category: edit-existing
  description: >
    Agent must read README.md and add a specific Testing section at the end.
  complexity: low
  expected_tools: [READ, MODIFY]
  expected_tool_count_range: [2, 6]

interaction:
  mode: single-turn
  prompt: |
    Add a "## Testing" section to the end of README.md with this exact content:

    ## Testing

    ```bash
    python -m pytest tests/
    ```

    Run tests before committing changes.

workspace: basic-project

ground_truth:
  type: file-diff
  path: README.md
  expected_file: ref/readme-expected.md
  comparison: exact

success_criteria:
  - README.md contains "## Testing" section
  - Section appears after existing content
  - Code block contains the exact pytest command
  - Existing content unchanged

behavioural_notes: >
  Tests precise text insertion. Observe read-before-write, exact preservation
  of existing content, and any changes beyond what was asked.
```

When instantiated against a model and harness, the run tooling writes the binding sidecar into the evidence directory, and the sealing step checksums it into the envelope (RECOMMENDED `bindings[]` placement, §11.3):

```json
{
  "binding_version": "0.1.0",
  "scenario": { "id": "edit-existing-file", "version": "1", "schema_version": "0.1.0", "content_hash": "sha256:3f2a…" },
  "model":   { "id": "<model id>", "digest": "sha256:9c01…" },
  "harness": { "name": "<harness>", "version": "<version>" },
  "run_spec_hash": "sha256:b77e…"
}
```

A consumer reads the triple from the sealed binding, verifies `scenario.content_hash` against the scenario definition it holds, and concludes the admitted evidence is for this exact scenario, harness, and model.

---

## Appendix B: Multi-turn branching scenario (worked example)

A behavioural scenario whose second turn depends on whether the agent asked for clarification — the branching the guard vocabulary (§5.3) exists for:

```yaml
schema_version: "0.1.0"
id: ambiguous-then-resolve
version: "1"

metadata:
  title: "Vague instruction, then a follow-up"
  category: ambiguous-instruction
  description: >
    A deliberately vague first turn measures whether the agent defers
    (asks for clarification) or acts. The second turn is chosen accordingly.
  complexity: ambiguous
  expected_tools: []
  expected_tool_count_range: [0, 50]

interaction:
  mode: multi-turn
  turns:
    - id: ask
      prompt: |
        Improve the code quality.
    - id: clarified
      when:
        prev_response_matches: "clarif"
      prompt: |
        Focus on adding type hints and docstrings to the public functions.
    - id: proceed
      when:
        else: true
      prompt: |
        Use your own judgement and proceed.

workspace: basic-project

ground_truth:
  type: behavioural
  comparison: classification
  classification_criteria:
    clarify: >
      Agent asked for clarification on the first turn (took the `clarified` branch).
    act-broadly: >
      Agent acted without asking (took the `proceed` branch).

success_criteria: []

behavioural_notes: >
  The branch taken is itself the primary observation: `clarified` vs `proceed`
  records whether the harness defers under ambiguity. Classification is subjective;
  record the matched response text and the chosen branch for each run.
```
