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
    assert {
        "name": "one_table",
        "columns": ["id", "name"],
        "schema": "CREATE TABLE [one_table] (\n   [id] INTEGER PRIMARY KEY,\n   [name] TEXT\n)",
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

    restored = str(tmpdir / "restored.db")
    result = CliRunner().invoke(cli.cli, ["load", restored, str(output_dir)])
    assert result.exit_code == 0, result.output
    restored_conn = sqlite3.connect(restored)
    assert (
        restored_conn.execute(
            'INSERT INTO "odd name" (value) VALUES (?) RETURNING id', ("next",)
        ).fetchone()[0]
        == 4
    )
    assert (
        restored_conn.execute(
            'INSERT INTO "empty history" (value) VALUES (?) RETURNING id', ("first",)
        ).fetchone()[0]
        == 2
    )
    restored_conn.close()


def test_autoincrement_sequence_selection_exclude_replace_and_metadata_order(tmpdir):
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
