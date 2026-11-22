"""Open a harness's SQLite session store as evidence: read it, never change it.

goose and opencode keep sessions in WAL-mode SQLite databases. Opening the
original with a read-write connection creates `-wal`/`-shm` files next to it,
and closing that connection checkpoints any pending WAL frames into the main
file and removes the `-wal`: converting the evidence would change it. The
read-only alternatives do not fit either. A `mode=ro` connection to a WAL
database still creates `-wal`/`-shm` next to the original when the directory
is writable, and cannot remove them on close;
`immutable=1` ignores the WAL, silently dropping every transaction not yet
checkpointed.

So the database and its `-wal` (when present) are copied into a private
temporary directory and the copy is opened. The originals are only read. The
`-shm` is not copied: it is an index over the WAL that SQLite rebuilds.

The copy is removed at exit (atexit), which a process killed by a signal
never reaches. Under verify_run the copy is inside the run's own step
directory (TMPDIR), which the run removes even when the step is killed
(not when verify_run itself is killed). Run
by hand, a preprocessor killed by SIGTERM or SIGKILL leaves its copy in
$TMPDIR; Ctrl-C removes it (ruled 2026-10-28).

The copy is opened with `trusted_schema=OFF`, so views and triggers defined
in an untrusted file cannot call functions with side effects, and with
`query_only`, so nothing the preprocessor does can write even to the copy.

Copying a database while its harness is still writing it is not atomic; a
store must be quiescent (harness stopped). A store that changes during the
copy is detected (hashes before and after) and refused, not read.

The bounds are the shared reader's (ruled 2026-10-28): the store and its
`-wal` together may hold MAX_STORE_BYTES, checked before anything is hashed
or copied, and no string or blob may exceed MAX_VALUE_BYTES (SQLite's length
limit on the connection, so a longer value fails its query with
sqlite3.DataError). An expected table with a generated column is refused: a
VIRTUAL one is computed on every read (`SELECT *` materialises it, whatever
trusted_schema says), and no harness store read here has either kind.
"""
from __future__ import annotations

import atexit
import hashlib
import shutil
import sqlite3
import tempfile
from pathlib import Path

MAX_STORE_BYTES = 512 * 1024 * 1024   # the store and its -wal together (source_reader max_file_bytes)
MAX_VALUE_BYTES = 32 * 1024 * 1024    # one string or blob (source_reader max_line_bytes)


class EvidenceUnusable(Exception):
    """The store cannot be read as evidence (larger than the bound, changed
    while copied, a symlinked or irregular sidecar, or an expected table that
    is not a table or has a generated column)."""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_evidence_db(path: Path, tables: tuple[str, ...] = ()) -> sqlite3.Connection:
    """Connection to a private copy of `path` (plus its `-wal`, if any).

    The originals are hashed before and after copying and the copies are
    checked against them: a store that changes while it is copied is refused
    rather than read as a mixture of two states. A `-wal` that is a symlink or
    not a regular file is refused. Each name in `tables` that exists in the
    store must be an ordinary table: a view of that name (e.g. an unbounded
    recursive CTE) would run arbitrary query cost inside the conversion.
    """
    path = Path(path)
    wal = path.with_name(path.name + "-wal")
    # A symlink, FIFO or device as the store would be read without end (a
    # link to /dev/zero has size 0) or block; the store is checked as its
    # -wal is.
    if path.is_symlink() or not path.is_file():
        raise EvidenceUnusable(f"{str(path)!r} is not a regular file")
    if wal.is_symlink() or (wal.exists() and not wal.is_file()):
        raise EvidenceUnusable(f"{str(wal)!r} is not a regular file")
    size = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    if size > MAX_STORE_BYTES:
        raise EvidenceUnusable(f"{str(path)!r} and its -wal hold {size} bytes, "
                               f"more than {MAX_STORE_BYTES}")
    before =(_sha256(path), _sha256(wal) if wal.exists() else None)
    tmp = Path(tempfile.mkdtemp(prefix="minitrace-sqlite-"))
    atexit.register(shutil.rmtree, tmp, True)
    copy = tmp / "evidence.db"
    shutil.copyfile(path, copy)
    wal_copy = copy.with_name(copy.name + "-wal")
    if before[1] is not None:
        shutil.copyfile(wal, wal_copy)
    after = (_sha256(path), _sha256(wal) if wal.exists() else None)
    copied = (_sha256(copy), _sha256(wal_copy) if wal_copy.exists() else None)
    if not (before == after == copied):
        raise EvidenceUnusable(f"{str(path)!r} changed while it was copied; "
                               "stop the harness and convert again")
    db = sqlite3.connect(str(copy))
    # Text that is not valid UTF-8 (a crafted TEXT column) is decoded with
    # U+FFFD instead of aborting the query with OperationalError.
    db.text_factory = lambda b: b.decode("utf-8", "replace")
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    db.execute("PRAGMA trusted_schema = OFF")
    db.execute("PRAGMA query_only = ON")
    for name in tables:
        # The type of the parsed schema (table, view, virtual, shadow), not
        # the statement text, which writable_schema can rewrite while the
        # table still loads through its module (fts5 content from a view,
        # for one). Case-insensitive, as SQL resolves the caller's
        # `FROM name`; triggers and indexes are not listed.
        row = db.execute("SELECT type FROM pragma_table_list WHERE schema = 'main' "
                         "AND name = ? COLLATE NOCASE", (name,)).fetchone()
        if row is None:
            continue
        if row[0] != "table":
            db.close()
            kind = {"virtual": "virtual table", "shadow": "shadow table"}.get(row[0], row[0])
            raise EvidenceUnusable(f"{name!r} is a {kind}, not a table")
        # hidden 2: a VIRTUAL generated column, 3: a STORED one
        generated = [c for c, hidden in db.execute(
            "SELECT name, hidden FROM pragma_table_xinfo(?)", (name,)) if hidden in (2, 3)]
        if generated:
            db.close()
            raise EvidenceUnusable(f"{name!r} has a generated column ({', '.join(generated)})")
    return db
