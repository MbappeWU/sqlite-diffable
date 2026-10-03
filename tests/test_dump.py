from click.testing import CliRunner
from sqlite_diffable import cli
import sqlite_utils
import sqlite3
import json
import pathlib


def test_dump(one_table_db, tmpdir):
    output_dir = tmpdir / "out"
    result = CliRunner().invoke(
        cli.cli, ["dump", one_table_db, str(output_dir), "one_table"]
    )
    assert result.exit_code == 0, result.output
    # out/ should now have a single file in it
    ndjson = output_dir / "one_table.ndjson"
    metadata = output_dir / "one_table.metadata.json"
    assert ndjson.exists()
    assert metadata.exists()
    assert [[1, "Stacey"], [2, "Tilda"], [3, "Bartek"]] == [
        json.loads(line) for line in ndjson.open()
    ]
    actual_schema = (
        sqlite3.connect(one_table_db)
        .execute("select sql from sqlite_master where name = 'one_table'")
        .fetchone()[0]
    )
    assert {
        "name": "one_table",
        "columns": ["id", "name"],
        "schema": actual_schema,
    } == json.load(metadata)


def test_dump_all(two_tables_db, tmpdir):
    output_dir = tmpdir / "out"
    result = CliRunner().invoke(
        cli.cli, ["dump", two_tables_db, str(output_dir), "--all"]
    )
    assert result.exit_code == 0, result.output
    assert (output_dir / "one_table.ndjson").exists()
    assert (output_dir / "one_table.metadata.json").exists()
    assert (output_dir / "second_table.ndjson").exists()
    assert (output_dir / "second_table.metadata.json").exists()


def test_dump_exclude(two_tables_db, tmpdir):
    output_dir = tmpdir / "out"
    result = CliRunner().invoke(
        cli.cli,
        ["dump", two_tables_db, str(output_dir), "--all", "--exclude", "second_table"],
    )
    assert result.exit_code == 0, result.output
    assert (output_dir / "one_table.ndjson").exists()
    assert (output_dir / "one_table.metadata.json").exists()
    assert not (output_dir / "second_table.ndjson").exists()
    assert not (output_dir / "second_table.metadata.json").exists()


def test_load(two_tables_db, tmpdir):
    output_dir = tmpdir / "out"
    restore_db = tmpdir / "restore.db"
    result = CliRunner().invoke(
        cli.cli, ["dump", str(two_tables_db), str(output_dir), "--all"]
    )
    assert result.exit_code == 0, result.output
    # Now load it again
    result2 = CliRunner().invoke(cli.cli, ["load", str(restore_db), str(output_dir)])
    assert result2.exit_code == 0, result2.output
    db = sqlite_utils.Database(str(restore_db))
    assert set(db.table_names()) == {"second_table", "one_table"}
    assert list(db["one_table"].rows) == [
        {"id": 1, "name": "Stacey"},
        {"id": 2, "name": "Tilda"},
        {"id": 3, "name": "Bartek"},
    ]
    assert list(db["second_table"].rows) == [
        {"id": 1, "name": "Cleo"},
    ]
    # Running load a second time should error
    result3 = CliRunner().invoke(cli.cli, ["load", str(restore_db), str(output_dir)])
    assert result3.exit_code == 1
    assert (
        "already exists\n\nUse the --replace option to over-write existing tables\n"
    ) in result3.output
    # Using --replace should work correctly
    (output_dir / "one_table.ndjson").write_text(
        '[1, "Stacey"]\n[2, "Tilda"]\n', "utf-8"
    )
    result4 = CliRunner().invoke(
        cli.cli, ["load", str(restore_db), str(output_dir), "--replace"]
    )
    assert result4.exit_code == 0
    assert list(db["one_table"].rows) == [
        {"id": 1, "name": "Stacey"},
        {"id": 2, "name": "Tilda"},
    ]


def test_autoincrement_dump_load_preserves_deleted_and_empty_history(tmpdir):
    source = str(tmpdir / "source.db")
    source_conn = sqlite3.connect(source)
    source_conn.executescript("""
        CREATE TABLE "odd name" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT
        );
        CREATE TABLE "empty history" (
            id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT
        );
        INSERT INTO "odd name" (value) VALUES ('one'), ('two'), ('three');
        INSERT INTO "empty history" (value) VALUES ('gone');
        DELETE FROM "odd name" WHERE id = 3;
        DELETE FROM "empty history";
        """)
    source_conn.commit()
    source_conn.close()
    output_dir = tmpdir / "out"
    assert (
        CliRunner()
        .invoke(cli.cli, ["dump", source, str(output_dir), "--all"])
        .exit_code
        == 0
    )
    assert sorted(
        json.loads(line) for line in (output_dir / "sqlite_sequence.ndjson").open()
    ) == [
        ["empty history", 1],
        ["odd name", 3],
    ]
    ordinary_conn = sqlite3.connect(source)
    ordinary_conn.execute("create table ordinary (value text)")
    ordinary_conn.commit()
    ordinary_conn.close()
    result = CliRunner().invoke(cli.cli, ["dump", source, str(output_dir), "ordinary"])
    assert result.exit_code == 0, result.output
    assert not (output_dir / "sqlite_sequence.metadata.json").exists()
    assert not (output_dir / "sqlite_sequence.ndjson").exists()

    # Recreate the counter dump for the restore assertions below.
    result = CliRunner().invoke(cli.cli, ["dump", source, str(output_dir), "--all"])
    assert result.exit_code == 0, result.output
    excluded_output = tmpdir / "excluded-sequence"
    result = CliRunner().invoke(
        cli.cli,
        ["dump", source, str(excluded_output), "--all", "--exclude", "SQLITE_SEQUENCE"],
    )
    assert result.exit_code == 0, result.output
    assert not (excluded_output / "sqlite_sequence.metadata.json").exists()

    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(output_dir)])
    assert result.exit_code == 0, result.output
    restored_conn = sqlite3.connect(restored)
    restored_conn.execute('INSERT INTO "odd name" (value) VALUES (?)', ("next",))
    assert restored_conn.execute("select last_insert_rowid()").fetchone()[0] == 4
    restored_conn.execute('INSERT INTO "empty history" (value) VALUES (?)', ("first",))
    assert restored_conn.execute("select last_insert_rowid()").fetchone()[0] == 2
    restored_conn.close()


def test_autoincrement_sequence_selection_exclude_replace_and_metadata_order(
    tmpdir, monkeypatch
):
    source = str(tmpdir / "source.db")
    conn = sqlite3.connect(source)
    conn.executescript("""
        CREATE TABLE selected (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        CREATE TABLE excluded (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        INSERT INTO selected (value) VALUES ('one'), ('two');
        INSERT INTO excluded (value) VALUES ('one'), ('two'), ('three');
        DELETE FROM selected WHERE id = 2;
        """)
    conn.commit()
    conn.close()
    output_dir = tmpdir / "selected"
    result = CliRunner().invoke(
        cli.cli,
        [
            "dump",
            source,
            str(output_dir),
            "selected",
            "excluded",
            "--exclude",
            "excluded",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (output_dir / "excluded.metadata.json").exists()
    assert json.loads((output_dir / "sqlite_sequence.ndjson").read_text("utf-8")) == [
        "selected",
        2,
    ]

    # Put sequence metadata first in directory iteration and rename its pair;
    # the loader uses the metadata name, so order cannot affect restoration.
    ordered_dir = tmpdir / "ordered"
    ordered_dir.mkdir()
    for path in pathlib.Path(str(output_dir)).iterdir():
        target = pathlib.Path(str(ordered_dir)) / (
            "0" + path.name if "sqlite_sequence" in path.name else "z" + path.name
        )
        target.write_bytes(path.read_bytes())
    restored = str(tmpdir / "restored.db")
    existing = sqlite3.connect(restored)
    existing.executescript("""
        CREATE TABLE selected (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        CREATE TABLE unrelated (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        INSERT INTO unrelated (value) VALUES ('keep');
        """)
    existing.commit()
    existing.close()
    result = CliRunner().invoke(
        cli.cli, ["load", restored, str(ordered_dir), "--replace"]
    )
    assert result.exit_code == 0, result.output
    db = sqlite_utils.Database(restored)
    assert list(db["selected"].rows) == [{"id": 1, "value": "one"}]
    assert list(db["unrelated"].rows) == [{"id": 1, "value": "keep"}]
    assert db.conn.execute(
        "select name, seq from sqlite_sequence order by name"
    ).fetchall() == [
        ("selected", 2),
        ("unrelated", 1),
    ]

    # Exercise the opposite metadata iteration order as well.
    original_glob = cli.pathlib.Path.glob

    def reverse_glob(path, pattern):
        return list(reversed(list(original_glob(path, pattern))))

    monkeypatch.setattr(cli.pathlib.Path, "glob", reverse_glob)
    reversed_restored = str(tmpdir / "reversed-restored.db")
    result = CliRunner().invoke(cli.cli, ["load", reversed_restored, str(ordered_dir)])
    assert result.exit_code == 0, result.output
    reversed_db = sqlite_utils.Database(reversed_restored)
    assert reversed_db.conn.execute(
        "select name, seq from sqlite_sequence"
    ).fetchall() == [("selected", 2)]


def test_autoincrement_mixed_case_alias_and_unicode_names(tmpdir):
    source = str(tmpdir / "source.db")
    conn = sqlite3.connect(source)
    conn.executescript("""
        CREATE TABLE "AuditLog" (id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE "Ä" (id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE "ä" (id INTEGER PRIMARY KEY AUTOINCREMENT);
        INSERT INTO "AuditLog" DEFAULT VALUES;
        INSERT INTO "Ä" DEFAULT VALUES;
        INSERT INTO "ä" DEFAULT VALUES;
        """)
    conn.commit()
    conn.close()
    output_dir = tmpdir / "out"
    result = CliRunner().invoke(
        cli.cli, ["dump", source, str(output_dir), "auditlog", "Ä"]
    )
    assert result.exit_code == 0, result.output
    assert sorted(
        json.loads(line) for line in (output_dir / "sqlite_sequence.ndjson").open()
    ) == [["AuditLog", 1], ["Ä", 1]]


def test_legacy_full_sequence_and_missing_counter_exports_are_safe(tmpdir):
    source = str(tmpdir / "source.db")
    conn = sqlite3.connect(source)
    conn.executescript("""
        CREATE TABLE selected (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        CREATE TABLE unrelated (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
        INSERT INTO selected (value) VALUES ('one'), ('two');
        INSERT INTO unrelated (value) VALUES ('keep');
        DELETE FROM selected WHERE id = 2;
        """)
    conn.commit()
    conn.close()
    dump_dir = tmpdir / "dump"
    assert (
        CliRunner()
        .invoke(cli.cli, ["dump", source, str(dump_dir), "selected"])
        .exit_code
        == 0
    )

    # A pre-existing legacy sequence file can contain counters for tables not
    # present in this load. Those rows must not alter the destination.
    sequence_metadata = dump_dir / "sqlite_sequence.metadata.json"
    sequence_metadata.write_text(
        json.dumps(
            {
                "name": "sqlite_sequence",
                "columns": ["name", "seq"],
                "schema": "CREATE TABLE sqlite_sequence(name,seq)",
            }
        ),
        "utf-8",
    )
    (dump_dir / "sqlite_sequence.ndjson").write_text(
        '["selected", 2]\n["unrelated", 99]\n', "utf-8"
    )
    restored = str(tmpdir / "restored.db")
    existing = sqlite3.connect(restored)
    existing.execute(
        "CREATE TABLE unrelated (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)"
    )
    existing.execute("INSERT INTO unrelated (value) VALUES ('keep')")
    existing.commit()
    existing.close()
    result = CliRunner().invoke(cli.cli, ["load", restored, str(dump_dir)])
    assert result.exit_code == 0, result.output
    restored_conn = sqlite3.connect(restored)
    assert restored_conn.execute(
        "select name, seq from sqlite_sequence order by name"
    ).fetchall() == [
        ("selected", 2),
        ("unrelated", 1),
    ]
    restored_conn.close()

    # Removing sequence files models an older export: available rows load and
    # SQLite chooses the next ID from the surviving rows.
    no_counter = tmpdir / "no-counter"
    no_counter.mkdir()
    for path in pathlib.Path(str(dump_dir)).iterdir():
        if "sqlite_sequence" not in path.name:
            (pathlib.Path(str(no_counter)) / path.name).write_bytes(path.read_bytes())
    old_restored = str(tmpdir / "old-restored.db")
    result = CliRunner().invoke(cli.cli, ["load", old_restored, str(no_counter)])
    assert result.exit_code == 0, result.output


def test_sequence_only_export_is_safe_noop(tmpdir):
    directory = tmpdir / "sequence-only"
    directory.mkdir()
    (directory / "sqlite_sequence.metadata.json").write_text(
        json.dumps(
            {
                "name": "sqlite_sequence",
                "columns": ["name", "seq"],
                "schema": "CREATE TABLE sqlite_sequence(name,seq)",
            }
        ),
        "utf-8",
    )
    (directory / "sqlite_sequence.ndjson").write_text('["not-loaded", 4]\n', "utf-8")
    result = CliRunner().invoke(
        cli.cli, ["load", str(tmpdir / "noop.db"), str(directory)]
    )
    assert result.exit_code == 0, result.output


def test_invalid_sequence_payload_is_rejected_before_table_changes(tmpdir):
    source = str(tmpdir / "source.db")
    conn = sqlite3.connect(source)
    conn.execute("create table selected (id integer primary key autoincrement)")
    conn.execute("insert into selected default values")
    conn.commit()
    conn.close()
    directory = tmpdir / "invalid"
    assert (
        CliRunner()
        .invoke(cli.cli, ["dump", source, str(directory), "selected"])
        .exit_code
        == 0
    )
    (directory / "sqlite_sequence.ndjson").write_text('["selected", "bad"]\n', "utf-8")
    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(directory)])
    assert result.exit_code == 1
    assert "integer seq values" in result.output
    assert (
        not sqlite3.connect(restored)
        .execute("select 1 from sqlite_master where name = 'selected'")
        .fetchone()
    )


def test_conflicting_sequence_duplicates_are_rejected(tmpdir):
    directory = tmpdir / "conflict"
    directory.mkdir()
    (directory / "sqlite_sequence.metadata.json").write_text(
        json.dumps(
            {
                "name": "sqlite_sequence",
                "columns": ["name", "seq"],
                "schema": "CREATE TABLE sqlite_sequence(name,seq)",
            }
        ),
        "utf-8",
    )
    (directory / "sqlite_sequence.ndjson").write_text(
        '["AuditLog", 1]\n["auditlog", 2]\n', "utf-8"
    )
    result = CliRunner().invoke(
        cli.cli, ["load", str(tmpdir / "conflict.db"), str(directory)]
    )
    assert result.exit_code == 1
    assert "Conflicting sqlite_sequence" in result.output
