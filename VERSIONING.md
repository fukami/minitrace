# Versioning

How the minitrace specifications are versioned, named, and referenced. This
document is normative for the suite's filenames and cross-references.

## Independent SemVer per specification

minitrace is a suite of specifications that ship together but version
independently:

| Specification | Identifier | Current version |
|---|---|---|
| Trace | `minitrace-spec` | v0.3.0 |
| Adapter DSL | `minitrace-adapter-dsl-spec` | v0.1.0 |
| Envelope | `minitrace-envelope-spec` | v0.1.1 |
| Scenario | `minitrace-scenario-spec` | v0.1.0 |

Each takes the form and precedence of Semantic Versioning 2.0.0 and follows the
version policy of `minitrace-spec` §3.1, which the envelope (§4.2) and adapter
DSL (§16) specifications adopt, and the scenario specification takes with the
departures its §14 names. Before `1.0.0`, a revision that
changes `Y` of a version `0.Y.Z` (`0.3.0` to `0.4.0`) may contain breaking
changes and requires explicit migration, and a revision that changes only `Z`
(`0.1.0` to `0.1.1`) is backwards-compatible, additive or editorial only. From
`1.0.0` on, Semantic Versioning applies as published. A specification's own
version-history section is authoritative for its line.

The suite ships as a coherent bundle, but the bundle is not a single version
number. Each specification carries its own version, and a coordinated git tag
marks a release across the set; the release gate is that every specification is
at the same *stage* (all release candidates, then all released), not that they
share a number.

## What a release candidate may change

A release candidate is an editorial and publication motion, not a drafting one.
Candidates of the same version may differ in presentation and in informative
content: frontmatter and document metadata, non-normative sections, examples,
validation evidence, and material stripped for publication. They may not differ
normatively. Adding, removing, or re-levelling a requirement, changing an
obligation's strength or its bearer, or altering a field, type, or baseline
value is a version change, not a candidate change.

The test is a consumer test: if an implementation conformant to `rcN` is not
conformant to `rcN+1`, the two are not candidates of one version. Under that
test a `MUST` demoted to `SHOULD` disqualifies a candidate even though it
loosens rather than tightens the obligation, because the pair no longer
describes one contract under one version string.

Normative change leaves the candidate and takes a version bump instead. Where
the change is additive, that is Path 1 below: it increments `Z` before `1.0.0` (the
minor from `1.0.0` on), ships its own schema cut beside it, states what it changes against
its predecessor, and leaves the predecessor frozen. Where it is not additive, it
is a breaking revision (a change of `Y` before `1.0.0`, of the major from
`1.0.0` on) taken
through a release cut (Path 2). Either destination gives the new contract a new
number and leaves the predecessor frozen; what this rule forbids is a second
candidate under the old number. The envelope line is the worked instance of the
additive case, except that entries removed as a whole carry no file fields,
which v0.1.0 required (envelope §12): when new normative content arrived it did not become an
`envelope-spec` v0.1.0 rc2, it became `v0.1.1-draft-1` with
`schemas/minitrace-envelope-v0.1.1.schema.json` cut beside it, `v0.1.0` rc1
untouched as the corpus-production reference.

**A candidate that has already diverged** is not retroactively legitimised by
this rule, and is not edited into compliance either, since editing a published
candidate is the act the rule forbids. Its non-normative content is eligible
for the next candidate of the same version; its normative deltas are re-decided
one at a time, against the next candidate while the version is unpublished and
against the next revision's draft once it is published, each carried or dropped on
its own argument.

**When the rule binds.** The rule protects implementers who build against a
candidate the project has published, so it binds from a version's first
published candidate. Before that, a later candidate of an unreleased version
may change normatively under a narrower test: every artefact conformant to an
earlier candidate of the same version remains conformant to the later one. A
loosening passes that test by construction. A tightening passes only when it is
checked against the artefacts known to exist, and a tightening that fails the
check takes a version bump instead. A candidate shared with a party building for
the project, rather than published, does not start the rule; the artefacts that
party produces against it join the set the narrower test is checked against.

## Filenames carry the version, not the release-candidate state

A published specification's filename carries its **version only**:

    minitrace-spec-v0.3.0.md
    minitrace-envelope-spec-v0.1.0.md

The filename does **not** encode release-candidate state. A specification keeps
the same filename from its first release candidate through its final release —
the bytes change, the name does not. This is deliberate: filename churn across
release candidates is the primary generator of dangling cross-references
between sibling specifications.

Release-candidate and draft state is tracked **outside the filename**:

- the document's **`Status:` header** (`DRAFT`, `Release candidate N.`, or
  `Release.` under a numbered version);
- the **`Date:` line**, which dates the text and its amendments; and
- **git tags** (`v0.3.0-rc1`, `v0.3.0-rc2`, … `v0.3.0`).

Working drafts in development may carry a `-draft-N` or `-rcN` suffix while a
revision is in flight; those are working-tree artefacts. The published
specification is always the bare-versioned file.

## Cross-references use the bare name, version, and section

A reference from one specification (or decision record) to another names the
**specification, its version, and the section** — never a working-draft
filename:

    minitrace-envelope-spec v0.1.0 §4.11            ← correct
    minitrace-envelope-spec-v0.1.0-draft-5 §4.11    ← wrong: cites a draft that gets superseded

Section numbers are **stable within a version line**: additive revisions append
new sections rather than renumbering existing ones, so a reference to `§4.11`
resolves to the same content across that version's release candidates and its
final release. Stable section numbers plus bare-versioned names are what let a
cross-reference survive the RC→release transition without edits.

## Advancing a specification between releases

A specification advances along **two distinct paths**, with different costs and
different gates. Conflating them is what makes additive improvement feel blocked
by release machinery. They are independent: a specification can take the
additive path many times between two release cuts.

### Path 1 — additive revision (the unblocking path)

Use this whenever a confirmed learning needs to land and the change is
strictly additive. It is the frequent, cheap motion. **No readiness review
and no quiescence gate apply.**

The rules:

- **The predecessor is frozen.** The current released version or release
  candidate is not edited. It remains the **corpus-production reference**:
  existing corpora and adapters keep validating against it unchanged. Freezing
  the predecessor is what decouples production from in-flight spec work — neither
  blocks the other.
- **The successor is a strictly-additive revision.** It increments the version
  per the additive rule under *Independent SemVer* above. *Additive* means: no
  field, type, baseline value or section is removed, changed or renumbered; no
  requirement is removed or relaxed; no value is added to any enumeration,
  whether or not it admits `x-*` values, nor any baseline kind or baseline
  `trace_type`, except as a specification's own versioning section allows
  (envelope §4.2 for documented `x-*` kinds, scenario §14 for its closed
  vocabularies); and a
  requirement it adds binds only documents that declare it or a later revision
  (`minitrace-spec` §3.1). If the change is not additive, it is not Path 1 — it is a breaking
  revision (a change of `Y` before `1.0.0`) and belongs in a release cut (Path 2).
- **Content admission is governed by §16, not by this path.** A change may
  enter the additive revision only if it is **second-deployment-confirmed**
  (the §16 convention-absorption rule in `AGENTS.md`) **or** a **registered
  provisional contract** — a value or shape documented producer-side and
  registered in the spec at the same commit, promoted to normative only at the
  next revision, never editing the frozen predecessor (the producer-ahead
  discipline; see the envelope `x-*` provisional kinds for the worked form). A
  first-deployment candidate is **not** admissible: it stays parked in the §16
  queue. Primitives of the adapter DSL (actions, built-ins, predicates) are the
  exception: they are admitted under the DSL specification's own §16
  (Versioning), on one adapter author's demonstration that a target harness
  output cannot be mapped without them, not under the second-deployment rule or
  as a provisional contract.
- **Dual-validate.** Production artifacts validate against the frozen
  predecessor's schema; new artifacts exercising the addition validate against
  the successor's schema. The predecessor schema file is left unchanged; the
  successor ships its own schema file.
- **The revision matures as a working draft** (`-draft-N` working-tree file)
  and states in its own text what it changes against its predecessor (as the
  envelope specification's §12 does). It is not a release; it does not require
  the bundle to move.

The only gate on Path 1 is the two checks above: *is it strictly additive?* and
*is the content second-deployment-confirmed, a registered provisional contract,
or a DSL primitive admitted under the DSL specification's own §16?* If both
hold, it proceeds without waiting on anything else.

### Path 2 — release cut (the gated, terminal motion)

Use this to promote accumulated additive revisions to a final filename and tag
a release across the bundle. It is rare and deliberately heavy. This is where
the release machinery fires:

- the **quiescence gate** — release prep cannot honestly begin while the §16
  candidate queue is still growing;
- the **readiness review** before the cut, with its abort and sequencing
  conditions; and
- the **filename promotion** (drop `-draft`/`-rc`) and the coordinated **git
  tag** described above.

A breaking revision (a change of `Y` before `1.0.0`, of the major from `1.0.0`
on) also goes through Path 2, because it
cannot satisfy the additivity rule of Path 1.

**Back-fold (Path 2 is not forward-only).** A release cut runs against the
publication tree, so the fixes and decisions its review surfaces land *there*.
A cut is **not done** until those findings are folded back into the dev tree —
the authoritative source for the next revision — as a dated review-findings
note, and, where a finding is a real spec defect, as a candidate for the next
additive revision (Path 1). Without this step a downstream fix stays a commit
message in the publication tree and the next revision re-inherits the defect
from dev.

### The adapter track feeds Path 1; it does not gate it

Building an adapter runs on its own rhythm (the adapter cycle in `AGENTS.md`):
deep-dive → scope → preprocessor → DSL → smoke test → corpus run → regression
pin → §16 yield. An adapter cycle forces **nothing** on any specification. Its
only spec-facing output is §16 yield: it either parks a first-deployment
candidate or supplies the second deployment that confirms an existing one. Only
that confirmation makes content admissible to a Path-1 revision, apart from a
DSL primitive, which one adapter's demonstrated need admits under the DSL
specification's own §16. An adapter is
"integrated" when its cycle closes and its regression baseline is pinned —
independent of whether any spec is mid-revision.

Note the standing tension: starting a new
adapter cycle adds validation evidence *and* can surface new candidates, which
re-trips the §16 quiescence gate. So adapter work advances the portfolio while
pushing a *release* (Path 2) further out — by design. It does not push out
additive revisions (Path 1), which do not depend on quiescence.

### Why "move to rc2" is usually the wrong frame

Because specifications version independently, there is rarely a single
bundle-wide "rc2" event. The next step for a given specification is almost
always a Path-1 additive revision of *that* spec, taken on its own evidence:

- The **envelope** spec's step after v0.1.0 was `v0.1.1`, an additive revision
  (Path 1) except that entries removed as a whole carry no file fields, which
  v0.1.0 required (envelope §12), drafted on its own evidence without waiting
  on, or pulling forward, any other spec.
- The **trace** spec's `v0.3.0` rc2 was a *separate* question, held behind the
  quiescence gate (Path 2); it did not block the envelope's `v0.1.1`, and the
  envelope's `v0.1.1` did not advance it.

A coordinated RC/release naming step (rc1 → rc2 → release) is a **bundle**
motion under Path 2, taken only when the §16 queue has quiesced and every
specification is ready to move to the same *stage* together. Between those
bundle motions, individual specifications advance additively under Path 1
without a bundle release and without tripping the release gates.
