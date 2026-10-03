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

    def name_matches(left, right):
        return bool(
            conn.conn.execute("select ? = ? collate nocase", (left, right)).fetchone()[
                0
            ]
        )

    excluded_sequence = any(name_matches(name, "sqlite_sequence") for name in excluded)

    def is_excluded(name):
        return any(name_matches(name, excluded_name) for excluded_name in excluded)

    internal_requested = any(
        name_matches(name, "sqlite_sequence") for name in requested_tables
    )
    if all:
        tables = {
            name
            for name in conn.table_names()
            if not name_matches(name, "sqlite_sequence") and not is_excluded(name)
        }
    else:
        tables = {
            name
            for name in requested_tables
            if not name_matches(name, "sqlite_sequence") and not is_excluded(name)
        }
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
    sequence_requested = bool(tables) or internal_requested
    if (
        sequence_requested
        and not excluded_sequence
        and "sqlite_sequence" in conn.table_names()
    ):
        sequence_rows = [
            row
            for row in conn.conn.execute(
                "select name, seq from sqlite_sequence"
            ).fetchall()
            if row[0] in sequence_tables
            or (internal_requested and not is_excluded(row[0]))
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
        else:
            for filename in ("sqlite_sequence.metadata.json", "sqlite_sequence.ndjson"):
                stale = output / filename
                if stale.exists():
                    stale.unlink()
    elif (
        excluded_sequence
        or not sequence_requested
        or "sqlite_sequence" not in conn.table_names()
    ):
        for filename in ("sqlite_sequence.metadata.json", "sqlite_sequence.ndjson"):
            stale = output / filename
            if stale.exists():
                stale.unlink()


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
    metadatas = list(directory.glob("*.metadata.json"))
    metadata_infos = []
    sequence_rows = {}

    def sequence_key(name):
        for existing_name in sequence_rows:
            same_name = db.conn.execute(
                "select ? = ? collate nocase", (name, existing_name)
            ).fetchone()[0]
            if same_name:
                return existing_name
        return name

    for metadata in metadatas:
        info = json.loads(metadata.read_text())
        if info["name"] != "sqlite_sequence":
            metadata_infos.append((metadata, info))
            continue
        columns = info.get("columns")
        if (
            not isinstance(columns, list)
            or "name" not in columns
            or "seq" not in columns
        ):
            raise click.ClickException("Invalid sqlite_sequence metadata columns")
        ndjson = metadata.parent / metadata.stem.replace(".metadata", ".ndjson")
        positions = {column: index for index, column in enumerate(columns)}
        for line_number, line in enumerate(ndjson.open(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, list) or len(row) != len(columns):
                raise click.ClickException(
                    "Invalid sqlite_sequence row at line {}".format(line_number)
                )
            name, seq = row[positions["name"]], row[positions["seq"]]
            if (
                not isinstance(name, str)
                or not isinstance(seq, int)
                or isinstance(seq, bool)
            ):
                raise click.ClickException(
                    "sqlite_sequence rows require text names and integer seq values"
                )
            if seq < -(2**63) or seq > 2**63 - 1:
                raise click.ClickException(
                    "sqlite_sequence seq is outside signed 64-bit range"
                )
            key = sequence_key(name)
            if key in sequence_rows and sequence_rows[key] != seq:
                raise click.ClickException(
                    "Conflicting sqlite_sequence values for {!r}".format(name)
                )
            sequence_rows[key] = seq
    loaded_tables = set()
    for metadata, info in metadata_infos:
        columns = info["columns"]
        schema = info["schema"]
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
            for name, seq in sequence_rows.items():
                actual_name = next(
                    (
                        loaded_name
                        for loaded_name in loaded_tables
                        if db.conn.execute(
                            "select ? = ? collate nocase",
                            (name, loaded_name),
                        ).fetchone()[0]
                    ),
                    None,
                )
                if actual_name is not None:
                    db.conn.execute(
                        "DELETE FROM sqlite_sequence WHERE name = ?", (actual_name,)
                    )
                    db.conn.execute(
                        "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                        (actual_name, seq),
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
