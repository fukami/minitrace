"""Reading a SQLite session store must not change it, and must not lose data.

The evidence here is what a collector finds for a harness that was stopped
without a final checkpoint: a WAL-mode database whose newest row exists only
in the `-wal` file.
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlite_source import open_evidence_db  # noqa: E402


def _evidence(tmp_path: pathlib.Path) -> pathlib.Path:
    live = tmp_path / "live"
    live.mkdir()
    db = live / "sessions.db"
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode = WAL")
    w.execute("PRAGMA wal_autocheckpoint = 0")
    w.execute("CREATE TABLE s (id TEXT)")
    w.execute("INSERT INTO s VALUES ('checkpointed')")
    w.commit()
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.execute("INSERT INTO s VALUES ('only-in-wal')")
    w.commit()
    # Collect while the writer is still open: the second row is WAL-only.
    ev = tmp_path / "evidence"
    ev.mkdir()
    for name in ("sessions.db", "sessions.db-wal"):
        shutil.copyfile(live / name, ev / name)
    w.close()
    return ev / "sessions.db"


def _fingerprint(d: pathlib.Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


def test_reads_wal_frames_and_leaves_evidence_untouched(tmp_path):
    db_path = _evidence(tmp_path)
    before = _fingerprint(db_path.parent)
    db = open_evidence_db(db_path)
    rows = [r[0] for r in db.execute("SELECT id FROM s ORDER BY rowid")]
    db.close()
    assert rows == ["checkpointed", "only-in-wal"]
    assert _fingerprint(db_path.parent) == before  # no change, no new sidecar


def test_copy_is_query_only_and_schema_untrusted(tmp_path):
    db = open_evidence_db(_evidence(tmp_path))
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    assert db.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    try:
        db.execute("INSERT INTO s VALUES ('x')")
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("write to the evidence copy succeeded")


def test_plain_connect_would_alter_the_evidence(tmp_path):
    # Kill-test for the premise: the naive open the preprocessors used before
    # does change the evidence directory, so the assertion above is not vacuous.
    db_path = _evidence(tmp_path)
    before = _fingerprint(db_path.parent)
    db = sqlite3.connect(str(db_path))
    db.execute("SELECT count(*) FROM s").fetchone()
    db.close()
    assert _fingerprint(db_path.parent) != before


def test_read_only_uri_would_also_leave_sidecars(tmp_path):
    # Why the drift watchers moved off `mode=ro` as well: on a WAL database a
    # read-only connection still creates -wal/-shm beside the evidence.
    db_path = _evidence(tmp_path)
    (db_path.parent / "sessions.db-wal").unlink()  # checkpointed-only evidence
    before = set(p.name for p in db_path.parent.iterdir())
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.execute("SELECT count(*) FROM s").fetchone()
    db.close()
    assert set(p.name for p in db_path.parent.iterdir()) != before


def test_view_in_place_of_an_expected_table_is_refused(tmp_path):
    import pytest
    from sqlite_source import EvidenceUnusable
    db_path = tmp_path / "v.db"
    c = sqlite3.connect(db_path)
    c.execute("CREATE TABLE real (x)")
    c.execute("CREATE VIEW sessions AS WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT n FROM r")
    c.commit(); c.close()
    with pytest.raises(EvidenceUnusable, match="is a view"):
        open_evidence_db(db_path, tables=("sessions",))
    open_evidence_db(db_path, tables=("real",)).close()


def test_store_changing_during_copy_is_refused(tmp_path, monkeypatch):
    import pytest
    import shutil as _sh
    import sqlite_source
    from sqlite_source import EvidenceUnusable
    db_path = _evidence(tmp_path)
    real_copy = _sh.copyfile

    def copy_then_touch(src, dst):
        r = real_copy(src, dst)
        if str(src).endswith("sessions.db"):
            with open(src, "ab") as fh:
                fh.write(b"\0")          # the harness wrote meanwhile
        return r
    monkeypatch.setattr(sqlite_source.shutil, "copyfile", copy_then_touch)
    with pytest.raises(EvidenceUnusable, match="changed while it was copied"):
        open_evidence_db(db_path)


def test_symlinked_wal_is_refused(tmp_path):
    import pytest
    from sqlite_source import EvidenceUnusable
    db_path = _evidence(tmp_path)
    wal = db_path.with_name(db_path.name + "-wal")
    elsewhere = tmp_path / "elsewhere-wal"
    wal.rename(elsewhere)
    wal.symlink_to(elsewhere)
    with pytest.raises(EvidenceUnusable, match="not a regular file"):
        open_evidence_db(db_path)


# --- bounds on a store read as evidence (ruled 2026-10-28) ------------------
# A hostile store bypassed every bound: a GENERATED column passed the table
# check and trusted_schema=OFF and was materialised per row by SELECT *, and
# no size or value-length bound applied to SQLite stores.


def _store_with(tmp_path, ddl: str, rows=(), name="g.db") -> pathlib.Path:
    p = tmp_path / name
    c = sqlite3.connect(p)
    c.execute(ddl)
    for sql, args in rows:
        c.execute(sql, args)
    c.commit()
    c.close()
    return p


def test_an_expected_table_with_a_generated_column_is_refused(tmp_path):
    import pytest
    from sqlite_source import EvidenceUnusable
    for kind in ("VIRTUAL", "STORED"):
        p = _store_with(tmp_path, "CREATE TABLE messages (id INTEGER PRIMARY KEY, c TEXT, "
                        f"g TEXT GENERATED ALWAYS AS (printf('%.*c', 1000, 'x')) {kind})",
                        name=f"{kind}.db")
        with pytest.raises(EvidenceUnusable, match="generated column"):
            open_evidence_db(p, tables=("sessions", "messages"))
        # A table the caller does not read is not checked, and a store
        # without generated columns in what it reads opens as before.
        open_evidence_db(p, tables=("sessions",)).close()
    plain = _store_with(tmp_path, "CREATE TABLE messages (id INTEGER PRIMARY KEY, c TEXT)", name="plain.db")
    open_evidence_db(plain, tables=("messages",)).close()


def test_a_value_longer_than_32_mib_cannot_be_read(tmp_path):
    import pytest
    import sqlite_source
    bound = 32 * 1024 * 1024                    # the shared reader's per-record bound
    p = _store_with(tmp_path, "CREATE TABLE t (k TEXT, v BLOB)",
                    [("INSERT INTO t VALUES (?, ?)", ("at", b"x" * bound)),
                     ("INSERT INTO t VALUES (?, ?)", ("over", b"x" * (bound + 1)))])
    db = open_evidence_db(p, tables=("t",))
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == bound
    assert len(db.execute("SELECT v FROM t WHERE k = 'at'").fetchone()[0]) == bound
    with pytest.raises(sqlite3.DataError):
        db.execute("SELECT v FROM t WHERE k = 'over'").fetchone()
    with pytest.raises(sqlite3.DataError):
        db.execute("SELECT * FROM t").fetchall()
    db.close()
    assert sqlite_source.MAX_VALUE_BYTES == bound


def test_a_store_and_its_wal_over_the_size_bound_are_refused_before_copying(tmp_path, monkeypatch):
    import pytest
    import sqlite_source
    from sqlite_source import EvidenceUnusable
    default = getattr(sqlite_source, "MAX_STORE_BYTES", None)
    db_path = _evidence(tmp_path)
    wal = db_path.with_name(db_path.name + "-wal")
    main_size, wal_size = db_path.stat().st_size, wal.stat().st_size
    assert main_size > 0 and wal_size > 0
    copies = []
    real_mkdtemp = sqlite_source.tempfile.mkdtemp
    monkeypatch.setattr(sqlite_source.tempfile, "mkdtemp",
                        lambda **kw: copies.append(1) or real_mkdtemp(**kw))
    # The two together count: a bound above either file alone but below
    # their sum refuses, and nothing is copied.
    monkeypatch.setattr(sqlite_source, "MAX_STORE_BYTES", main_size + wal_size - 1, raising=False)
    assert sqlite_source.MAX_STORE_BYTES >= max(main_size, wal_size)
    with pytest.raises(EvidenceUnusable, match="more than"):
        open_evidence_db(db_path)
    assert copies == []
    monkeypatch.setattr(sqlite_source, "MAX_STORE_BYTES", main_size + wal_size)
    open_evidence_db(db_path).close()
    assert copies == [1]
    assert default == 512 * 1024 * 1024        # the shared reader's whole-file bound


def test_expected_tables_are_found_whatever_the_case_of_their_name(tmp_path):
    # SQL resolves `SELECT * FROM messages` to a table named MESSAGES, so the
    # checks look the name up the same way.
    import pytest
    from sqlite_source import EvidenceUnusable
    for n, name in enumerate(("MESSAGES", "Messages")):   # one file each: APFS folds case
        p = _store_with(tmp_path, f"CREATE TABLE {name} (c TEXT, "
                        "g TEXT GENERATED ALWAYS AS (printf('%.*c', 10, 'x')) VIRTUAL)", name=f"case{n}.db")
        with pytest.raises(EvidenceUnusable, match="generated column"):
            open_evidence_db(p, tables=("messages",))
    p = _store_with(tmp_path, "CREATE VIEW SESSIONS AS SELECT 1 AS n", name="view.db")
    with pytest.raises(EvidenceUnusable, match="is a view"):
        open_evidence_db(p, tables=("sessions",))
    # A trigger may share a table's name; it is not the table. Created first,
    # it is listed before the table.
    p = _store_with(tmp_path, "CREATE TABLE other (id TEXT)", name="trigger.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TRIGGER Sessions AFTER INSERT ON other BEGIN SELECT 1; END")
    c.execute("CREATE TABLE sessions (id TEXT)")
    c.commit(); c.close()
    open_evidence_db(p, tables=("sessions",)).close()


def test_an_expected_table_that_is_a_virtual_table_is_refused(tmp_path):
    import pytest
    from sqlite_source import EvidenceUnusable
    for n, ddl in enumerate(("CREATE VIRTUAL TABLE messages USING fts5(c)",
                             "create virtual table messages using fts5(c)")):   # kept as written
        p = _store_with(tmp_path, ddl, name=f"fts{n}.db")
        with pytest.raises(EvidenceUnusable, match="virtual table"):
            open_evidence_db(p, tables=("messages",))
        open_evidence_db(p, tables=("sessions",)).close()   # not read, not checked
        # Its module's own tables (fts5: messages_data, ...) are not plain tables either.
        with pytest.raises(EvidenceUnusable, match="shadow table"):
            open_evidence_db(p, tables=("messages_data",))
    # The statement text in sqlite_master can be rewritten (writable_schema)
    # and SQLite still loads the table through its module: the type is
    # taken from the parsed schema, not from the text.
    for n, text in enumerate(("create virtual table messages using fts5(c)",
                              "CREATE  VIRTUAL TABLE messages USING fts5(c)")):
        p = _store_with(tmp_path, "CREATE VIRTUAL TABLE messages USING fts5(c)", name=f"rewritten{n}.db")
        c = sqlite3.connect(p)
        c.execute("PRAGMA writable_schema = ON")
        c.execute("UPDATE sqlite_master SET sql = ? WHERE name = 'messages'", (text,))
        c.commit(); c.close()
        with pytest.raises(EvidenceUnusable, match="virtual table"):
            open_evidence_db(p, tables=("messages",))


def test_a_store_path_that_is_not_a_regular_file_is_refused(tmp_path):
    import os
    import pytest
    from sqlite_source import EvidenceUnusable
    real = _store_with(tmp_path, "CREATE TABLE sessions (id TEXT)", name="real.db")
    link = tmp_path / "link.db"
    link.symlink_to(real)
    zero = tmp_path / "zero.db"
    zero.symlink_to("/dev/zero")          # stat size 0, reads without end
    fifo = tmp_path / "fifo.db"
    os.mkfifo(fifo)                       # opening it would block
    for p in (link, zero, fifo):
        with pytest.raises(EvidenceUnusable, match="not a regular file"):
            open_evidence_db(p, tables=("sessions",))
    open_evidence_db(real, tables=("sessions",)).close()


def _refusal(tmp_path, harness: str, db: pathlib.Path):
    import subprocess  # noqa: PLC0415
    script = pathlib.Path(__file__).resolve().parent.parent / harness / f"preprocess_{harness}.py"
    out = tmp_path / f"{harness}-out.json"
    out.unlink(missing_ok=True)
    p = subprocess.run([sys.executable, str(script), str(db), str(out)],
                       capture_output=True, text=True, timeout=300)
    return p.returncode, p.stderr, out.exists()


def test_goose_and_opencode_refuse_an_unusable_store_with_exit_2(tmp_path):
    """Ruled 2026-10-28: a store the evidence reader refuses (a generated
    column, a value longer than the length limit) ends the conversion with an
    "error:" line and exit 2 and no output, not a traceback; an honest store
    converts as before."""
    import sqlite_source
    adapters = pathlib.Path(__file__).resolve().parent.parent
    shipped = {"goose": ("goose/regression/1.33.1/inputs/s1-fetch.db", "messages", "content_json"),
               "opencode": ("opencode/regression/1.17.15/inputs/s1-config.db", "part", "data")}
    for harness, (rel, table, column) in shipped.items():
        honest = tmp_path / f"{harness}-honest.db"
        shutil.copyfile(adapters / rel, honest)
        rc, err, wrote = _refusal(tmp_path, harness, honest)
        assert rc == 0 and wrote, (harness, rc, err[-300:])

        generated = tmp_path / f"{harness}-generated.db"
        shutil.copyfile(adapters / rel, generated)
        c = sqlite3.connect(generated)
        c.execute(f"ALTER TABLE {table} ADD COLUMN g TEXT GENERATED ALWAYS AS (printf('%.*c', 1000, 'x')) VIRTUAL")
        c.commit(); c.close()
        rc, err, wrote = _refusal(tmp_path, harness, generated)
        assert (rc, wrote) == (2, False), (harness, rc, err[-300:])
        assert "Traceback" not in err and "error:" in err and "generated column" in err, (harness, err[-300:])

        long_value = tmp_path / f"{harness}-long.db"
        shutil.copyfile(adapters / rel, long_value)
        c = sqlite3.connect(long_value)
        c.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = (SELECT min(rowid) FROM {table})",
                  ("x" * (sqlite_source.MAX_VALUE_BYTES + 1),))
        c.commit(); c.close()
        rc, err, wrote = _refusal(tmp_path, harness, long_value)
        assert (rc, wrote) == (2, False), (harness, rc, err[-300:])
        assert "Traceback" not in err, (harness, err[-300:])
        assert f"error: {long_value}: a value longer than {sqlite_source.MAX_VALUE_BYTES} bytes" in err, (harness, err[-300:])


def test_goose_and_opencode_refuse_a_store_sqlite_cannot_read_with_exit_2(tmp_path):
    """Ruled 2026-10-28: a file that is not a SQLite database, a malformed
    schema, or a store without the harness's tables ends with an "error:"
    line and exit 2 and no output, as the evidence reader's refusals do."""
    adapters = pathlib.Path(__file__).resolve().parent.parent
    shipped = {"goose": "goose/regression/1.33.1/inputs/s1-fetch.db",
               "opencode": "opencode/regression/1.17.15/inputs/s1-config.db"}
    not_a_db = tmp_path / "not-a-db.db"
    not_a_db.write_bytes(b"this is not a database\n" * 200)
    malformed = tmp_path / "malformed.db"
    c = sqlite3.connect(malformed)
    c.execute("CREATE TABLE sessions (id TEXT)")
    c.execute("PRAGMA writable_schema = ON")
    c.execute("UPDATE sqlite_master SET sql = 'CREATE VIEW sessions AS SELECT 1' WHERE name = 'sessions'")
    c.commit(); c.close()
    for harness, rel in shipped.items():
        other = tmp_path / f"other-{harness}.db"   # the other harness's store
        shutil.copyfile(adapters / shipped["opencode" if harness == "goose" else "goose"], other)
        if harness == "goose":
            # goose's own check comes first: no schema_version row, exit 3.
            rc, err, wrote = _refusal(tmp_path, harness, other)
            assert (rc, wrote) == (3, False) and "no schema_version row" in err, (rc, err[-300:])
        for case in (not_a_db, malformed) + ((other,) if harness == "opencode" else ()):
            rc, err, wrote = _refusal(tmp_path, harness, case)
            assert (rc, wrote) == (2, False), (harness, case.name, rc, err[-300:])
            assert "Traceback" not in err, (harness, case.name, err[-300:])
            article = "an" if harness == "opencode" else "a"
            assert f"error: {case}: cannot be read as {article} {harness} store" in err, (harness, case.name, err[-300:])
