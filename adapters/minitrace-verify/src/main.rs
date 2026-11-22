//! Trace-aware verifier for minitrace v0.3.0 §3.4 `content_hash`.
//!
//! Two modes, selected by how many traces are supplied:
//!
//! - **Single-trace** (`--file PATH` once, or stdin): strip the trace's
//!   `content_hash`, canonicalise the remainder per RFC 8785 (JCS), SHA-256
//!   the canonical bytes, compare to the stored `content_hash`.
//!
//! - **Set / edge-consistency** (`--file PATH` more than once, or `--dir DIR`):
//!   load all traces, self-verify each one that carries a non-null
//!   `content_hash`, AND verify cross-trace integrity edges (§7.1): for every
//!   `lineage[]` edge that carries a resolved `target_content_hash`, the
//!   referenced in-set trace MUST carry a `content_hash` (§7.1) and
//!   it MUST equal the edge's `target_content_hash`. Edges whose referent is
//!   not in the supplied set are reported but not failed (external referent).
//!   This is the verifier-side realisation of the producer-side resolver in
//!   `adapters/cross_trace.py`; it does NOT resolve placeholders (see below).
//!
//! `--strict` (set mode only; it needs `--file` or `--dir`, and a one-trace
//! `--dir` stays in set mode): a set passes only if everything in it was
//! verified. A trace with a null `content_hash`, an edge to a trace outside
//! the set, and an edge whose hash differs from its referent's then each
//! count as a problem instead of being reported only.
//!
//! Exit codes
//! - `0` all checks passed
//! - `1` a check ran and found a problem: a hash mismatch, a cross-trace edge
//!       naming a referent with no hash, a trace outside the §3.4 numeric
//!       domain (below) or nested deeper than
//!       its 64 levels (at any depth, also past the parser's own limit of
//!       128, where a trace is not parsed and an error after its deepest part
//!       is not seen), a `judges`, `reviews`, `audits` or `wraps` edge whose
//!       `target_content_hash` is absent or null (§7.1 makes it REQUIRED), a
//!       `lineage[]` entry whose `type` or `target_trace_id` is not a
//!       non-empty string (absent, null, empty or of another JSON type; each
//!       in either mode, with or without `--strict`), or, with `--strict`,
//!       anything left unverified
//! - `2` a check could not run (missing `content_hash` in single mode, parse
//!       failure, duplicate ids, unresolved placeholder or malformed hash in
//!       lineage[].target_content_hash, `--strict` without `--file` or
//!       `--dir`, I/O, a trace of more than 256 MiB)
//!
//! A trace containing a number outside the JCS-safe range [−(2^53−1),
//! 2^53−1] by value, in any notation (an integer literal, `1e16`,
//! `9007199254740992.0`, an overflow such as `1e999`), is not conformant
//! (minitrace-spec v0.3.0 §3.4): that is reported on its own line whatever
//! the hash verdict, and exits 1, because a hash over f64-quantised values
//! cannot pin such numbers (single or set mode). When such a trace's hash
//! also mismatches, the output adds a note pointing at §14.3 — the
//! operational realisation of §14.3's "consumers SHOULD NOT treat a
//! `content_hash` mismatch on traces carrying out-of-range integers as
//! evidence of tampering without first ruling out verifier-implementation
//! divergence".
//!
//! Out of scope: resolving `${trace:<id>:content_hash}` placeholders. Those
//! are a producer-side authoring construct that `cross_trace.py` resolves
//! before stamping; published traces do not carry them. Per DSL spec RC 3
//! §13.1 the only placeholder-bearing field is `lineage[].target_content_hash`:
//! an unresolved placeholder there, or any value that is neither null nor a
//! well-formed hash, exits 2. Placeholder-shaped text in any other field is
//! content and is not inspected.

use std::collections::HashMap;
use std::error::Error;
use std::fs;
use std::io::{self, Read, Write};
use std::process::ExitCode;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// JCS-safe integer bounds (RFC 8785 / ECMA-404 IEEE 754 binary64).
const I53_MIN: i128 = -((1i128 << 53) - 1);
const I53_MAX: i128 = (1i128 << 53) - 1;

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            let _ = writeln!(io::stderr(), "verify: {e}");
            ExitCode::from(2)
        }
    }
}

fn run() -> Result<u8, Box<dyn Error>> {
    let mut files: Vec<String> = Vec::new();
    let mut dir: Option<String> = None;
    let mut strict = false;
    let mut iter = std::env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--file" => {
                files.push(
                    iter.next().ok_or("--file requires a path argument")?,
                );
            }
            "--dir" => {
                if dir.is_some() {
                    return Err("--dir given more than once".into());
                }
                dir = Some(iter.next().ok_or("--dir requires a path argument")?);
            }
            "--strict" => strict = true,
            "-h" | "--help" => {
                print_help();
                return Ok(0);
            }
            other => return Err(format!("unknown argument: {}", sanitise(other)).into()),
        }
    }

    if let Some(d) = &dir {
        files.extend(read_trace_dir(d)?);
    }

    // Mode selection. Zero files + no dir => read a single trace from stdin
    // (back-compat). Exactly one trace => single-trace mode. More => set mode.
    let stdout = io::stdout();
    let mut out = stdout.lock();
    if files.is_empty() {
        if strict {
            // Single-trace mode checks no lineage referent; --strict cannot be
            // honoured on one trace read from stdin, so it is refused.
            return Err("--strict needs --file or --dir (stdin is single-trace mode)".into());
        }
        let s = read_bounded(io::stdin(), "<stdin>", MAX_TRACE_BYTES)?;
        return verify_single(&s, "<stdin>", &mut out);
    }
    // --dir and --strict always take set mode: a one-trace directory must not
    // drop to single mode, which checks no lineage referent and ignores --strict.
    if files.len() == 1 && dir.is_none() && !strict {
        let path = &files[0];
        let input = read_trace_file(path)?;
        return verify_single(&input, path, &mut out);
    }
    verify_set_opts(&files, strict, &mut out)
}

/// The most bytes one trace may hold, as verify_envelope's MAX_TRACE_BYTES
/// (256 MiB): a larger input is refused (exit 2) instead of being read
/// whole into memory (ruled 2026-10-29).
const MAX_TRACE_BYTES: u64 = 256 * 1024 * 1024;

/// Read at most `limit` bytes of UTF-8 from `r`; more is an error naming
/// `name`, so a growing file or an endless stream is not read whole.
fn read_bounded(r: impl Read, name: &str, limit: u64) -> Result<String, Box<dyn Error>> {
    let mut buf = Vec::new();
    r.take(limit + 1).read_to_end(&mut buf).map_err(|e| format!("reading {name}: {e}"))?;
    if buf.len() as u64 > limit {
        return Err(format!("{name}: more than {limit} bytes, the limit for one trace; not checked").into());
    }
    String::from_utf8(buf).map_err(|_| format!("reading {name}: stream did not contain valid UTF-8").into())
}

/// One trace file, refused by its size before any of it is read when the
/// file system states one over the limit.
fn read_trace_file(path: &str) -> Result<String, Box<dyn Error>> {
    let name = sanitise(path);
    let f = fs::File::open(path).map_err(|e| format!("reading {name}: {e}"))?;
    if let Ok(m) = f.metadata() {
        if m.is_file() && m.len() > MAX_TRACE_BYTES {
            return Err(format!("{name}: {} bytes, more than {MAX_TRACE_BYTES}, the limit for one trace; not checked",
                               m.len()).into());
        }
    }
    read_bounded(f, &name, MAX_TRACE_BYTES)
}

/// Load every `*.minitrace.json` file in `dir`, sorted by name for
/// deterministic output. Non-recursive.
fn read_trace_dir(dir: &str) -> Result<Vec<String>, Box<dyn Error>> {
    let mut out: Vec<String> = Vec::new();
    for entry in fs::read_dir(dir).map_err(|e| format!("reading dir {}: {e}", sanitise(dir)))? {
        let entry = entry?;
        let path = entry.path();
        if path.is_file()
            && path
                .file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.ends_with(".minitrace.json"))
                .unwrap_or(false)
        {
            out.push(path.to_string_lossy().into_owned());
        }
    }
    if out.is_empty() {
        return Err(format!("no *.minitrace.json files under {}", sanitise(dir)).into());
    }
    out.sort();
    Ok(out)
}

// ---------------------------------------------------------------------------
// Single-trace mode (output preserved byte-identical for back-compat).
// ---------------------------------------------------------------------------

/// RFC 8785 canonicalises I-JSON, which forbids duplicate member names.
/// serde_json keeps the LAST duplicate silently, so a key inserted before the
/// real one (`"content": "INJECTED", "content": "original"`) would leave the
/// hash intact while a first-wins reader, grep or a person sees the insertion.
/// Walk the document once and refuse any object with a repeated name, and
/// any string or member name holding a Unicode noncharacter (I-JSON, §3.4;
/// serde_json already refuses an unpaired surrogate escape, and a Rust string
/// cannot hold one).
struct NoDuplicateKeys;

/// U+FDD0..U+FDEF, and U+xFFFE and U+xFFFF in every plane (66 code points).
fn is_noncharacter(c: char) -> bool {
    let cp = c as u32;
    (0xFDD0..=0xFDEF).contains(&cp) || (cp & 0xFFFE) == 0xFFFE
}

fn noncharacter_in<E: de::Error>(s: &str, what: &str) -> Result<(), E> {
    match s.chars().find(|&c| is_noncharacter(c)) {
        Some(c) => Err(E::custom(format!(
            "noncharacter U+{:04X} in a {what} (not I-JSON; refused)", c as u32))),
        None => Ok(()),
    }
}

impl<'de> Deserialize<'de> for NoDuplicateKeys {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(NoDupVisitor)
    }
}

struct NoDupVisitor;

impl<'de> Visitor<'de> for NoDupVisitor {
    type Value = NoDuplicateKeys;
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("any JSON value")
    }
    fn visit_bool<E>(self, _: bool) -> Result<Self::Value, E> { Ok(NoDuplicateKeys) }
    fn visit_i64<E>(self, _: i64) -> Result<Self::Value, E> { Ok(NoDuplicateKeys) }
    fn visit_u64<E>(self, _: u64) -> Result<Self::Value, E> { Ok(NoDuplicateKeys) }
    fn visit_f64<E>(self, _: f64) -> Result<Self::Value, E> { Ok(NoDuplicateKeys) }
    fn visit_str<E: de::Error>(self, s: &str) -> Result<Self::Value, E> {
        noncharacter_in(s, "string").map(|_| NoDuplicateKeys)
    }
    fn visit_unit<E>(self) -> Result<Self::Value, E> { Ok(NoDuplicateKeys) }
    fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
        while a.next_element::<NoDuplicateKeys>()?.is_some() {}
        Ok(NoDuplicateKeys)
    }
    fn visit_map<A: MapAccess<'de>>(self, mut m: A) -> Result<Self::Value, A::Error> {
        let mut seen = std::collections::HashSet::new();
        while let Some(k) = m.next_key::<String>()? {
            noncharacter_in::<A::Error>(&k, "member name")?;
            if !seen.insert(k.clone()) {
                return Err(de::Error::custom(format!(
                    "duplicate object key {} (not I-JSON; refused)", sanitise(&k))));
            }
            m.next_value::<NoDuplicateKeys>()?;
        }
        Ok(NoDuplicateKeys)
    }
}

fn parse_trace(raw: &str) -> Result<Value, Box<dyn Error>> {
    serde_json::from_str::<NoDuplicateKeys>(raw)?;
    Ok(serde_json::from_str(raw)?)
}

fn verify_single(input: &str, _name: &str, out: &mut impl Write) -> Result<u8, Box<dyn Error>> {
    let has_bigint = detect_out_of_domain_number_in_raw(input);
    let too_deep = depth_exceeds_in_raw(input, MAX_TRACE_DEPTH);
    let trace: Value = match parse_trace(input) {
        Ok(v) => v,
        Err(e) if too_deep && is_recursion_limit(&*e) => {
            write_depth_violation(out, false)?;
            return Ok(1);
        }
        Err(e) => return Err(e),
    };

    let stored = match &trace {
        Value::Object(obj) => match obj.get("content_hash") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Null) | None => {
                return Err(
                    "trace has no `content_hash` field (or it is null); \
                     a verifiable trace must carry a stored hash"
                        .into(),
                );
            }
            Some(other) => {
                return Err(format!(
                    "`content_hash` is not a string (got {})",
                    type_name(other)
                )
                .into());
            }
        },
        _ => return Err("trace is not a JSON object".into()),
    };

    if let Some(problem) = lineage_hash_problem(&trace) {
        return Err(problem.into());
    }

    let unhashed = integrity_edges_without_hash(&trace);
    let recomputed = recompute_hash(&trace)?;
    let matched = recomputed == stored;
    if matched {
        writeln!(out, "verified: {recomputed}")?;
        if has_bigint {
            write_numeric_domain_violation(out)?;
        }
    } else {
        writeln!(out, "mismatch:")?;
        writeln!(out, "  stored:     {}", hash_for_display(&stored))?;
        writeln!(out, "  recomputed: {recomputed}")?;
        if has_bigint {
            write_bigint_note(out)?;
            write_numeric_domain_violation(out)?;
        }
    }
    if too_deep {
        write_depth_violation(out, true)?;
    }
    for &(i, ty) in &unhashed {
        write_integrity_edge_without_hash(out, i, ty)?;
    }
    let shape = lineage_shape_failures(&trace);
    for msg in &shape {
        writeln!(out, "{msg}")?;
    }
    Ok(if matched && !has_bigint && !too_deep && unhashed.is_empty() && shape.is_empty() { 0 } else { 1 })
}

// ---------------------------------------------------------------------------
// Set / edge-consistency mode.
// ---------------------------------------------------------------------------

struct Loaded {
    name: String,
    raw: String,
    value: Value,
}

/// One cross-trace integrity edge: a `lineage[]` entry carrying a resolved
/// `target_content_hash`.
struct Edge {
    target_id: String,
    target_hash: String,
}

#[cfg(test)]
fn verify_set(files: &[String], out: &mut impl Write) -> Result<u8, Box<dyn Error>> {
    verify_set_opts(files, false, out)
}

/// `strict`: a set passes only if everything in it was actually verified.
/// Without it (the default, spec §14.3), a trace with a null content_hash, an
/// edge to a trace outside the set and an edge to another state of its
/// referent are reported but do not fail; a set of unhashed traces then
/// reports `ok` although nothing was checked. An entry
/// `lineage_shape_failures` reports fails in either case.
fn verify_set_opts(files: &[String], strict: bool, out: &mut impl Write) -> Result<u8, Box<dyn Error>> {
    // Load + parse all traces first; any load/parse failure is exit 2.
    // A trace the parser cannot read and that nests past the §3.4 depth is
    // one problem of the set, not a set that cannot be checked.
    let mut loaded: Vec<Loaded> = Vec::with_capacity(files.len());
    let mut unparsed_too_deep: Vec<String> = Vec::new();
    for path in files {
        let raw = read_trace_file(path)?;
        let value: Value = match parse_trace(&raw) {
            Ok(v) => v,
            Err(e) if is_recursion_limit(&*e) && depth_exceeds_in_raw(&raw, MAX_TRACE_DEPTH) => {
                unparsed_too_deep.push(display_name(path));
                continue;
            }
            Err(e) => return Err(format!("parsing {}: {e}", sanitise(path)).into()),
        };
        if !value.is_object() {
            return Err(format!("{}: trace is not a JSON object", sanitise(path)).into());
        }
        if let Some(problem) = lineage_hash_problem(&value) {
            return Err(format!("{}: {}", sanitise(path), problem).into());
        }
        loaded.push(Loaded {
            name: display_name(path),
            raw,
            value,
        });
    }

    // Index by id; duplicate ids cannot be disambiguated (exit 2).
    let mut by_id: HashMap<String, usize> = HashMap::new();
    for (i, lt) in loaded.iter().enumerate() {
        if let Some(Value::String(id)) = lt.value.get("id") {
            if let Some(&prev) = by_id.get(id) {
                return Err(format!(
                    "duplicate trace id `{}` in {} and {}; \
                     cannot resolve cross-trace references",
                    sanitise(id),
                    loaded[prev].name,
                    lt.name
                )
                .into());
            }
            by_id.insert(id.clone(), i);
        }
    }

    writeln!(
        out,
        "minitrace-verify: set mode ({} traces)",
        loaded.len() + unparsed_too_deep.len()
    )?;

    let mut problems = 0usize;
    let mut verified = 0usize;
    let mut edges_ok = 0usize;
    let mut edges_external = 0usize;
    let mut edges_other = 0usize;

    for name in &unparsed_too_deep {
        problems += 1;
        write!(out, "  {name}: ")?;
        write_depth_violation(out, false)?;
    }

    // Pass 1: self-verify each trace that carries a non-null content_hash.
    for lt in &loaded {
        match stored_hash(&lt.value) {
            StoredHash::Present(stored) => {
                let recomputed = recompute_hash(&lt.value)?;
                if recomputed == stored {
                    verified += 1;
                    writeln!(out, "  {}: verified {recomputed}", lt.name)?;
                } else {
                    problems += 1;
                    writeln!(out, "  {}: SELF-HASH MISMATCH", lt.name)?;
                    writeln!(out, "      stored:     {}", hash_for_display(&stored))?;
                    writeln!(out, "      recomputed: {recomputed}")?;
                    if detect_out_of_domain_number_in_raw(&lt.raw) {
                        write_bigint_note(out)?;
                    }
                }
            }
            StoredHash::Null => {
                writeln!(out, "  {}: content_hash null (not self-verified)", lt.name)?;
                if strict {
                    problems += 1;
                    writeln!(out, "      --strict: an unhashed trace fails the set")?;
                }
            }
            StoredHash::NotString(t) => {
                problems += 1;
                writeln!(
                    out,
                    "  {}: content_hash is not a string (got {t})",
                    lt.name
                )?;
            }
        }
        // §3.4 numeric domain: conformance is checked whatever the hash state,
        // and reported apart from it.
        if detect_out_of_domain_number_in_raw(&lt.raw) {
            problems += 1;
            write!(out, "  {}: ", lt.name)?;
            write_numeric_domain_violation(out)?;
        }
        if depth_exceeds_in_raw(&lt.raw, MAX_TRACE_DEPTH) {
            problems += 1;
            write!(out, "  {}: ", lt.name)?;
            write_depth_violation(out, true)?;
        }
        // §7.1: an integrity edge without its hash fails whatever --strict says.
        for (i, ty) in integrity_edges_without_hash(&lt.value) {
            problems += 1;
            write!(out, "  {}: ", lt.name)?;
            write_integrity_edge_without_hash(out, i, ty)?;
        }
        // An edge that cannot be read fails whatever --strict says.
        for msg in lineage_shape_failures(&lt.value) {
            problems += 1;
            writeln!(out, "  {}: {msg}", lt.name)?;
        }
    }

    // Pass 2: cross-trace edge consistency (§7.1).
    for lt in &loaded {
        for edge in edges_of(&lt.value) {
            match by_id.get(&edge.target_id) {
                None => {
                    edges_external += 1;
                    writeln!(
                        out,
                        "  edge {} -> {} (external referent, not in set): not verified",
                        lt.name,
                        sanitise(&edge.target_id)
                    )?;
                    if strict {
                        problems += 1;
                    }
                }
                Some(&ri) => {
                    let referent = &loaded[ri];
                    match stored_hash(&referent.value) {
                        StoredHash::Present(ref_hash) if ref_hash == edge.target_hash => {
                            edges_ok += 1;
                            writeln!(
                                out,
                                "  edge {} -> {}: consistent ({})",
                                lt.name,
                                referent.name,
                                hash_for_display(&edge.target_hash)
                            )?;
                        }
                        StoredHash::Present(ref_hash) => {
                            // trace §7.1, envelope §6.3 (ruled 2026-10-29): the
                            // edge refers to another state of the referent (a
                            // permitted later change cannot be told from an
                            // error), so it is not verified against it and is no
                            // failed check; with --strict it is left unverified.
                            edges_other += 1;
                            if strict {
                                problems += 1;
                            }
                            writeln!(
                                out,
                                "  edge {} -> {}: refers to another state of the referent, not verified (trace §7.1)",
                                lt.name, referent.name
                            )?;
                            writeln!(out, "      edge target:     {}",
                                     hash_for_display(&edge.target_hash))?;
                            writeln!(out, "      referent stored: {}",
                                     hash_for_display(&ref_hash))?;
                        }
                        StoredHash::Null | StoredHash::NotString(_) => {
                            // §7.1: an edge requiring integrity MUST
                            // NOT reference a trace with no content_hash.
                            problems += 1;
                            writeln!(out, "  edge {} -> {}: REFERENT HAS NO HASH", lt.name, referent.name)?;
                            writeln!(out, "      edge target: {}",
                                     hash_for_display(&edge.target_hash))?;
                            writeln!(out, "      referent {} carries no content_hash (§7.1)", referent.name)?;
                        }
                    }
                }
            }
        }
    }

    writeln!(
        out,
        "{}: {verified} self-verified, {edges_ok} edges consistent, \
         {edges_external} external edges, {edges_other} edges to another state, {problems} problems",
        if problems == 0 { "ok" } else { "FAIL" }
    )?;

    Ok(if problems == 0 { 0 } else { 1 })
}

enum StoredHash {
    Present(String),
    Null,
    NotString(&'static str),
}

fn stored_hash(trace: &Value) -> StoredHash {
    match trace.get("content_hash") {
        Some(Value::String(s)) => StoredHash::Present(s.clone()),
        Some(Value::Null) | None => StoredHash::Null,
        Some(other) => StoredHash::NotString(type_name(other)),
    }
}

/// Edge types whose target `content_hash` trace §7.1 makes REQUIRED at
/// reference time.
const INTEGRITY_EDGE_TYPES: [&str; 4] = ["judges", "reviews", "audits", "wraps"];

/// The two members every `lineage[]` entry carries as a non-empty string
/// (the base schema requires both on every edge).
const LINEAGE_REQUIRED: [&str; 2] = ["type", "target_trace_id"];

/// `true` when `e` carries both required members as non-empty strings.
fn lineage_entry_readable(e: &Value) -> bool {
    LINEAGE_REQUIRED
        .iter()
        .all(|k| matches!(e.get(*k), Some(Value::String(s)) if !s.is_empty()))
}

/// `lineage[]` entries this verifier cannot read as edges, as messages: a
/// `type` or `target_trace_id` that is absent, null, the empty string or of
/// another JSON type. Each is a failed check in either mode, with or without
/// `--strict`; such an entry is not also checked against a referent.
fn lineage_shape_failures(trace: &Value) -> Vec<String> {
    let mut out = Vec::new();
    let Some(Value::Array(lineage)) = trace.get("lineage") else {
        return out;
    };
    for (i, e) in lineage.iter().enumerate() {
        for key in LINEAGE_REQUIRED {
            let what = match e.get(key) {
                Some(Value::String(s)) if !s.is_empty() => continue,
                Some(Value::String(_)) => "the empty string".to_string(),
                None => "absent".to_string(),
                Some(other) => format!("not a string (got {})", type_name(other)),
            };
            out.push(format!("lineage[{i}].{key} is {what}; a non-empty string is required"));
        }
    }
    out
}

/// `lineage[]` entries of an integrity type (§7.1) whose `target_content_hash`
/// is absent or null, as (index, type). Such an edge is not conformant, so it
/// is a failed check in either mode, not a discovery-only edge. A value that
/// is present but not a string is refused earlier by `lineage_hash_problem`.
fn integrity_edges_without_hash(trace: &Value) -> Vec<(usize, &'static str)> {
    let mut out = Vec::new();
    if let Some(Value::Array(lineage)) = trace.get("lineage") {
        for (i, e) in lineage.iter().enumerate() {
            let ty = match e.get("type") {
                Some(Value::String(t)) => t.as_str(),
                _ => continue,
            };
            if let Some(&known) = INTEGRITY_EDGE_TYPES.iter().find(|&&k| k == ty) {
                if matches!(e.get("target_content_hash"), None | Some(Value::Null)) {
                    out.push((i, known));
                }
            }
        }
    }
    out
}

fn write_integrity_edge_without_hash(out: &mut impl Write, i: usize, ty: &str) -> io::Result<()> {
    writeln!(out, "lineage[{i}]: {ty} edge without target_content_hash (REQUIRED, trace §7.1)")
}

/// Extract cross-trace integrity edges: `lineage[]` entries carrying a
/// resolved (non-placeholder) `target_content_hash` and a `target_trace_id`.
/// Other edges are skipped here: one of a type that needs no hash is
/// discovery-only; one of an integrity type without a hash is reported by
/// `integrity_edges_without_hash`; one without a non-empty string `type` and
/// `target_trace_id` is reported by `lineage_shape_failures` and is not also
/// checked against a referent.
fn edges_of(trace: &Value) -> Vec<Edge> {
    let mut out = Vec::new();
    if let Some(Value::Array(lineage)) = trace.get("lineage") {
        for e in lineage {
            if !lineage_entry_readable(e) {
                continue; // cannot be read as an edge; a failed check above
            }
            let target_hash = match e.get("target_content_hash") {
                Some(Value::String(s)) => s.clone(),
                _ => continue, // discovery-only edge
            };
            let target_id = match e.get("target_trace_id") {
                Some(Value::String(s)) => s.clone(),
                _ => continue,
            };
            out.push(Edge {
                target_id,
                target_hash,
            });
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Output sanitisation.
//
// Every value this tool prints that came from a trace, a lineage edge, or a
// filename is attacker-controlled. `content_hash` in particular is read as an
// unvalidated string and printed on the mismatch path, so a crafted value could
// emit CR and CSI sequences that erase the verdict line and repaint it as a
// green `verified:` while the tool correctly exits 1. The exit code was never
// wrong; the human reading the terminal was the target.
//
// Two defences, because they fail differently. Hashes are validated against
// their own syntax and a value that is not a hash is never printed as one.
// Everything else is escaped and bounded, so a field with no fixed syntax still
// cannot move the cursor or forge a line.
// ---------------------------------------------------------------------------

/// Longest untrusted run echoed into a message. A well-formed hash is 71
/// characters, so this leaves room to recognise a near-miss without letting a
/// megabyte-long field scroll the real output off the screen.
const MAX_ECHO_CHARS: usize = 80;

/// `true` for characters that must never reach a terminal verbatim: C0 and C1
/// controls and DEL (via `is_control`), plus the bidirectional formatting
/// characters, which reorder text without being controls.
fn needs_escape(c: char) -> bool {
    c.is_control()
        || matches!(c,
            '\u{200E}' | '\u{200F}'      // LRM, RLM
            | '\u{202A}'..='\u{202E}'    // embeddings and overrides
            | '\u{2066}'..='\u{2069}'    // isolates
            | '\u{200B}'..='\u{200D}'    // zero-width space, non-joiner, joiner
            | '\u{2060}'..='\u{2065}'    // word joiner, invisible operators
            | '\u{FEFF}'                   // zero-width no-break space / BOM
            | '\u{2028}' | '\u{2029}'      // line and paragraph separators: a reader
                                           // splitting as str.splitlines() does starts a line
        )
}

/// Escape control and bidi characters, and bound the length.
fn sanitise(s: &str) -> String {
    let total = s.chars().count();
    let mut out = String::new();
    for (n, c) in s.chars().enumerate() {
        if n >= MAX_ECHO_CHARS {
            out.push_str(&format!("... [truncated, {total} chars]"));
            break;
        }
        if needs_escape(c) {
            out.push_str(&format!("\\u{{{:04x}}}", c as u32));
        } else {
            out.push(c);
        }
    }
    out
}

/// `true` for exactly the `content_hash` syntax of minitrace-spec v0.3.0 §3.4:
/// `sha256:` followed by 64 lowercase hex digits.
fn is_wellformed_hash(s: &str) -> bool {
    match s.strip_prefix("sha256:") {
        Some(hex) => {
            hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        }
        None => false,
    }
}

/// Render a stored hash for output. A well-formed hash is printed unchanged, so
/// output on every valid trace is byte-identical to previous releases. Anything
/// else is labelled as malformed and escaped, which is both the safe rendering
/// and the more informative one: a value that is not a hash cannot have been
/// produced by a conforming producer.
fn hash_for_display(s: &str) -> String {
    if is_wellformed_hash(s) {
        s.to_string()
    } else {
        format!("<malformed content_hash: {}>", sanitise(s))
    }
}

// ---------------------------------------------------------------------------
// Shared helpers.
// ---------------------------------------------------------------------------

/// Strip `content_hash` and recompute the JCS+SHA-256 digest. Mirrors
/// `cross_trace.py`'s `canonical_body`: the hash covers the trace excluding
/// the hash field itself.
fn recompute_hash(trace: &Value) -> Result<String, Box<dyn Error>> {
    let mut body = trace.clone();
    if let Value::Object(ref mut obj) = body {
        obj.remove("content_hash");
    }
    let canonical = serde_json_canonicalizer::to_vec(&body)?;
    let digest = Sha256::digest(&canonical);
    Ok(format!("sha256:{digest:x}"))
}

/// §3.4 numeric domain: a number whose binary64 value lies outside
/// [-(2^53-1), 2^53-1], in any notation, makes the trace not conformant. Reported apart from the hash verdict, because a
/// hash computed over f64-quantised values can still match (9007199254740993
/// and 9007199254740992 hash alike), so "verified" alone would overstate it.
fn write_numeric_domain_violation(out: &mut impl Write) -> io::Result<()> {
    writeln!(
        out,
        "not conformant: number outside [-(2^53-1), 2^53-1] by value; minitrace-spec"
    )?;
    writeln!(
        out,
        "        v0.3.0 §3.4 requires such numbers as strings (the hash does not pin their values)."
    )
}

/// §3.4 nesting depth: a trace may nest arrays and objects 64 deep (the trace
/// object itself counts 1, `[]` is depth 1).
const MAX_TRACE_DEPTH: usize = 64;

/// The §3.4 depth limit, judged on the raw text (ruled 2026-10-28): one
/// string-aware pass without recursion, so a trace too deep for the parser
/// (serde_json stops at 128) is still not conformant, as the Python
/// references read it, rather than a parse failure.
fn depth_exceeds_in_raw(input: &str, limit: usize) -> bool {
    let mut depth = 0usize;
    let (mut in_string, mut escape) = (false, false);
    for &c in input.as_bytes() {
        if in_string {
            if escape {
                escape = false;
            } else if c == b'\\' {
                escape = true;
            } else if c == b'"' {
                in_string = false;
            }
            continue;
        }
        match c {
            b'"' => in_string = true,
            b'[' | b'{' => {
                depth += 1;
                if depth > limit {
                    return true;
                }
            }
            b']' | b'}' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    false
}

/// The one parse failure the depth scan stands in for: serde_json's own
/// recursion limit (128). Any other failure (a duplicate key, a lone
/// surrogate, a non-finite number, truncation, trailing text) keeps its own
/// message and exit 2 when the parser meets it first. serde stops at the
/// first error in text order, so in a trace past the limit a failure that
/// lies after the deep part is not seen, and the trace is reported for its
/// depth (exit 1), where the Python references name the other failure
/// (ruled 2026-10-28: named, not fixed before the v0.3.0 cut).
fn is_recursion_limit(e: &(dyn Error + 'static)) -> bool {
    e.downcast_ref::<serde_json::Error>()
        .is_some_and(|e| e.to_string().starts_with("recursion limit exceeded"))
}

/// Reported apart from the hash verdict, as the numeric domain is. A trace
/// the parser could not read for its depth has no hash verdict.
fn write_depth_violation(out: &mut impl Write, parsed: bool) -> io::Result<()> {
    writeln!(
        out,
        "not conformant: nested deeper than {MAX_TRACE_DEPTH} levels; minitrace-spec"
    )?;
    writeln!(
        out,
        "        v0.3.0 §3.4 limits a trace's depth to {MAX_TRACE_DEPTH}{}.",
        if parsed { "" } else { " (not parsed; hash not checked)" }
    )
}

fn write_bigint_note(out: &mut impl Write) -> io::Result<()> {
    writeln!(
        out,
        "  note: trace contains numbers outside [-(2^53-1), 2^53-1];"
    )?;
    writeln!(
        out,
        "        mismatch may be verifier-implementation divergence"
    )?;
    writeln!(out, "        per minitrace-spec v0.3.0 §14.3.")
}

fn placeholder_error() -> String {
    "unresolved cross-trace placeholder `${trace:<id>:<field>}`; this CLI does \
     not resolve placeholders (they are producer-side — see adapters/cross_trace.py)"
        .to_string()
}

/// Short label for a trace in set-mode output. Sanitised at construction so
/// every later use is safe by default: filenames come from `--dir`, which reads
/// whatever the directory happens to contain, and a filename is as
/// attacker-controlled as a trace field when the attacker can write there.
fn display_name(path: &str) -> String {
    let raw = std::path::Path::new(path)
        .file_name()
        .and_then(|n| n.to_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| path.to_string());
    sanitise(&raw)
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// Check the placeholder-bearing field (DSL spec RC 3 §13.1, §13.4): every
/// non-null `lineage[].target_content_hash` must be a well-formed hash. A
/// whole-value placeholder there is unresolved; any other value is malformed.
/// Nothing outside that field is inspected: placeholder-shaped text anywhere
/// else is content (a session that discusses the format quotes the syntax).
fn lineage_hash_problem(trace: &Value) -> Option<String> {
    // A present `lineage` must be an array of edge objects. Anything else would
    // hide the bearing field from this check (the verifier does not
    // schema-validate), so it is rejected rather than skipped.
    let lineage = match trace.get("lineage") {
        None | Some(Value::Null) => return None,
        Some(Value::Array(a)) => a,
        Some(other) => {
            return Some(format!("lineage is not an array (got {})", type_name(other)))
        }
    };
    for (i, edge) in lineage.iter().enumerate() {
        if !edge.is_object() {
            return Some(format!("lineage[{i}] is not an object (got {})", type_name(edge)));
        }
        match edge.get("target_content_hash") {
            None | Some(Value::Null) => {}
            Some(Value::String(s)) if is_wellformed_hash(s) => {}
            Some(Value::String(s)) if is_placeholder(s) => {
                return Some(format!("lineage[{i}].target_content_hash: {}", placeholder_error()))
            }
            Some(Value::String(s)) => {
                return Some(format!(
                    "lineage[{i}].target_content_hash is neither a well-formed hash nor a \
                     placeholder: {}",
                    hash_for_display(s)
                ))
            }
            Some(other) => {
                return Some(format!(
                    "lineage[{i}].target_content_hash is not a string (got {})",
                    type_name(other)
                ))
            }
        }
    }
    None
}

/// `true` iff the whole value is a cross-trace placeholder, grammar
/// `^\$\{trace:(.+):([a-z_]+)\}$` (DSL spec RC 3 §13.1): the id runs to the
/// last `:`, so ids may contain `:` and `}`; the field is lowercase and `_`.
/// Mirrors `is_placeholder` in adapters/cross_trace.py.
fn is_placeholder(s: &str) -> bool {
    let body = match s.strip_prefix("${trace:").and_then(|r| r.strip_suffix('}')) {
        Some(b) => b,
        None => return false,
    };
    match body.rfind(':') {
        Some(k) => {
            let (id, field) = (&body[..k], &body[k + 1..]);
            !id.is_empty()
                && !field.is_empty()
                && field.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
        }
        None => false,
    }
}

/// Scan the raw JSON input for a number outside [I53_MIN, I53_MAX] by value
/// (minitrace-spec §3.4: decided by value, not spelling). An integer token is
/// range-checked exactly; any other token is read as binary64 and flagged
/// when it is not finite (an overflow such as 1e999) or beyond the range
/// (1e16, 9007199254740992.0).
///
/// Walks bytes once, tracking string boundaries so number-shaped substrings
/// inside string literals are skipped. Returns on the first out-of-range
/// number token found.
///
/// This runs BEFORE `serde_json::from_str`. With `serde_json`'s default
/// features (no `arbitrary_precision`), bigint tokens are quantised to f64
/// on parse and the lexical evidence is lost — by which point we cannot
/// distinguish "trace contained 9007199254740993" from "trace contained
/// 9007199254740992". Detecting on raw bytes is the only reliable place.
fn detect_out_of_domain_number_in_raw(input: &str) -> bool {
    let bytes = input.as_bytes();
    let mut i = 0;
    let mut in_string = false;
    let mut escape = false;
    while i < bytes.len() {
        let c = bytes[i];
        if in_string {
            if escape {
                escape = false;
            } else if c == b'\\' {
                escape = true;
            } else if c == b'"' {
                in_string = false;
            }
            i += 1;
            continue;
        }
        if c == b'"' {
            in_string = true;
            i += 1;
            continue;
        }
        let starts_number = c.is_ascii_digit()
            || (c == b'-'
                && i + 1 < bytes.len()
                && bytes[i + 1].is_ascii_digit());
        if !starts_number {
            i += 1;
            continue;
        }
        // Walk the number token. JSON number grammar:
        //   int = "-"? ("0" | [1-9][0-9]*)
        //   frac = "." [0-9]+
        //   exp = [eE] [-+]? [0-9]+
        let start = i;
        if c == b'-' {
            i += 1;
        }
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            i += 1;
        }
        let int_end = i;
        let mut is_float = false;
        if i < bytes.len() && bytes[i] == b'.' {
            is_float = true;
            i += 1;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
        }
        if i < bytes.len() && (bytes[i] == b'e' || bytes[i] == b'E') {
            is_float = true;
            i += 1;
            if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
                i += 1;
            }
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
        }
        if is_float {
            // SAFETY: bytes[start..i] is ASCII (digits, '-', '.', 'e', 'E', '+').
            let lexeme = std::str::from_utf8(&bytes[start..i]).expect("ascii");
            match lexeme.parse::<f64>() {
                Ok(f) => {
                    if !f.is_finite() || f.abs() > I53_MAX as f64 {
                        return true;
                    }
                }
                Err(_) => return true,
            }
        } else {
            // Pure integer token. Parse to i128 and bounds-check.
            // SAFETY: bytes[start..int_end] is ASCII (digits and optional '-').
            let int_lexeme =
                std::str::from_utf8(&bytes[start..int_end]).expect("ascii");
            match int_lexeme.parse::<i128>() {
                Ok(big) => {
                    if big < I53_MIN || big > I53_MAX {
                        return true;
                    }
                }
                // Token too large for i128 — definitely out of JCS range.
                Err(_) => return true,
            }
        }
    }
    false
}

fn print_help() {
    println!(
        "minitrace-verify — trace-aware verifier for minitrace v0.3.0 §3.4 content_hash\n\
         \n\
         Single-trace mode (one --file without --strict, or stdin): recompute the trace's\n\
         content_hash (JCS + SHA-256) and compare to the stored value.\n\
         \n\
         Set / edge-consistency mode (multiple --file, or --dir): self-verify\n\
         each stamped trace AND check every cross-trace lineage edge\n\
         (target_content_hash) against the referenced in-set trace (§7.1).\n\
         Traces with a null content_hash and edges to traces outside the set\n\
         are reported but do not fail the set, unless --strict is given.\n\
         \n\
         Usage:\n\
         \x20\x20minitrace-verify --file path/to/trace.minitrace.json\n\
         \x20\x20minitrace-verify < trace.minitrace.json\n\
         \x20\x20minitrace-verify --file a.minitrace.json --file b.minitrace.json\n\
         \x20\x20minitrace-verify --dir examples/v0.3.0\n\
         \x20\x20minitrace-verify --strict --dir out/   # fail unless everything verified\n\
         \n\
         Exit codes:\n\
         \x20\x200  all checks passed\n\
         \x20\x201  a check ran and found a problem (hash mismatch, a\n\
         \x20\x20   cross-trace edge naming a referent with no hash, a\n\
         \x20\x20   number outside the §3.4 domain, nesting deeper than\n\
         \x20\x20   §3.4's 64 levels, a judges, reviews, audits or wraps\n\
         \x20\x20   edge without target_content_hash (§7.1), a lineage\n\
         \x20\x20   entry whose type or target_trace_id is not a non-empty\n\
         \x20\x20   string (each in either mode), or with --strict anything\n\
         \x20\x20   left unverified)\n\
         \x20\x202  a check could not run (missing content_hash in single\n\
         \x20\x20   mode, parse failure, duplicate ids, unresolved\n\
         \x20\x20   cross-trace placeholders, --strict without --file or\n\
         \x20\x20   --dir, I/O, a trace of more than 256 MiB)\n\
         \x20\x20An edge whose hash differs from its in-set referent's refers\n\
         \x20\x20to another state of it (trace §7.1): reported, not verified,\n\
         \x20\x20a problem only with --strict.\n\
         \n\
         A trace containing a number outside [-(2^53-1), 2^53-1] by value,\n\
         in any notation, is reported \"not conformant\" (minitrace-spec\n\
         v0.3.0 §3.4) whatever its hash verdict; if its hash also\n\
         mismatches, a note points at minitrace-spec v0.3.0 §14.3.\n\
         \n\
         --strict requires --file or --dir.\n"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_bounded_refuses_more_than_the_limit() {
        assert_eq!(read_bounded(&b"abcd"[..], "x", 4).unwrap(), "abcd");
        let e = read_bounded(&b"abcde"[..], "x", 4).unwrap_err().to_string();
        assert!(e.contains("x: more than 4 bytes"), "{e}");
        let e = read_bounded(&[0xffu8][..], "x", 4).unwrap_err().to_string();
        assert!(e.contains("reading x: stream did not contain valid UTF-8"), "{e}");
        // A longer stream is not read whole: at most one byte past the limit.
        struct Counting<R> {
            inner: R,
            read: usize,
        }
        impl<R: Read> Read for Counting<R> {
            fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
                let n = self.inner.read(b)?;
                self.read += n;
                Ok(n)
            }
        }
        let mut c = Counting { inner: io::repeat(b'a').take(1 << 20), read: 0 };
        assert!(read_bounded(&mut c, "x", 4).is_err());
        assert!(c.read <= 5, "read {} bytes of a stream over the limit", c.read);
    }

    #[test]
    fn strict_set_fails_on_unverified_members() {
        let dir = std::env::temp_dir().join(format!("mv-strict-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let a = dir.join("a.minitrace.json");
        let b = dir.join("b.minitrace.json");
        std::fs::write(&a, r#"{"id":"a","content_hash":null}"#).unwrap();
        std::fs::write(&b, r#"{"id":"b","content_hash":null,"lineage":[{"type":"derived-from","target_trace_id":"ghost","target_content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]}"#).unwrap();
        let files = [a.to_string_lossy().into_owned(), b.to_string_lossy().into_owned()];
        assert_eq!(verify_set_opts(&files, false, &mut std::io::sink()).unwrap(), 0);
        let mut buf = Vec::new();
        assert_eq!(verify_set_opts(&files, true, &mut buf).unwrap(), 1);
        let s = String::from_utf8(buf).unwrap();
        // Two unhashed traces and one edge to a trace outside the set.
        assert!(s.contains("3 problems"), "{s}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn zero_width_characters_are_escaped() {
        let s = sanitise(&format!("a{}b{}c", char::from_u32(0x200B).unwrap(), char::from_u32(0xFEFF).unwrap()));
        assert_eq!(s, "a\\u{200b}b\\u{feff}c");
    }

    #[test]
    fn strict_single_trace_dir_takes_set_mode() {
        let dir = std::env::temp_dir().join(format!("mv-one-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let t = serde_json::json!({"id": "j", "lineage": [{"type":"derived-from","target_trace_id": "ghost",
            "target_content_hash": format!("sha256:{}", "a".repeat(64))}]});
        let mut t2 = t.clone();
        t2["content_hash"] = Value::String(recompute_hash(&t).unwrap());
        let f = dir.join("j.minitrace.json");
        std::fs::write(&f, t2.to_string()).unwrap();
        let files = [f.to_string_lossy().into_owned()];
        assert_eq!(verify_set_opts(&files, true, &mut std::io::sink()).unwrap(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn stamped(mut t: Value) -> Value {
        let h = recompute_hash(&t).unwrap();
        t["content_hash"] = Value::String(h);
        t
    }

    /// A stamped referent `a` and a stamped trace `b` with one edge to it.
    /// `hash`: None leaves target_content_hash out, Some(v) sets it to v.
    fn edge_set(dir: &std::path::Path, ty: &str, hash: Option<Value>) -> Vec<String> {
        std::fs::create_dir_all(dir).unwrap();
        let a = stamped(serde_json::json!({"id": "a", "turns": []}));
        let mut edge = serde_json::json!({"type": ty, "target_trace_id": "a"});
        if let Some(h) = hash {
            edge["target_content_hash"] = h;
        }
        let b = stamped(serde_json::json!({"id": "b", "lineage": [edge]}));
        let mut files = Vec::new();
        for (n, t) in [("a", &a), ("b", &b)] {
            let f = dir.join(format!("{n}.minitrace.json"));
            std::fs::write(&f, t.to_string()).unwrap();
            files.push(f.to_string_lossy().into_owned());
        }
        files
    }

    #[test]
    fn an_integrity_edge_without_its_hash_fails_in_both_modes() {
        // trace §7.1: judges, reviews, audits and wraps need the target's
        // content_hash; without it the edge is not conformant, not
        // discovery-only, with or without --strict.
        let base = std::env::temp_dir().join(format!("mv-int-{}", std::process::id()));
        for ty in ["judges", "reviews", "audits", "wraps"] {
            for (k, hash) in [("absent", None), ("null", Some(Value::Null))] {
                let dir = base.join(format!("{ty}-{k}"));
                let files = edge_set(&dir, ty, hash);
                let want = format!("lineage[0]: {ty} edge without target_content_hash (REQUIRED, trace §7.1)");
                for strict in [false, true] {
                    let mut buf = Vec::new();
                    assert_eq!(verify_set_opts(&files, strict, &mut buf).unwrap(), 1, "{ty} {k} strict={strict}");
                    let s = String::from_utf8(buf).unwrap();
                    assert!(s.contains(&format!("b.minitrace.json: {want}")), "{s}");
                    assert!(s.contains("1 problems"), "{s}");
                }
                let mut buf = Vec::new();
                let raw = std::fs::read_to_string(&files[1]).unwrap();
                assert_eq!(verify_single(&raw, "b", &mut buf).unwrap(), 1, "{ty} {k} single");
                let s = String::from_utf8(buf).unwrap();
                assert!(s.starts_with("verified: ") && s.contains(&want), "{s}");
            }
        }
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn a_lineage_entry_that_cannot_be_read_fails_in_every_mode() {
        // Every entry carries type and target_trace_id as non-empty strings
        // (the base schema requires both on every edge). Absent, null, the
        // empty string or another JSON type fails with or without --strict,
        // single or set, for an integrity edge and any other alike.
        let h = Value::String(format!("sha256:{}", "a".repeat(64)));
        let req = "; a non-empty string is required";
        let cases: Vec<(&str, Value, String)> = vec![
            ("judges tid null",
             serde_json::json!({"type": "judges", "target_content_hash": h, "target_trace_id": null}),
             format!("lineage[0].target_trace_id is not a string (got null){req}")),
            ("wraps tid absent",
             serde_json::json!({"type": "wraps", "target_content_hash": h}),
             format!("lineage[0].target_trace_id is absent{req}")),
            ("judges tid empty",
             serde_json::json!({"type": "judges", "target_content_hash": h, "target_trace_id": ""}),
             format!("lineage[0].target_trace_id is the empty string{req}")),
            ("audits tid number",
             serde_json::json!({"type": "audits", "target_content_hash": h, "target_trace_id": 123}),
             format!("lineage[0].target_trace_id is not a string (got number){req}")),
            ("derived-from tid null",
             serde_json::json!({"type": "derived-from", "target_trace_id": null}),
             format!("lineage[0].target_trace_id is not a string (got null){req}")),
            ("derived-from tid absent",
             serde_json::json!({"type": "derived-from"}),
             format!("lineage[0].target_trace_id is absent{req}")),
            ("derived-from tid number",
             serde_json::json!({"type": "derived-from", "target_trace_id": 456}),
             format!("lineage[0].target_trace_id is not a string (got number){req}")),
            ("type null",
             serde_json::json!({"type": null, "target_trace_id": "a"}),
             format!("lineage[0].type is not a string (got null){req}")),
            ("type absent",
             serde_json::json!({"target_trace_id": "a"}),
             format!("lineage[0].type is absent{req}")),
            ("type empty",
             serde_json::json!({"type": "", "target_trace_id": "a"}),
             format!("lineage[0].type is the empty string{req}")),
            ("type number",
             serde_json::json!({"type": 123, "target_trace_id": "a"}),
             format!("lineage[0].type is not a string (got number){req}")),
            ("type object",
             serde_json::json!({"type": {"k": "judges"}, "target_trace_id": "a"}),
             format!("lineage[0].type is not a string (got object){req}")),
        ];
        let base = std::env::temp_dir().join(format!("mv-shape-{}", std::process::id()));
        for (n, (label, edge, want)) in cases.iter().enumerate() {
            let dir = base.join(n.to_string());
            std::fs::create_dir_all(&dir).unwrap();
            let a = stamped(serde_json::json!({"id": "a", "turns": []}));
            let b = stamped(serde_json::json!({"id": "b", "lineage": [edge]}));
            let mut files = Vec::new();
            for (name, t) in [("a", &a), ("b", &b)] {
                let f = dir.join(format!("{name}.minitrace.json"));
                std::fs::write(&f, t.to_string()).unwrap();
                files.push(f.to_string_lossy().into_owned());
            }
            for strict in [false, true] {
                let mut buf = Vec::new();
                assert_eq!(verify_set_opts(&files, strict, &mut buf).unwrap(), 1, "{label} strict={strict}");
                let s = String::from_utf8(buf).unwrap();
                assert!(s.contains(&format!("b.minitrace.json: {want}")), "{label}: {s}");
                assert_eq!(s.matches(want.as_str()).count(), 1, "{label}: {s}");
            }
            let mut buf = Vec::new();
            assert_eq!(verify_single(&b.to_string(), "b", &mut buf).unwrap(), 1, "{label} single");
            let s = String::from_utf8(buf).unwrap();
            assert!(s.starts_with("verified: ") && s.contains(want.as_str()), "{label}: {s}");
        }
        // Not counted twice under --strict: an entry reported above is one
        // problem, and is not also an edge to a trace outside the set.
        for (k, edge) in [
            ("judges-null", serde_json::json!({"type": "judges", "target_content_hash": h, "target_trace_id": null})),
            ("derived-number", serde_json::json!({"type": "derived-from", "target_content_hash": h, "target_trace_id": 7})),
            ("derived-no-id", serde_json::json!({"type": "derived-from", "target_content_hash": h})),
            ("typeless-hashed", serde_json::json!({"target_trace_id": "ghost", "target_content_hash": h})),
        ] {
            let dir = base.join(format!("strict-count-{k}"));
            std::fs::create_dir_all(&dir).unwrap();
            let b = stamped(serde_json::json!({"id": "b", "lineage": [edge]}));
            let f = dir.join("b.minitrace.json");
            std::fs::write(&f, b.to_string()).unwrap();
            for strict in [false, true] {
                let mut buf = Vec::new();
                assert_eq!(verify_set_opts(&[f.to_string_lossy().into_owned()], strict, &mut buf).unwrap(), 1, "{k}");
                let s = String::from_utf8(buf).unwrap();
                assert!(s.contains(", 1 problems") && s.contains("0 external edges"), "{k} strict={strict}: {s}");
            }
        }
        // An entry whose type cannot be read is not checked against its
        // referent as well: no "consistent" line beside its failure, and on
        // its own under --strict one problem, not also an external edge.
        let dir = base.join("type-number-in-set");
        std::fs::create_dir_all(&dir).unwrap();
        let a = stamped(serde_json::json!({"id": "a", "turns": []}));
        let b = stamped(serde_json::json!({"id": "b", "lineage": [
            {"type": 123, "target_trace_id": "a", "target_content_hash": a["content_hash"].clone()}]}));
        let mut files = Vec::new();
        for (name, t) in [("a", &a), ("b", &b)] {
            let f = dir.join(format!("{name}.minitrace.json"));
            std::fs::write(&f, t.to_string()).unwrap();
            files.push(f.to_string_lossy().into_owned());
        }
        let mut buf = Vec::new();
        assert_eq!(verify_set_opts(&files, false, &mut buf).unwrap(), 1);
        let s = String::from_utf8(buf).unwrap();
        assert!(!s.contains("consistent (") && s.contains("0 edges consistent") && s.contains(", 1 problems"), "{s}");
        let mut buf = Vec::new();
        assert_eq!(verify_set_opts(&files[1..], true, &mut buf).unwrap(), 1);
        let s = String::from_utf8(buf).unwrap();
        assert!(s.contains("0 external edges") && s.contains(", 1 problems"), "{s}");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn an_edge_that_needs_no_hash_stays_discovery_only() {
        let base = std::env::temp_dir().join(format!("mv-disc-{}", std::process::id()));
        // Types without an integrity requirement, and near misses of the four.
        for ty in ["derived-from", "delegates-to", "delegated-by", "wrapped-by", "Judges", "x-judges"] {
            let files = edge_set(&base.join(ty), ty, None);
            let mut buf = Vec::new();
            assert_eq!(verify_set_opts(&files, false, &mut buf).unwrap(), 0, "{ty}");
            let s = String::from_utf8(buf).unwrap();
            assert!(!s.contains("without target_content_hash"), "{s}");
            let raw = std::fs::read_to_string(&files[1]).unwrap();
            assert_eq!(verify_single(&raw, "b", &mut std::io::sink()).unwrap(), 0, "{ty} single");
        }
        // The hashed integrity edge still passes.
        let a = stamped(serde_json::json!({"id": "a", "turns": []}));
        let h = a["content_hash"].clone();
        let files = edge_set(&base.join("judges-hashed"), "judges", Some(h));
        assert_eq!(verify_set_opts(&files, true, &mut std::io::sink()).unwrap(), 0);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn duplicate_keys_are_refused() {
        assert!(parse_trace(r#"{"a":1,"a":2}"#).is_err());
        assert!(parse_trace(r#"{"t":[{"c":"x","c":"y"}]}"#).is_err());
        assert!(parse_trace(r#"{"a":{"a":1},"b":[{"a":1},{"a":2}]}"#).is_ok());
    }

    #[test]
    fn detects_pure_bigint_token() {
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": 9007199254740993}"#));
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": -9007199254740993}"#));
    }

    #[test]
    fn does_not_flag_safe_integers() {
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": 9007199254740991}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": -9007199254740991}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": 0}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": 42}"#));
    }

    #[test]
    fn flags_floats_by_value() {
        // §3.4 decides by value, not spelling: a float token whose binary64
        // value lies beyond 2^53-1, or overflows, is out of the domain too.
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": 9007199254740993.0}"#));
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": 9007199254740992.0}"#));
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": 1.5e20}"#));
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": 1e16}"#));
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": -1E300}"#));
        assert!(detect_out_of_domain_number_in_raw(r#"{"x": 1e999}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": 9007199254740991.0}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": 1e15}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": -0.0}"#));
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": 0.1}"#));
    }

    #[test]
    fn does_not_flag_numbers_inside_strings() {
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": "9007199254740993"}"#));
        // Even if the string contains an escaped quote then a bigint outside
        // — the escape handling keeps us inside the string.
        assert!(!detect_out_of_domain_number_in_raw(r#"{"x": "a\"b 9007199254740993"}"#));
    }

    #[test]
    fn flags_too_large_for_i128() {
        // i128 max is ~1.7e38; this token is bigger.
        let s = format!(r#"{{"x": {}}}"#, "1".repeat(40));
        assert!(detect_out_of_domain_number_in_raw(&s));
    }

    /// §3.4 depth: a scalar is 0, `[]` and `{}` are 1, an array or object is
    /// one more than its deepest member. Iterative.
    fn depth_exceeds(value: &Value, limit: usize) -> bool {
        let mut stack = vec![(value, 1usize)];
        while let Some((v, d)) = stack.pop() {
            let children: Vec<&Value> = match v {
                Value::Array(a) => a.iter().collect(),
                Value::Object(o) => o.values().collect(),
                _ => continue,
            };
            if d > limit {
                return true;
            }
            stack.extend(children.into_iter().map(|c| (c, d + 1)));
        }
        false
    }

    #[test]
    fn depth_counts_as_section_3_4() {
        // The CLI's raw scan, checked against the value-based reference.
        let nest = |n: usize| format!("{}1{}", "[".repeat(n), "]".repeat(n));
        assert!(!depth_exceeds_in_raw(&nest(64), MAX_TRACE_DEPTH) && depth_exceeds_in_raw(&nest(65), MAX_TRACE_DEPTH));
        assert!(!depth_exceeds_in_raw("[]", 1) && depth_exceeds_in_raw("[[]]", 1));
        for text in [
            nest(64), nest(65), "[]".into(), "[[]]".into(), r#"{"a":[{"b":{}}],"c":[]}"#.into(),
            r#"{"s":"[[[[{{{{\"]]]","t":[1,[2,[3]]]}"#.into(), r#"["\\",[["\\\\"]]]"#.into(),
        ] {
            let v: Value = serde_json::from_str(&text).unwrap();
            for limit in 1..=66 {
                assert_eq!(depth_exceeds_in_raw(&text, limit), depth_exceeds(&v, limit), "{text} at {limit}");
            }
        }
    }

    #[test]
    fn placeholder_grammar_is_whole_value() {
        assert!(is_placeholder("${trace:abc:content_hash}"));
        assert!(is_placeholder("${trace:a:b:content_hash}")); // id "a:b"
        assert!(is_placeholder("${trace:x}y:content_hash}")); // id "x}y"
        assert!(is_placeholder("${trace:abc:other_field}"));
        assert!(!is_placeholder("${trace:abc:content_hash} ")); // trailing space
        assert!(!is_placeholder("${trace:abc:content_hash}\n"));
        assert!(!is_placeholder("prefix ${trace:abc:content_hash}"));
        assert!(!is_placeholder("${trace:abc:Content_Hash}"));
        assert!(!is_placeholder("${trace::content_hash}")); // empty id
        assert!(!is_placeholder("${trace:abc:}")); // empty field
        assert!(!is_placeholder("${trace:abc}")); // no field
    }

    fn lineage_hash(h: Value) -> Value {
        serde_json::json!({"id": "t", "lineage": [{"type":"derived-from","target_trace_id": "p", "target_content_hash": h}]})
    }

    #[test]
    fn unresolved_placeholder_in_bearing_field_is_a_problem() {
        let p = lineage_hash_problem(&lineage_hash("${trace:p:content_hash}".into()));
        assert_eq!(p, Some(format!("lineage[0].target_content_hash: {}", placeholder_error())));
    }

    #[test]
    fn malformed_lineage_shapes_are_problems() {
        let ph = "${trace:a:content_hash}";
        let edge = serde_json::json!({"type":"derived-from","target_trace_id": "a", "target_content_hash": ph});
        let cases = [
            (serde_json::json!({"lineage": edge.clone()}), "lineage is not an array"),
            (serde_json::json!({"lineage": [[edge.clone()]]}), "lineage[0] is not an object"),
            (serde_json::json!({"lineage": [ph]}), "lineage[0] is not an object"),
            (serde_json::json!({"lineage": "x"}), "lineage is not an array"),
        ];
        for (trace, want) in cases {
            let p = lineage_hash_problem(&trace);
            assert!(p.as_deref().is_some_and(|m| m.contains(want)), "{trace} -> {p:?}");
        }
        assert_eq!(lineage_hash_problem(&serde_json::json!({"lineage": null})), None);
        assert_eq!(lineage_hash_problem(&serde_json::json!({"lineage": []})), None);
    }

    #[test]
    fn near_miss_in_bearing_field_is_malformed() {
        for v in ["${trace:p:content_hash} ", "prefix ${trace:p:content_hash}",
                  "sha256:zz", "sha256:ABC", ""] {
            let p = lineage_hash_problem(&lineage_hash(v.into()));
            assert!(p.as_deref().is_some_and(|m| m.contains("neither a well-formed hash")),
                    "{v:?} -> {p:?}");
        }
        let p = lineage_hash_problem(&lineage_hash(serde_json::json!(7)));
        assert!(p.as_deref().is_some_and(|m| m.contains("not a string")), "{p:?}");
    }

    #[test]
    fn wellformed_or_null_bearing_field_is_fine() {
        let h = format!("sha256:{}", "0".repeat(64));
        assert_eq!(lineage_hash_problem(&lineage_hash(h.into())), None);
        assert_eq!(lineage_hash_problem(&lineage_hash(Value::Null)), None);
        assert_eq!(lineage_hash_problem(&serde_json::json!({"id": "t"})), None);
    }

    #[test]
    fn placeholder_text_outside_bearing_field_is_content() {
        let v = serde_json::json!({
            "id": "t",
            "turns": [{"content": "quote: ${trace:abc:content_hash} and ${record.x}"}],
            "lineage": [{"type":"derived-from","target_trace_id": "p", "target_content_hash": null,
                         "x-note": "${trace:abc:content_hash}"}]
        });
        assert_eq!(lineage_hash_problem(&v), None);
    }

    #[test]
    fn edges_of_extracts_resolved_integrity_edges() {
        let v: Value = serde_json::from_str(
            r#"{"lineage":[
                {"type":"judges","target_trace_id":"a","target_content_hash":"sha256:aa"},
                {"type":"delegated-by","target_trace_id":"b"}
            ]}"#,
        )
        .unwrap();
        let edges = edges_of(&v);
        assert_eq!(edges.len(), 1, "discovery-only edge (no target hash) must be skipped");
        assert_eq!(edges[0].target_id, "a");
        assert_eq!(edges[0].target_hash, "sha256:aa");
    }

    #[test]
    fn edges_of_empty_when_no_lineage() {
        let v: Value = serde_json::from_str(r#"{"id":"t1"}"#).unwrap();
        assert!(edges_of(&v).is_empty());
    }

    #[test]
    fn wellformed_hash_is_printed_unchanged() {
        // Output on every valid trace must be byte-identical to previous
        // releases, or this fix breaks every downstream consumer of the text.
        let h = format!("sha256:{}", "a".repeat(64));
        assert_eq!(hash_for_display(&h), h);
    }

    #[test]
    fn hash_syntax_check_rejects_near_misses() {
        assert!(!is_wellformed_hash(&format!("sha256:{}", "a".repeat(63))));
        assert!(!is_wellformed_hash(&format!("sha256:{}", "a".repeat(65))));
        assert!(!is_wellformed_hash(&format!("sha256:{}", "A".repeat(64)))); // uppercase
        assert!(!is_wellformed_hash(&format!("sha512:{}", "a".repeat(64))));
        assert!(!is_wellformed_hash(&format!("sha256:{}\n", "a".repeat(64)))); // trailing LF
        assert!(!is_wellformed_hash(""));
    }

    #[test]
    fn crafted_hash_cannot_emit_control_bytes() {
        // The live defect: a stored value that erases the verdict line and
        // repaints it as a green `verified:` while the tool exits 1.
        let spoof = format!(
            "sha256:{}\r\x1b[2K\x1b[32mverified: sha256:{}\x1b[0m\x07\x1b[1A",
            "0".repeat(64),
            "a".repeat(64)
        );
        assert!(spoof.contains('\x1b'), "fixture must actually be hostile");
        let shown = hash_for_display(&spoof);
        assert!(!shown.contains('\x1b'));
        assert!(!shown.contains('\r'));
        assert!(!shown.contains('\x07'));
        assert!(shown.starts_with("<malformed content_hash: "));
    }

    #[test]
    fn sanitise_escapes_controls_and_bidi() {
        assert_eq!(sanitise("a\x1bb"), "a\\u{001b}b");
        assert_eq!(sanitise("a\rb"), "a\\u{000d}b");
        assert_eq!(sanitise("a\x7fb"), "a\\u{007f}b");
        assert_eq!(sanitise("a\u{202e}b"), "a\\u{202e}b");   // RTL override
        assert_eq!(sanitise("a\u{2069}b"), "a\\u{2069}b");   // pop isolate
        // Ordinary text, including non-ASCII, passes through unchanged.
        assert_eq!(sanitise("café ok"), "café ok");
    }

    #[test]
    fn sanitise_bounds_length() {
        let long = "x".repeat(5000);
        let shown = sanitise(&long);
        assert!(shown.len() < 200, "unbounded echo: {} chars", shown.len());
        assert!(shown.contains("truncated, 5000 chars"));
    }

    #[test]
    fn no_control_byte_survives_any_untrusted_field() {
        // Sweep rather than spot-check: every value the tool echoes goes
        // through one of these two, so the property is stated over both.
        for hostile in [
            "\x1b[32mverified",
            "\r\n  verified: sha256:0",
            "\u{202e}drossap",
            "\x00\x01\x02",
            "\x07\x08\x0b\x0c",
        ] {
            for shown in [sanitise(hostile), hash_for_display(hostile)] {
                assert!(
                    !shown.chars().any(needs_escape),
                    "control character survived: {shown:?}"
                );
            }
        }
    }

    #[test]
    fn stored_hash_variants() {
        let present: Value =
            serde_json::from_str(r#"{"content_hash":"sha256:aa"}"#).unwrap();
        assert!(matches!(stored_hash(&present), StoredHash::Present(s) if s == "sha256:aa"));
        let null: Value = serde_json::from_str(r#"{"content_hash":null}"#).unwrap();
        assert!(matches!(stored_hash(&null), StoredHash::Null));
        let absent: Value = serde_json::from_str(r#"{"id":"t1"}"#).unwrap();
        assert!(matches!(stored_hash(&absent), StoredHash::Null));
        let wrong: Value = serde_json::from_str(r#"{"content_hash":42}"#).unwrap();
        assert!(matches!(stored_hash(&wrong), StoredHash::NotString("number")));
    }

    // ---- verify_single / verify_set kill-tests ----------------------------
    // cargo-mutants showed the core verdict + cross-trace integrity logic was
    // entirely unconstrained (verify_set: 20 survivors / 0 caught). These pin
    // the exit-code-affecting behaviour; the display-only tallies (verified,
    // edges_ok, edges_external) are pinned by set_summary_reports_counts, which
    // captures the summary into a buffer (enabled by the writer-refactor).

    use std::sync::atomic::{AtomicU64, Ordering};

    /// Insert the correct `content_hash` so the trace self-verifies.
    fn sealed(mut v: Value) -> Value {
        if let Value::Object(ref mut m) = v {
            m.remove("content_hash");
        }
        let h = recompute_hash(&v).unwrap();
        if let Value::Object(ref mut m) = v {
            m.insert("content_hash".to_string(), Value::String(h));
        }
        v
    }

    fn hash_of(v: &Value) -> String {
        match v.get("content_hash") {
            Some(Value::String(s)) => s.clone(),
            _ => unreachable!("sealed trace must carry a string content_hash"),
        }
    }

    /// Temp JSON file that removes itself on drop.
    struct TmpJson(String);
    impl TmpJson {
        fn new(v: &Value) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let n = N.fetch_add(1, Ordering::Relaxed);
            let p = std::env::temp_dir()
                .join(format!("mtv_test_{}_{}.json", std::process::id(), n));
            std::fs::write(&p, serde_json::to_string(v).unwrap()).unwrap();
            TmpJson(p.to_str().unwrap().to_string())
        }
    }
    impl Drop for TmpJson {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }

    #[test]
    fn single_good_trace_verifies_exit_0() {
        let t = sealed(serde_json::json!({"id": "t1", "data": "x"}));
        assert_eq!(verify_single(&serde_json::to_string(&t).unwrap(), "t1", &mut std::io::sink()).unwrap(), 0);
    }

    #[test]
    fn single_tampered_trace_mismatches_exit_1() {
        let mut t = sealed(serde_json::json!({"id": "t1", "data": "x"}));
        if let Value::Object(ref mut m) = t {
            m.insert("content_hash".into(), Value::String("sha256:deadbeef".into()));
        }
        assert_eq!(verify_single(&serde_json::to_string(&t).unwrap(), "t1", &mut std::io::sink()).unwrap(), 1);
    }

    #[test]
    fn single_missing_content_hash_errors() {
        assert!(verify_single(r#"{"id":"t1","data":"x"}"#, "t1", &mut std::io::sink()).is_err());
    }

    #[test]
    fn set_all_good_exit_0() {
        let a = TmpJson::new(&sealed(serde_json::json!({"id":"a","data":1})));
        let b = TmpJson::new(&sealed(serde_json::json!({"id":"b","data":2})));
        assert_eq!(verify_set(&[a.0.clone(), b.0.clone()], &mut std::io::sink()).unwrap(), 0);
    }

    #[test]
    fn set_self_hash_mismatch_exit_1() {
        let good = TmpJson::new(&sealed(serde_json::json!({"id":"a","data":1})));
        let mut bad = sealed(serde_json::json!({"id":"b","data":2}));
        if let Value::Object(ref mut m) = bad {
            m.insert("content_hash".into(), Value::String("sha256:wrong".into()));
        }
        let bad = TmpJson::new(&bad);
        assert_eq!(verify_set(&[good.0.clone(), bad.0.clone()], &mut std::io::sink()).unwrap(), 1);
    }

    #[test]
    fn set_content_hash_not_string_is_problem_exit_1() {
        let t = TmpJson::new(&serde_json::json!({"id":"a","content_hash":42}));
        assert_eq!(verify_set(&[t.0.clone()], &mut std::io::sink()).unwrap(), 1);
    }

    #[test]
    fn set_consistent_edge_exit_0() {
        let a = sealed(serde_json::json!({"id":"a","data":1}));
        let b = sealed(serde_json::json!({
            "id":"b",
            "lineage":[{"type":"derived-from","target_trace_id":"a","target_content_hash": hash_of(&a)}]
        }));
        let pa = TmpJson::new(&a);
        let pb = TmpJson::new(&b);
        assert_eq!(verify_set(&[pa.0.clone(), pb.0.clone()], &mut std::io::sink()).unwrap(), 0);
    }

    #[test]
    fn set_edge_to_another_state_is_not_verified_and_fails_only_strict() {
        // trace §7.1, envelope §6.3 (ruled 2026-10-29): an edge whose hash
        // differs from its in-set referent's refers to another state of it.
        let a = sealed(serde_json::json!({"id":"a","data":1}));
        let b = sealed(serde_json::json!({
            "id":"b",
            "lineage":[{"type":"derived-from","target_trace_id":"a","target_content_hash":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}]
        }));
        let pa = TmpJson::new(&a);
        let pb = TmpJson::new(&b);
        let mut buf: Vec<u8> = Vec::new();
        assert_eq!(verify_set(&[pa.0.clone(), pb.0.clone()], &mut buf).unwrap(), 0);
        let s = String::from_utf8(buf).unwrap();
        assert!(s.contains("refers to another state of the referent, not verified"), "{s}");
        assert!(s.contains("0 edges consistent") && s.contains("1 edges to another state") && s.contains("0 problems"), "{s}");
        assert!(!s.contains("INCONSISTENT"), "{s}");
        let mut buf: Vec<u8> = Vec::new();
        assert_eq!(verify_set_opts(&[pa.0.clone(), pb.0.clone()], true, &mut buf).unwrap(), 1);
        let s = String::from_utf8(buf).unwrap();
        assert!(s.contains("1 edges to another state, 1 problems"), "{s}");
    }

    #[test]
    fn set_edge_referent_null_hash_is_problem_exit_1() {
        let a = serde_json::json!({"id":"a","content_hash":null});
        let b = sealed(serde_json::json!({
            "id":"b",
            "lineage":[{"type":"derived-from","target_trace_id":"a","target_content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
        }));
        let pa = TmpJson::new(&a);
        let pb = TmpJson::new(&b);
        assert_eq!(verify_set(&[pa.0.clone(), pb.0.clone()], &mut std::io::sink()).unwrap(), 1);
    }

    #[test]
    fn set_external_edge_not_a_problem_exit_0() {
        let b = sealed(serde_json::json!({
            "id":"b",
            "lineage":[{"type":"derived-from","target_trace_id":"ghost","target_content_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
        }));
        let pb = TmpJson::new(&b);
        assert_eq!(verify_set(&[pb.0.clone()], &mut std::io::sink()).unwrap(), 0);
    }

    #[test]
    fn set_duplicate_id_errors() {
        let a = TmpJson::new(&sealed(serde_json::json!({"id":"dup","data":1})));
        let b = TmpJson::new(&sealed(serde_json::json!({"id":"dup","data":2})));
        assert!(verify_set(&[a.0.clone(), b.0.clone()], &mut std::io::sink()).is_err());
    }

    #[test]
    fn set_non_object_errors() {
        let p = TmpJson::new(&serde_json::json!([1, 2, 3]));
        assert!(verify_set(&[p.0.clone()], &mut std::io::sink()).is_err());
    }

    #[test]
    fn set_summary_reports_counts() {
        // Pins the display-only tallies (verified / edges_ok / edges_external)
        // by capturing the summary into a buffer — the writer-refactor enables this.
        let a = sealed(serde_json::json!({"id":"a","data":1}));
        let b = sealed(serde_json::json!({
            "id":"b",
            "lineage":[
                {"type":"derived-from","target_trace_id":"a","target_content_hash": hash_of(&a)},
                {"type":"derived-from","target_trace_id":"ghost","target_content_hash": format!("sha256:{}", "e".repeat(64))}
            ]
        }));
        let pa = TmpJson::new(&a);
        let pb = TmpJson::new(&b);
        let mut buf: Vec<u8> = Vec::new();
        let code = verify_set(&[pa.0.clone(), pb.0.clone()], &mut buf).unwrap();
        let s = String::from_utf8(buf).unwrap();
        assert_eq!(code, 0, "all-good set must exit 0; output:\n{s}");
        assert!(s.contains("2 self-verified"), "verified count wrong:\n{s}");
        assert!(s.contains("1 edges consistent"), "edges_ok count wrong:\n{s}");
        assert!(s.contains("1 external edges"), "edges_external count wrong:\n{s}");
    }

    #[test]
    fn single_content_quoting_placeholder_verifies_exit_0() {
        let t = sealed(serde_json::json!({
            "id": "t1",
            "turns": [{"content": "the syntax is ${trace:<id>:content_hash}, e.g. ${trace:agent-1:content_hash}"}]
        }));
        assert_eq!(verify_single(&serde_json::to_string(&t).unwrap(), "t1", &mut std::io::sink()).unwrap(), 0);
    }

    #[test]
    fn single_unresolved_or_malformed_edge_errors() {
        for (h, want) in [("${trace:p:content_hash}", "unresolved cross-trace placeholder"),
                          ("${trace:p:content_hash} ", "neither a well-formed hash"),
                          ("sha256:zz", "neither a well-formed hash")] {
            let t = sealed(serde_json::json!({
                "id": "t1", "lineage": [{"type":"derived-from","target_trace_id": "p", "target_content_hash": h}]
            }));
            let err = verify_single(&serde_json::to_string(&t).unwrap(), "t1", &mut std::io::sink())
                .expect_err(h).to_string();
            assert!(err.contains(want), "{h:?} -> {err}");
        }
    }

    #[test]
    fn set_content_quoting_placeholder_exit_0_and_malformed_edge_errors() {
        let a = sealed(serde_json::json!({"id": "a", "turns": [{"content": "${trace:b:content_hash}"}]}));
        let pa = TmpJson::new(&a);
        assert_eq!(verify_set(&[pa.0.clone()], &mut std::io::sink()).unwrap(), 0);
        for (h, want) in [("${trace:a:content_hash}", "unresolved cross-trace placeholder"),
                          ("prefix ${trace:a:content_hash}", "neither a well-formed hash")] {
            let b = sealed(serde_json::json!({
                "id": "b", "lineage": [{"type":"derived-from","target_trace_id": "a", "target_content_hash": h}]
            }));
            let pb = TmpJson::new(&b);
            let err = verify_set(&[pa.0.clone(), pb.0.clone()], &mut std::io::sink())
                .expect_err(h).to_string();
            assert!(err.contains(want), "{h:?} -> {err}");
        }
    }

    #[test]
    fn set_malformed_hash_on_external_edge_errors() {
        // formerly `sha256:zz` on a ghost edge was reported as external and passed
        let b = sealed(serde_json::json!({
            "id": "b", "lineage": [{"type":"derived-from","target_trace_id": "ghost", "target_content_hash": "sha256:zz"}]
        }));
        let pb = TmpJson::new(&b);
        let err = verify_set(&[pb.0.clone()], &mut std::io::sink()).expect_err("malformed").to_string();
        assert!(err.contains("neither a well-formed hash"), "{err}");
    }

    #[test]
    fn single_malformed_lineage_shape_errors() {
        let t = sealed(serde_json::json!({
            "id": "t1", "lineage": {"type":"derived-from","target_trace_id": "a", "target_content_hash": "${trace:a:content_hash}"}
        }));
        let err = verify_single(&serde_json::to_string(&t).unwrap(), "t1", &mut std::io::sink())
            .expect_err("object lineage").to_string();
        assert!(err.contains("lineage is not an array"), "{err}");
    }

    /// The shared §3.4 vectors (adapters/tests/vectors/canonical-vectors.json),
    /// which the Python parity test runs too. A canonical vector must pass the
    /// numeric-domain check and reproduce the recorded bytes and digest; a
    /// not-conformant vector must be refused for its reason: the by-value
    /// number check, the parser (a non-finite number), or the depth limit.
    #[test]
    fn shared_canonical_vectors() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/vectors/canonical-vectors.json");
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let vectors = doc["vectors"].as_array().unwrap();
        let (mut canonical, mut refused) = (0usize, 0usize);
        for v in vectors {
            let name = v["name"].as_str().unwrap();
            let input = v["input"].as_str().unwrap();
            let parsed: Result<Value, _> = serde_json::from_str(input);
            match v["verdict"].as_str().unwrap() {
                "canonical" => {
                    assert!(!detect_out_of_domain_number_in_raw(input), "[{name}] flagged as out of range");
                    let value = parsed.unwrap_or_else(|e| panic!("[{name}] parse: {e}"));
                    let bytes = serde_json_canonicalizer::to_vec(&value).unwrap();
                    assert_eq!(
                        std::str::from_utf8(&bytes).unwrap(),
                        v["canonical"].as_str().unwrap(),
                        "[{name}] canonical bytes"
                    );
                    assert_eq!(
                        format!("sha256:{:x}", Sha256::digest(&bytes)),
                        v["sha256"].as_str().unwrap(),
                        "[{name}] digest"
                    );
                    canonical += 1;
                }
                "not-conformant" => {
                    let reason = v["reason"].as_str().unwrap();
                    let refused_here = match reason {
                        "number-outside-safe-range" => detect_out_of_domain_number_in_raw(input),
                        // I-JSON (§3.4): refused by parse_trace, for this reason.
                        "duplicate-key" => parse_trace(input).is_err_and(|e| e.to_string().contains("duplicate object key")),
                        "noncharacter" => parse_trace(input).is_err_and(|e| e.to_string().contains("noncharacter")),
                        "unpaired-surrogate" => parse_trace(input).is_err_and(|e| e.to_string().contains("surrogate")
                            || e.to_string().contains("hex escape")),
                        "non-finite-number" => parsed.is_err(),
                        // The check the CLI runs (verify_single, verify_set_opts).
                        "depth-exceeds-64" => depth_exceeds_in_raw(input, MAX_TRACE_DEPTH),
                        other => panic!("[{name}] unknown reason {other}"),
                    };
                    assert!(refused_here, "[{name}] accepted, but the vector says {reason}");
                    refused += 1;
                }
                other => panic!("[{name}] unknown verdict {other}"),
            }
        }
        assert!(canonical > 0 && refused > 0, "vector file lost a class");
        assert_eq!(canonical + refused, vectors.len());
    }

    /// A trace whose hash verifies but which carries an out-of-range integer
    /// literal is reported not conformant, with exit 1, after the hash line.
    #[test]
    fn verified_hash_with_bigint_is_not_conformant() {
        let raw_body = r#"{"id":"t1","metrics":{"big":9007199254740993}}"#;
        let value: Value = serde_json::from_str(raw_body).unwrap();
        let hash = recompute_hash(&value).unwrap();
        let raw = format!(
            r#"{{"id":"t1","metrics":{{"big":9007199254740993}},"content_hash":"{hash}"}}"#
        );
        let mut out = Vec::new();
        let code = verify_single(&raw, "t1", &mut out).unwrap();
        let text = String::from_utf8(out).unwrap();
        assert_eq!(code, 1, "{text}");
        assert!(text.starts_with("verified: "), "{text}");
        assert!(text.contains("not conformant") && text.contains("§3.4"), "{text}");
    }
}
