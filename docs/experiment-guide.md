# Experiment Guide

How to design and run reproducible minitrace experiments.

This guide covers the controlled profile. Organic sessions are captured post-hoc and follow the adapter guide instead.

## Pre-run checklist

Before starting a controlled run, verify:

- [ ] Scenario spec is complete and versioned.
- [ ] Model and version are documented.
- [ ] System prompt is captured.
- [ ] Tools are configured as specified in the scenario.
- [ ] Capture mechanism is active.
- [ ] Privacy level is decided (full, anonymous, or minimal).
- [ ] For multi-session scenarios: project_id is assigned.

## During the run

- Capture all turns with timestamps.
- Capture all tool calls with full input/output.
- Do not intervene unless the scenario specifies it.
- Note any deviations from protocol.
- For delegated agents: capture the sub-session or at minimum a summary.

## Post-run

- Compute metrics (turn count, tool ratios, timing).
- Annotate outcome (success, failure, or partial).
- Apply failure codes if applicable.
- Map to external taxonomies (MAST, ToolEmu).
- Produce a handover document if this is part of a multi-session scenario.
- Store the complete session in minitrace format.

## Multi-session protocols

**Sequential sessions:**

1. Complete session N.
2. Produce a handover document.
3. Start session N+1 with the handover as input.
4. Link sessions via the `predecessor_session` field.

**Parallel sessions (naturalistic):**

1. Assign a shared `project_id`.
2. Capture `concurrent_sessions` count if known.
3. Note `human_attention` state (focused, divided, or unknown).
4. Analyze coordination failures post-hoc via project grouping.

## Comparison runs

For valid comparison across conditions:

- Use the same scenario and the same model. Vary one condition at a time.
- Run a minimum of 3 runs per condition to get a variance estimate.
- Report all runs, not just successes or failures.
