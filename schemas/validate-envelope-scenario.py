"""Validate the envelope-manifest and scenario JSON Schemas against worked
examples and a set of negative cases.

Per minitrace-envelope-spec §13 and minitrace-scenario-spec §15, the schemas are
published with the specs, authored against the settled (RC) shapes. This
script checks that:
  - each schema is itself a valid Draft 2020-12 schema;
  - the specs' own worked examples (and the extension paths) validate;
  - a binding record validates against the scenario schema's #/$defs/BindingRecord
    and signature.json against the envelope schema's #/$defs/SignatureFile;
  - representative spec-violating documents are REJECTED (so the constraints bite).

Run:  python schemas/validate-envelope-scenario.py
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

import jsonschema
from jsonschema.exceptions import SchemaError
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
EX = ROOT / "examples"

ENV_SCHEMA = json.loads((SCHEMAS / "minitrace-envelope-v0.1.0.schema.json").read_text())
ENV11_SCHEMA = json.loads((SCHEMAS / "minitrace-envelope-v0.1.1.schema.json").read_text())
SCN_SCHEMA = json.loads((SCHEMAS / "minitrace-scenario-v0.1.0.schema.json").read_text())

Validator = jsonschema.Draft202012Validator


def subschema(schema: dict, pointer: str) -> dict:
    """Resolve a #/$defs/Name fragment to a standalone schema carrying $defs."""
    node = schema
    for part in pointer.lstrip("#/").split("/"):
        node = node[part]
    out = copy.deepcopy(node)
    out["$defs"] = schema["$defs"]
    return out


BINDING_SCHEMA = subschema(SCN_SCHEMA, "#/$defs/BindingRecord")
SIGFILE_SCHEMA = subschema(ENV_SCHEMA, "#/$defs/SignatureFile")
REDACTION_V010 = subschema(ENV_SCHEMA, "#/$defs/Redaction")
REDACTION_V011 = subschema(ENV11_SCHEMA, "#/$defs/Redaction")


def load(path: pathlib.Path):
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(path.read_text())
    return json.loads(path.read_text())


results: list[tuple[bool, str]] = []


def expect_ok(schema: dict, doc, label: str) -> None:
    errs = sorted(Validator(schema).iter_errors(doc), key=str)
    if errs:
        results.append((False, f"EXPECTED-OK FAILED  {label}"))
        for e in errs[:4]:
            results.append((False, f"      - {e.message} at {list(e.absolute_path)}"))
    else:
        results.append((True, f"ok    {label}"))


def expect_fail(schema: dict, doc, label: str) -> None:
    errs = list(Validator(schema).iter_errors(doc))
    if errs:
        results.append((True, f"ok    (rejected) {label}"))
    else:
        results.append((False, f"EXPECTED-FAIL PASSED  {label}  (schema accepted a document it should reject)"))


def main() -> int:
    # 0. The schemas are themselves valid Draft 2020-12 schemas.
    for name, sch in (("envelope", ENV_SCHEMA), ("envelope v0.1.1", ENV11_SCHEMA), ("scenario", SCN_SCHEMA)):
        try:
            Validator.check_schema(sch)
            results.append((True, f"ok    {name} schema is valid Draft 2020-12"))
        except SchemaError as e:
            results.append((False, f"SCHEMA INVALID  {name}: {e.message}"))

    # 1. Positive: envelope manifests.
    expect_ok(ENV_SCHEMA, load(EX / "envelope" / "manifest-minimal.json"), "envelope manifest-minimal (Appendix A)")
    expect_ok(ENV_SCHEMA, load(EX / "envelope" / "manifest-bindings-witness.json"), "envelope manifest-bindings-witness (bindings + x-* + manipulated clock)")
    # signature.json against the SignatureFile $def.
    expect_ok(SIGFILE_SCHEMA, load(EX / "envelope" / "signature.json"), "signature.json against #/$defs/SignatureFile")

    # 2. Positive: scenarios.
    expect_ok(SCN_SCHEMA, load(EX / "scenario" / "single-turn.scenario.yaml"), "scenario single-turn (Appendix A)")
    expect_ok(SCN_SCHEMA, load(EX / "scenario" / "multi-turn-branching.scenario.yaml"), "scenario multi-turn-branching (Appendix B)")
    expect_ok(SCN_SCHEMA, load(EX / "scenario" / "multi-turn-per-turn-gt.scenario.yaml"), "scenario multi-turn per-turn ground_truth (§7 at-least-one)")
    # binding record against the BindingRecord $def.
    expect_ok(BINDING_SCHEMA, load(EX / "scenario" / "run-binding.json"), "run-binding.json against #/$defs/BindingRecord")
    expect_ok(BINDING_SCHEMA, {
        "binding_version": "0.1.0",
        "scenario": {"id": "s", "version": "1", "schema_version": "0.1.0",
                     "content_hash": "sha256:" + "a" * 64},
        "model": {"id": "some-model", "digest": None},
        "harness": {"name": "goose", "version": "1.0"},
        "run_spec_hash": "sha256:" + "b" * 64,
    }, "binding record with null model.digest (§11.1 'named not pinned')")

    # 3. Negative: envelope.
    base_env = load(EX / "envelope" / "manifest-minimal.json")

    d = copy.deepcopy(base_env); del d["traces"]
    expect_fail(ENV_SCHEMA, d, "envelope without traces (§4.1 required / §6.1)")

    d = copy.deepcopy(base_env); d["traces"] = []
    expect_fail(ENV_SCHEMA, d, "envelope with zero traces (§6.1 minItems 1)")

    d = copy.deepcopy(base_env); d["envelope_version"] = "0.2.0"
    expect_fail(ENV_SCHEMA, d, "envelope wrong version (schema is v0.1.0-pinned)")

    d = copy.deepcopy(base_env); d["traces"][0]["trace_content_hash"] = "deadbeef"
    expect_fail(ENV_SCHEMA, d, "envelope trace hash not sha256:<64hex>")

    d = copy.deepcopy(base_env); d["traces"][0]["path"] = "../escape/trace.json"
    expect_fail(ENV_SCHEMA, d, "envelope trace path with .. traversal (§3.2)")

    d = copy.deepcopy(base_env); d["traces"][0]["path"] = "./traces/trace.json"
    expect_fail(ENV_SCHEMA, d, "envelope trace path with leading '.' segment (§3.2)")

    d = copy.deepcopy(base_env); d["raw_artefacts"][0]["path"] = "raw/a/../b/x.log"
    expect_fail(ENV_SCHEMA, d, "envelope raw path with mid-path '..' segment (§3.2)")

    # Segment-accurate RelPath must still ACCEPT a dot-bearing filename that is not a traversal.
    d = copy.deepcopy(base_env); d["raw_artefacts"][0]["path"] = "raw/archive..2026/output.log"
    expect_ok(ENV_SCHEMA, d, "envelope raw path with '..' inside a filename (not a segment) accepted (§3.2)")

    d = copy.deepcopy(base_env)
    d["traces"].append(copy.deepcopy(d["traces"][0]))
    d["traces"][1]["trace_id"] = "second"
    expect_fail(ENV_SCHEMA, d, "envelope with two primary:true traces (§4.6 at most one)")

    d = copy.deepcopy(base_env)
    d["clocks"].append({"id": "m", "kind": "manipulated", "manipulated": True})
    expect_fail(ENV_SCHEMA, d, "manipulated clock without manipulation_notes (§8.3)")

    d = copy.deepcopy(base_env)
    d["witnesses"].append({
        "id": "w-exec", "kind": "x-process-exec-trace",
        "scope": {"type": "envelope"},
        "captured_at": "2026-05-03T10:00:02.000000Z",
        "content_hash": "sha256:" + "e" * 64,
        "primary_path": "witnesses/w-exec/exec.ndjson",
        "metadata": {"clock_source": "kernel_monotonic"}
    })
    expect_fail(ENV_SCHEMA, d, "kernel x-* witness missing metadata.tool_call_id_attribution (§7.6.1)")

    d = copy.deepcopy(base_env)
    d["witnesses"][0]["scope"] = {"type": "trace"}
    expect_fail(ENV_SCHEMA, d, "trace-scoped witness missing target_trace_id (§7.3)")

    # 4. Negative: scenario.
    base_scn = load(EX / "scenario" / "single-turn.scenario.yaml")

    d = copy.deepcopy(base_scn); d["unexpected_key"] = 1
    expect_fail(SCN_SCHEMA, d, "scenario unknown top-level key (§2.1 closed top level)")

    d = copy.deepcopy(base_scn); d["metadata"]["complexity"] = "trivial"
    expect_fail(SCN_SCHEMA, d, "scenario bad complexity enum (§4)")

    d = copy.deepcopy(base_scn); d["schema_version"] = "0.1.1"
    expect_ok(SCN_SCHEMA, d, "scenario declaring a later 0.1 revision, 0.1.1 (§14)")

    d = copy.deepcopy(base_scn); d["schema_version"] = "0.2.0"
    expect_fail(SCN_SCHEMA, d, "scenario declaring 0.2.0, outside the 0.1 line (§14)")

    d = copy.deepcopy(base_scn); d["interaction"]["prompt_path"] = "p.txt"
    expect_fail(SCN_SCHEMA, d, "scenario single-turn with both prompt and prompt_path (§5.1 XOR)")

    d = copy.deepcopy(base_scn); d["interaction"]["prompt"] = 123
    expect_fail(SCN_SCHEMA, d, "scenario single-turn prompt with non-string value (§5.1)")

    d = copy.deepcopy(base_scn); d["interaction"]["mode"] = "multi-turn"
    expect_fail(SCN_SCHEMA, d, "scenario multi-turn carrying top-level prompt (§5.4)")

    d = copy.deepcopy(base_scn); del d["ground_truth"]
    expect_fail(SCN_SCHEMA, d, "single-turn scenario with no ground_truth anywhere (§7 at-least-one)")

    d = copy.deepcopy(base_scn); d["ground_truth"]["comparison"] = "fuzzy"
    expect_fail(SCN_SCHEMA, d, "file-diff with non-exact comparison (§7.2)")

    d = copy.deepcopy(base_scn); del d["workspace"]
    expect_fail(SCN_SCHEMA, d, "file-diff scenario without workspace (§6)")

    d = load(EX / "scenario" / "multi-turn-branching.scenario.yaml")
    d = copy.deepcopy(d); d["interaction"]["turns"][0]["when"] = {"prev_response_matches": "x"}
    expect_fail(SCN_SCHEMA, d, "multi-turn first turn carrying a `when` guard (§5.3)")

    d = load(EX / "scenario" / "multi-turn-branching.scenario.yaml")
    d = copy.deepcopy(d)
    d["interaction"]["turns"][1]["when"] = {"prev_response_matches": "a", "else": True}
    expect_fail(SCN_SCHEMA, d, "guard with two predicate keys (§5.3 exactly one)")

    d = load(EX / "scenario" / "multi-turn-branching.scenario.yaml")
    d = copy.deepcopy(d)
    d["interaction"]["turns"][1]["when"] = {"prev_response_contains": "a"}
    expect_fail(SCN_SCHEMA, d, "guard with unknown predicate key (§5.3 closed vocabulary)")

    # 5. Negative: binding record.
    base_bind = load(EX / "scenario" / "run-binding.json")

    d = copy.deepcopy(base_bind); del d["model"]
    expect_fail(BINDING_SCHEMA, d, "binding record missing model (§11.1)")

    d = copy.deepcopy(base_bind); d["scenario"]["content_hash"] = "nope"
    expect_fail(BINDING_SCHEMA, d, "binding record scenario.content_hash not sha256 (§11.1)")

    # 6. Envelope v0.1.1 §7.4 key-set redaction.
    #
    # NB the rest of this script is pinned to the v0.1.0 schema. Everything in
    # this block runs against v0.1.1 deliberately: without it, edits to the
    # v0.1.1 schema are unexercised and this script reports green having tested
    # none of them.
    base_red = {
        "operation": "mask",
        "reason_code": "operator_privacy",
        "authorised_by": "capture-pipeline",
        "authorised_at": "2026-08-25T10:00:00Z",
    }

    # Positive: the v0.1.0 shape still validates, which is the additive claim.
    expect_ok(REDACTION_V011, base_red, "v0.1.1 redaction without target_kind (§7.4 default 'witness')")

    d = dict(base_red, target_kind="witness")
    expect_ok(REDACTION_V011, d, "v0.1.1 redaction explicit target_kind witness")

    keyset = dict(base_red, target_kind="payload_keys", targets=["user.email", "host.name"])
    expect_ok(REDACTION_V011, keyset, "v0.1.1 payload_keys redaction with targets (§7.4)")

    d = dict(keyset, original_path_hashes={"witnesses/w1/otel.ndjson": "sha256:" + "a" * 64})
    expect_ok(REDACTION_V011, d, "v0.1.1 payload_keys redaction with original_path_hashes (§7.4)")

    # Negative: the constraints the new field exists to carry.
    missing_targets = dict(base_red, target_kind="payload_keys")
    expect_fail(REDACTION_V011, missing_targets, "v0.1.1 payload_keys without targets (§7.4 REQUIRED)")

    d = dict(base_red, targets=["user.email"])
    expect_fail(REDACTION_V011, d, "v0.1.1 targets without target_kind (§7.4 MUST NOT be present otherwise)")

    d = dict(base_red, target_kind="witness", targets=["user.email"])
    expect_fail(REDACTION_V011, d, "v0.1.1 targets alongside target_kind witness (§7.4)")

    d = dict(base_red, target_kind="payload_keys", targets=[])
    expect_fail(REDACTION_V011, d, "v0.1.1 payload_keys with empty targets (§7.4 minItems 1)")

    d = dict(base_red, target_kind="payload_fields", targets=["user.email"])
    expect_fail(REDACTION_V011, d, "v0.1.1 unknown target_kind (§7.4 closed vocabulary)")

    d = dict(keyset, original_path_hashes={"witnesses/w1/otel.ndjson": "nope"})
    expect_fail(REDACTION_V011, d, "v0.1.1 original_path_hashes value not sha256 (§7.4)")

    # Kill-tests. Each negative above must fail BECAUSE of the v0.1.1 addition.
    # v0.1.0's Redaction carries additionalProperties:true and no conditional, so
    # it accepts both documents. If either of these two flipped to rejected, the
    # rejections above would be attributable to something pre-existing and this
    # block would be testing nothing.
    expect_ok(REDACTION_V010, missing_targets,
              "kill-test: v0.1.0 ACCEPTS payload_keys-without-targets (so the v0.1.1 rejection is the new constraint)")
    expect_ok(REDACTION_V010, keyset,
              "kill-test: v0.1.0 ACCEPTS a key-set redaction (so v0.1.1 cannot have invalidated a v0.1.0 key-set redaction)")

    # Additivity at whole-manifest scope: v0.1.0-valid manifests validate under v0.1.1.
    for fname in ("manifest-minimal.json", "manifest-bindings-witness.json"):
        expect_ok(ENV11_SCHEMA, load(EX / "envelope" / fname), f"v0.1.1 accepts v0.1.0 example {fname} (§12 additive)")

    # Report.
    failed = sum(1 for ok, _ in results if not ok)
    for ok, line in results:
        print(line)
    print()
    total = len(results)
    print(f"{total - failed}/{total} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
