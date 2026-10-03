import click
import json
import pathlib
import sqlite_utils
import sqlite3
import sys


@click.group()
@click.version_option()
def cli():
    "Tools for dumping/loading a SQLite database to diffable directory structure"
    pass


@cli.command()
@click.argument(
    "dbpath",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    required=True,
)
@click.argument(
    "output", type=click.Path(file_okay=False, dir_okay=True), required=True
)
@click.argument("tables", nargs=-1, required=False)
@click.option("--all", is_flag=True, help="Dump all tables")
@click.option("--exclude", multiple=True, help="Tables to exclude from the dump")
def dump(dbpath, output, tables, all, exclude):
    """
    Dump a SQLite database out as flat files in the directory

    Usage:

        sqlite-diffable dump my.db output/ --all

    --all dumps every table. Or specify tables like this:

        sqlite-diffable dump my.db output/ entries tags

    Exclude specific tables:

        sqlite-diffable dump my.db output/ --all --exclude table1 --exclude table2
    """
    if not tables and not all:
        raise click.ClickException("You must pass --all or specify some tables")
    output = pathlib.Path(output)
    output.mkdir(exist_ok=True)
    conn = sqlite_utils.Database(dbpath)
    excluded = set(exclude)
    requested_tables = set(tables)
    if all:
        tables = set(conn.table_names()) - excluded - {"sqlite_sequence"}
    else:
        tables = requested_tables - excluded - {"sqlite_sequence"}
    # SQLite compares ASCII identifiers case-insensitively. Use the database's
    # spelling for sequence matching and for metadata, while leaving Unicode
    # identifiers distinct just as SQLite does.
    table_identities = {}
    for table in tables:
        actual = conn.conn.execute(
            "select name from sqlite_master "
            "where type in ('table', 'view') and name = ? collate nocase",
            (table,),
        ).fetchone()
        table_identities[table] = actual[0] if actual else table
    sequence_tables = set(table_identities.values())
    for table in tables:
        actual_table = table_identities[table]
        tablename = table.replace("/", "")
        filepath = output / "{}.ndjson".format(tablename)
        metapath = output / "{}.metadata.json".format(tablename)
        # Dump rows to filepath
        with filepath.open("w") as fp:
            for row in conn[actual_table].rows:
                fp.write(json.dumps(list(row.values()), default=repr) + "\n")
            fp.close()
        # Dump out metadata
        metapath.open("w").write(
            json.dumps(
                {
                    "name": table,
                    "columns": [c.name for c in conn[actual_table].columns],
                    "schema": conn[actual_table].schema,
                },
                indent=4,
            )
        )

    # sqlite_sequence is an internal table. Keep its existing dump format, but
    # only include counters for tables selected by this dump operation.
    # Querying sqlite_sequence for named tables lets AUTOINCREMENT selections
    # carry their counters without creating a sequence file for ordinary tables.
    sequence_requested = bool(tables) or "sqlite_sequence" in requested_tables
    if (
        sequence_requested
        and "sqlite_sequence" not in excluded
        and "sqlite_sequence" in conn.table_names()
    ):
        sequence_rows = [
            row
            for row in conn.conn.execute(
                "select name, seq from sqlite_sequence"
            ).fetchall()
            if row[0] in sequence_tables
            or ("sqlite_sequence" in requested_tables and row[0] not in excluded)
        ]
        if sequence_rows:
            with (output / "sqlite_sequence.ndjson").open("w") as fp:
                for name, seq in sequence_rows:
                    fp.write(json.dumps([name, seq], default=repr) + "\n")
            (output / "sqlite_sequence.metadata.json").write_text(
                json.dumps(
                    {
                        "name": "sqlite_sequence",
                        "columns": ["name", "seq"],
                        "schema": "CREATE TABLE sqlite_sequence(name,seq)",
                    },
                    indent=4,
                )
            )


@cli.command()
@click.argument(
    "dbpath",
    type=click.Path(file_okay=True, allow_dash=False, dir_okay=False),
)
@click.argument(
    "directory",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.option("--replace", is_flag=True, help="Replace tables if they exist already")
def load(dbpath, directory, replace):
    """
    Load flat files from a directory into a SQLite database

    Usage:

        sqlite-diffable load my.db dump-location/
    """
    db = sqlite_utils.Database(dbpath)
    directory = pathlib.Path(directory)
    metadatas = directory.glob("*.metadata.json")
    sequence_rows = []
    loaded_tables = set()
    for metadata in metadatas:
        info = json.loads(metadata.read_text())
        columns = info["columns"]
        schema = info["schema"]
        if info["name"] == "sqlite_sequence":
            ndjson = metadata.parent / metadata.stem.replace(".metadata", ".ndjson")
            positions = {column: index for index, column in enumerate(columns)}
            for line in ndjson.open():
                if line.strip():
                    row = json.loads(line)
                    sequence_rows.append(
                        (row[positions["name"]], row[positions["seq"]])
                    )
            continue
        if db[info["name"]].exists() and replace:
            db[info["name"]].drop()
        try:
            db.execute(schema)
        except sqlite3.OperationalError as ex:
            msg = str(ex)
            if "already exists" in msg:
                msg += "\n\nUse the --replace option to over-write existing tables"
            raise click.ClickException(msg)
        # Now insert the rows
        ndjson = metadata.parent / metadata.stem.replace(".metadata", ".ndjson")
        rows = (
            dict(zip(columns, json.loads(line)))
            for line in ndjson.open()
            if line.strip()
        )
        db[info["name"]].insert_all(rows)
        actual_name = db.conn.execute(
            "select name from sqlite_master "
            "where type in ('table', 'view') and name = ? collate nocase",
            (info["name"],),
        ).fetchone()
        loaded_tables.add(actual_name[0] if actual_name else info["name"])

    # Restore counters only after all ordinary tables and rows exist. DELETE
    # first avoids duplicates in old or hand-edited exports.
    if sequence_rows:
        with db.conn:
            for name, seq in sequence_rows:
                if name in loaded_tables:
                    db.conn.execute(
                        "DELETE FROM sqlite_sequence WHERE name = ?", (name,)
                    )
                    db.conn.execute(
                        "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                        (name, seq),
                    )


@cli.command()
@click.argument(
    "filepath",
    type=click.Path(file_okay=True, allow_dash=False, dir_okay=False, exists=True),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(file_okay=True, allow_dash=True, dir_okay=False),
)
@click.option(
    "--array",
    is_flag=True,
    help="Output JSON array instead of newline-delimited objects",
)
def objects(filepath, output, array):
    """
    Output rows from a .ndjson file as newline-delimited JSON objects

    Usage:

        sqlite-diffable objects dump-location/mytable.ndjson

    This will read the column names from the accompanying .metadata.json file.
    """
    if not filepath.endswith(".ndjson"):
        raise click.ClickException("Must be a .ndjson file")
    path = pathlib.Path(filepath)
    metadata = path.parent / (path.stem + ".metadata.json")
    if not metadata.exists():
        raise click.ClickException("No accompanying .metadata.json file")
    # Read the column names
    info = json.loads(metadata.read_text())
    columns = info["columns"]
    # Output the rows
    out = sys.stdout if output is None else open(output, "w")
    if array:
        out.write("[")
    first = True
    for line in path.open():
        row = json.loads(line)
        if array and not first:
            out.write(",\n")
        else:
            out.write("\n")
        out.write(json.dumps(dict(zip(columns, row))))
        first = False
    if array:
        out.write("\n]\n")
    else:
        out.write("\n")
