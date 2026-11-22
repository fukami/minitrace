# claude-code adapter — regression & drift watch

Two complementary guards over the claude-code conformance unit. They watch
different drift axes and must not be conflated.

| Tool | Axis | Catches | Inputs |
|---|---|---|---|
| `<ver>/verify.py` | adapter code | a preprocess/DSL change that alters output over **fixed** inputs (per-trace `content_hash`, trace/unmapped counts) | the frozen `<ver>/inputs/` corpus only |
| `drift_watch.py` | harness format / coverage (Axis C) | a **new harness release** that ships native shapes the adapter no longer covers, or a contract cell that goes dark | frozen baseline **and** a fresh capture at a new image |

`verify.py` answers "did *we* change the adapter?"; `drift_watch.py` answers "did
*the harness* change under us?". Run both.

## Frozen versions (rungs)

| Rung | Harness | Role |
|---|---|---|
| `2.1.153/` | claude-code 2.1.153 | **anchor** — the reference other work cites; keep. |
| `2.1.215/` | claude-code 2.1.215 | second rung. Fired-cell sets identical to the anchor; the `agent_listing_delta` payload and the delegation Task-result `resolvedModel` are accepted as registration/redundant noise (neither a new modelled field). |
| `2.1.220/` | claude-code 2.1.220 | third rung (2026-07-25). Clean delta from 2.1.215: no drift signal fired. Notable because the delta spans cc 2.1.219 (subagent spawn-depth default 1 -> 3, nested stream-json forwarding, Opus 5 as default Opus) — none of it reached the session/sidechain transcript shape; the capture kept `MODEL=claude-opus-4-8` pinned so the harness stayed the only variable. |

The ladder grows, it does not replace: a new harness release adds a rung rather
than re-freezing the anchor. `drift_watch --baseline 2.1.153` against a newer
capture correctly stays exit 1 (it reports the real delta from the anchor);
watch fresh captures against the **newest** rung (`--baseline 2.1.220`).

## verify.py (per frozen version)

```
cd 2.1.153 && uv run --with pyyaml --with rfc8785 python verify.py
```

Deterministic content-hash freeze. Exit 1 on any adapter-output change; if the
change was intentional, re-freeze `manifest.json` (hashes + counts) alongside the
adapter commit. `content_hash` folds `provenance.source_path`, so inputs must stay
at `inputs/<name>.jsonl` — the script pins that convention.

## drift_watch.py (cross-version)

```
# self-test: candidate = frozen baseline, green by construction (proves the harness)
uv run --with pyyaml --with rfc8785 python drift_watch.py --baseline 2.1.153 --operator-type <type>

# drift run: candidate = a fresh capture at a new image
uv run --with pyyaml --with rfc8785 python drift_watch.py --baseline 2.1.153 --operator-type <type> \
    --candidate-inputs /path/to/latest-capture/inputs
```

Signals, per scenario, keyed by name from `<baseline>/manifest.json`:

0. **NEW RAW SHAPE** — read from the **raw** capture (including the subagent
   sidechain), upstream of the preprocessor, which drops unknown top-level fields /
   record types so the event arms below are blind to exactly this case.
   - a **new record TYPE** → **FAIL** (unambiguous, rare).
   - a **new field PATH** → **WARNING**, not a gate. A fresh live capture
     legitimately carries optional fields (`isApiErrorMessage`, `isMeta`, …) and
     freeform tool/result/content subtrees that vary run-to-run; gating on paths
     would red-flag every benign recapture. A new field that matters to the adapter
     and is *carried through* the preprocessor still fails signal 2. (`<kind>`
     variance — null vs populated, empty vs full list — is ignored regardless.)
1. **DARK CELL** — a cell in the scenario's `annotated_cells_fired` that no longer
   fires → **FAIL**. Fired cells are derived **structurally** from the emitted trace
   (op types, lineage edges, annotation-taxonomy suffixes, token metrics),
   version-agnostically. In **self-test** the check is exact both ways: an *over-fire*
   (a cell the extractor fires that the manifest does not annotate) also fails, so a
   stale annotation or a broken-open (always-true) predicate is caught at baseline.
2. **NEW UNMAPPED** — a field the preprocessor carries into events but the DSL does
   not consume, present in the candidate and not the baseline → **FAIL**.
3. **DROPPED DEPENDENCY** — a field the DSL reads (`consumed_but_absent_in_record`)
   present at baseline, absent at candidate → **FAIL**. The harness removed/renamed it.

Exit 0 = no drift (warnings may print); 1 = drift; 2 = harness/usage error.

## Capturing a candidate set (the manual Phase-3 cycle)

Re-run the seven conformance scenarios against the new image on **the arm64 capture workstation** (OrbStack;
`docker`, `uv`, and the API call need the sandbox disabled), collect each session's
raw transcript to `<capture>/inputs/<name>.jsonl`, then run `drift_watch.py
--baseline <ver> --candidate-inputs <capture>/inputs --operator-type <type>`. The scenario prompts and their host-staged
config (hook `settings.json`, the `/probe` command, the probe skill) are in
`../../../../miniobserve/frameworks/claude-code/scenarios/`.

**Requirements for a faithful candidate:**

- **Config must travel with every run.** The seven scenarios need their config staged
  into the mount-side `HOME` (`<CAP>/.miniobserve/home/.claude/`): the hook trio, the
  `/probe` command, the probe skill. Hand-staging is error-prone across seven runs —
  the intended end-state bakes this config into the image (seeded into `$HOME/.claude/`
  by the entrypoint) so every run carries it identically. **[not yet done: requires an
  image + entrypoint change.]**
- **Preserve the subagent sidechain.** The delegation capture must keep the native
  `<stem>/subagents/*.jsonl` layout the preprocessor auto-discovers. Drop it and
  lineage cells 12/13 falsely go dark.
- **Scenario file basenames must match** the manifest `input` basenames
  (`s1-fetch.jsonl`, `delegation.jsonl`, `tool-fill.jsonl`, `websearch.jsonl`,
  `hooks.jsonl`, `slashcmd.jsonl`, `skill-write.jsonl`).

On any FAIL: add/adjust DSL rules to cover the new shape, re-capture, then re-freeze
this version's `manifest.json` (and `verify.py` hashes) at the new harness version.
```
