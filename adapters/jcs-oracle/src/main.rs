//! JCS (RFC 8785) + SHA-256 oracle for minitrace content_hash parity tests.
//!
//! Mirrors the canonicalisation + hashing pipeline used by
//! `adapters/cross_trace.py` (Python `rfc8785` + `hashlib.sha256`), in Rust,
//! so that the byte-identical-across-languages claim in
//! minitrace-spec v0.3.0 §3.4 can be falsified by a Python-side parity
//! driver feeding edge cases to both implementations.
//!
//! ## Usage
//!
//! Reads one JSON document from stdin and prints either the JCS canonical
//! form (default) or `sha256:<hex>` of the JCS bytes (with `--hash`).
//!
//! ```text
//! echo '{"b":2,"a":1}' | jcs-oracle              # → {"a":1,"b":2}
//! echo '{"b":2,"a":1}' | jcs-oracle --hash       # → sha256:<64 hex>
//! ```
//!
//! ## Scope
//!
//! Does NOT strip a `content_hash` field, does NOT resolve cross-trace
//! placeholders, does NOT verify against a stored hash. Those are
//! trace-shape concerns belonging to a downstream verifier; the oracle
//! only proves the JCS+SHA-256 floor matches Python.

use std::io::{self, Read, Write};
use std::process::ExitCode;

use sha2::{Digest, Sha256};

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            // Write to stderr so stdout stays clean for the parity driver
            // to capture either the JCS bytes or the `sha256:` line.
            let _ = writeln!(io::stderr(), "jcs-oracle: {e}");
            ExitCode::from(1)
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut hash_mode = false;
    for arg in &args {
        match arg.as_str() {
            "--hash" => hash_mode = true,
            "-h" | "--help" => {
                print_help();
                return Ok(());
            }
            other => return Err(format!("unknown argument: {other}").into()),
        }
    }

    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;

    let value: serde_json::Value = serde_json::from_str(&input)?;
    let canonical: Vec<u8> = serde_json_canonicalizer::to_vec(&value)?;

    let stdout = io::stdout();
    let mut out = stdout.lock();
    if hash_mode {
        let digest = Sha256::digest(&canonical);
        writeln!(out, "sha256:{digest:x}")?;
    } else {
        out.write_all(&canonical)?;
        // No trailing newline: the JCS output is the exact bytes the hash
        // would cover; appending a newline would silently corrupt
        // byte-for-byte diff against rfc8785.dumps().
    }
    Ok(())
}

fn print_help() {
    println!(
        "jcs-oracle — RFC 8785 (JCS) + SHA-256 oracle\n\
         \n\
         Reads JSON from stdin, writes JCS bytes (default) or\n\
         `sha256:<hex>` of those bytes (--hash) to stdout.\n\
         \n\
         Usage:\n\
         \x20\x20jcs-oracle < input.json\n\
         \x20\x20jcs-oracle --hash < input.json\n\
         \n\
         Exit codes: 0 ok, 1 on parse or I/O error (message on stderr).\n"
    );
}
