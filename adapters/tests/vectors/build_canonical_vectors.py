"""Build canonical-vectors.json, the implementation-independent canonicalisation
vectors for minitrace-spec v0.3.0 §3.4 (canonical form and numeric domain).

Each vector carries the exact input text and an expected verdict:

  canonical      the input is conformant; `canonical` is its RFC 8785 form
                 and `sha256` the digest of those bytes
  not-conformant the input violates §3.4; `reason` is number-outside-safe-range
                 (a number beyond ±(2^53-1) by value, in any spelling),
                 non-finite-number (NaN, Infinity, an overflow such as 1e999)
                 or depth-exceeds-64 (nesting deeper than 64)

Expected values come from the Python reference (source_reader.trace_json_verdict
for the numeric domain and depth, rfc8785 for the canonical form). Every canonical
vector is cross-checked against the Rust jcs-oracle at build time, and the
build refuses to write the file if the two disagree, so the file never records
one implementation's reading as the expected value.

Run from adapters/:
    python tests/vectors/build_canonical_vectors.py           # check: rebuild in memory, compare, write nothing
    python tests/vectors/build_canonical_vectors.py --write   # rewrite the tracked file
Exit status in check mode: 0 if the tracked file equals the rebuild, 1 if it differs.
Prerequisite:
    cargo build --release --manifest-path jcs-oracle/Cargo.toml
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

import rfc8785

HERE = pathlib.Path(__file__).resolve().parent
ADAPTERS = HERE.parent.parent
sys.path.insert(0, str(ADAPTERS))
from source_reader import trace_json_verdict  # noqa: E402

ORACLE_BIN = ADAPTERS / "jcs-oracle" / "target" / "release" / "jcs-oracle"
OUT = HERE / "canonical-vectors.json"

# (name, input text, note). Inputs are fed verbatim as UTF-8 bytes.
CASES = [
    ("baseline_reorder", '{"b":2,"a":1}', "alphabetic key reorder"),
    ("e_neg_7", '{"x":1e-7}', "below 1e-6: canonical form uses exponent notation"),
    ("e_pos_16", '{"x":1e16}', "1e16 lies beyond 2^53-1 by value: outside the §3.4 domain whatever its spelling"),
    ("e_pos_21", '{"x":1e21}', "1e21 lies beyond 2^53-1 by value: outside the §3.4 domain (no conformant trace reaches RFC 8785's 1e21 exponent switch)"),
    ("exponent_uppercase", '{"x":1E3}', "source notation carries no meaning"),
    ("bmp_vs_non_bmp_keys", '{"\\uE000":"BMP-high","\\uD840\\uDC00":"non-BMP"}',
     "key sort by UTF-16 code units (RFC 8785 §3.2.3): the non-BMP key sorts first"),
    ("empty_object", "{}", "empty object"),
    ("empty_array", "[]", "empty array"),
    ("deep_nesting", '{"a":{"b":{"c":{"d":{"e":{"f":{"g":{"h":1}}}}}}}}', "8-level object nesting"),
    ("deep_array", "[" * 32 + "1" + "]" * 32, "32-level array nesting"),
    ("depth_64_array", "[" * 64 + "1" + "]" * 64, "array nesting at the §3.4 depth limit of 64: conformant"),
    ("depth_64_object", '{"a":' * 64 + "1" + "}" * 64, "object nesting at the §3.4 depth limit of 64: conformant"),
    ("depth_65_array", "[" * 65 + "1" + "]" * 65, "array nesting one beyond the §3.4 depth limit"),
    ("non_bmp_string_value", '{"k":"\\uD834\\uDD1E"}', "non-BMP character in a value"),
    ("escape_equiv_literal", '{"k":"\U0001D11E"}', "literal UTF-8 form of U+1D11E; same canonical bytes as the escaped form"),
    ("escape_equiv_surrogate", '{"k":"\\uD834\\uDD1E"}', "surrogate-pair escape of U+1D11E; same canonical bytes as the literal form"),
    ("mixed_array", '[1,"two",null,true,false,3.14,{"k":"v"}]', "array order preserved"),
    ("control_chars", '{"k":"\\u0000\\u0008\\u0009\\u000a\\u000b\\u000c\\u000d\\u001f"}',
     "C0 escapes: short forms for b t n f r, lowercase \\u00xx otherwise (RFC 8785 §3.2.2.2)"),
    ("large_string", '{"k":"' + "a" * 20000 + '"}', "20 000-character value, no truncation"),
    ("negative_zero", '{"x":-0}', "negative zero canonicalises to 0"),
    ("negative_zero_float", '{"x":-0.0}', "negative zero in float form canonicalises to 0"),
    ("max_safe_integer", '{"x":9007199254740991}', "2^53-1, the largest admissible integer literal"),
    ("min_safe_integer", '{"x":-9007199254740991}', "-(2^53-1), the smallest admissible integer literal"),
    ("bigint_as_string", '{"x":"9007199254740993"}', "an out-of-range integer carried as a string, as §3.4 requires"),
    ("float_form_beyond_safe", '{"x":9007199254740993.0}',
     "float spelling of 2^53+1, binary64 value 9007199254740992: outside the §3.4 domain by value"),
    ("two_pow_53", '{"x":9007199254740992}', "2^53 as an integer literal: outside the range although exactly representable"),
    ("beyond_safe_integer", '{"x":9007199254740993}', "2^53+1 as an integer literal"),
    ("beyond_safe_integer_negative", '{"x":-9007199254740993}', "-(2^53+1) as an integer literal"),
    ("nan", '{"x":NaN}', "NaN is not JSON and not admissible"),
    ("infinity", '{"x":Infinity}', "Infinity is not JSON and not admissible"),
    ("negative_infinity", '{"x":-Infinity}', "-Infinity is not JSON and not admissible"),
    ("float_overflow", '{"x":1e999}', "overflows binary64 to infinity"),
    # I-JSON (§3.4 "JSON form", RFC 7493; T19).
    ("duplicate_key", '{"x":1,"x":2}', "two members named x: no single canonical form (one reader keeps the last, another refuses)"),
    ("duplicate_key_nested", '{"a":{"b":1,"b":1}}', "a repeated name in a nested object, even with equal values"),
    ("lone_high_surrogate", '{"k":"\\uD834"}', "an unpaired high surrogate escape in a value"),
    ("lone_low_surrogate", '{"k":"a\\uDD1E"}', "an unpaired low surrogate escape in a value"),
    ("lone_surrogate_member_name", '{"\\uD834":1}', "an unpaired surrogate in a member name"),
    ("noncharacter_fdd0", '{"k":"\\uFDD0"}', "U+FDD0, the first of the 32 contiguous noncharacters"),
    ("noncharacter_fffe_literal", '{"k":"￾"}', "U+FFFE as literal UTF-8"),
    ("noncharacter_plane16", '{"k":"\\uDBFF\\uDFFF"}', "U+10FFFF as a surrogate pair: a noncharacter in plane 16"),
    ("noncharacter_member_name", '{"\\uFDEF":1}', "U+FDEF, the last contiguous noncharacter, in a member name"),
    ("noncharacter_neighbours", '{"k":"\\uFDCF\\uFDF0\\uFFFD\\uD83F\\uDFFD"}',
     "U+FDCF, U+FDF0, U+FFFD and U+1FFFD sit next to noncharacters and are characters"),
]


def python_verdict(text: str) -> dict:
    value, reason = trace_json_verdict(text)
    if reason:
        return {"verdict": "not-conformant", "reason": reason}
    canonical = rfc8785.dumps(value)
    return {
        "verdict": "canonical",
        "canonical": canonical.decode("utf-8"),
        "sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }


def oracle_canonical(raw: bytes) -> bytes:
    proc = subprocess.run([str(ORACLE_BIN)], input=raw, capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"jcs-oracle failed: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or check canonical-vectors.json (minitrace-spec v0.3.0 §3.4).")
    parser.add_argument("--write", action="store_true", help="rewrite the tracked vector file; without it, only compare")
    args = parser.parse_args()
    if not ORACLE_BIN.is_file():
        print(f"jcs-oracle not built: {ORACLE_BIN}", file=sys.stderr)
        return 2
    vectors = []
    names = set()
    for name, text, note in CASES:
        assert name not in names, name
        names.add(name)
        v = {"name": name, "input": text, "note": note, **python_verdict(text)}
        if v["verdict"] == "canonical":
            rust = oracle_canonical(text.encode("utf-8"))
            if rust != v["canonical"].encode("utf-8"):
                print(f"[{name}] Python and Rust disagree; not writing.\n"
                      f"  python: {v['canonical'][:120]!r}\n  rust:   {rust[:120]!r}", file=sys.stderr)
                return 1
        vectors.append(v)
    doc = {
        "description": "Canonicalisation and numeric-domain vectors for minitrace-spec v0.3.0 §3.4. "
                       "Generated by build_canonical_vectors.py; do not edit by hand.",
        "spec": "minitrace-spec v0.3.0 §3.4",
        "verdicts": {
            "canonical": "conformant; `canonical` is the RFC 8785 form and `sha256` its digest",
            "not-conformant": "violates the §3.4 numeric domain or depth limit; `reason` names the rule",
        },
        "vectors": vectors,
    }
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    counts = {}
    for v in vectors:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    if args.write:
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT.name}: {len(vectors)} vectors {counts}")
        return 0
    current = OUT.read_text(encoding="utf-8") if OUT.is_file() else None
    if current == text:
        print(f"check ok: {OUT.name} equals the rebuild ({len(vectors)} vectors {counts}); nothing written")
        return 0
    print(f"check FAILED: {OUT.name} differs from the rebuild; run with --write to regenerate", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
