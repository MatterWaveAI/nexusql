"""
Logical backup and restore.

Two layers sit behind ``DatabaseManager.backup`` / ``restore`` and
``export_tables`` / ``import_tables``.

The native layer hands the work to whatever each database already ships:
SQLite's online backup API, ``pg_dump``/``pg_restore``, ``mysqldump``/
``mysql``, and SQL Server's ``BACKUP DATABASE``/``RESTORE DATABASE``. Those
produce the most faithful copies (schema, indexes, sequences, permissions)
but each needs something outside this package: a client binary on PATH, or
for SQL Server a path the *server* can write to.

The portable layer needs nothing but the driver. ``export_tables`` writes a
directory of JSON-lines files plus a manifest, and ``import_tables`` reads
it back with parameterised INSERTs, one transaction per table. It carries
rows only, not schema: the tables must already exist on the target, which
is what migrations are for. Because it is plain JSON it also works across
backends, which none of the native formats do.

Point-in-time recovery is deliberately not here. Replaying to a moment
between backups needs the server's own log stream (PostgreSQL WAL
archiving, MySQL binlogs, SQL Server log backups) and is configured on the
server, not from a client library.
"""

import base64
import datetime as dt
import decimal
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from .interfaces import DatabaseType

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "nexusql-export"
EXPORT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"

# Rows per executemany() call on import. The JSON file is read one line at
# a time, so this bounds the memory held between flushes rather than the
# whole table.
IMPORT_BATCH_SIZE = 1000


class BackupError(RuntimeError):
    """A backup or restore step failed; the message carries the tool's output."""


class BackupToolNotFound(BackupError):
    """A client binary the native backup path relies on is not on PATH."""


# ---------------------------------------------------------------------------
# Identifier and URL helpers
# ---------------------------------------------------------------------------

def quote_identifier(db_type: DatabaseType, name: str) -> str:
    """
    Quote a table or column name in the dialect's own style.

    A dotted name is quoted part by part so ``schema.table`` stays a
    schema-qualified reference rather than one odd identifier.
    """
    parts = name.split(".") if "." in name else [name]
    if db_type == DatabaseType.MYSQL:
        return ".".join("`" + p.replace("`", "``") + "`" for p in parts)
    if db_type == DatabaseType.MSSQL:
        return ".".join("[" + p.replace("]", "]]") + "]" for p in parts)
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def connection_fields(manager) -> Dict[str, Optional[str]]:
    """
    Host, port, user, password and database for a server URL.

    Matches how the connection factories in manager.py read the URL, with
    ConnectionConfig fields taking precedence, so the dump tool talks to
    the same server the manager does. Credentials are percent-decoded
    because the driver does that for the URL and the tools would not.
    """
    parsed = urlparse(manager.database_url)
    config = manager.config
    return {
        "host": config.host or parsed.hostname or "localhost",
        "port": str(config.port or parsed.port or ""),
        "user": config.username or (unquote(parsed.username) if parsed.username else None),
        "password": config.password or (unquote(parsed.password) if parsed.password else None),
        "database": config.database_name or parsed.path.lstrip("/"),
    }


def sqlite_path(manager) -> str:
    """The file behind a SQLite URL, mirroring _make_sqlite_connection."""
    url = manager.database_url
    if url.startswith("sqlite:///"):
        return url[10:]
    if url.startswith("sqlite://"):
        return url[9:]
    return url


def mssql_master_url(url: str) -> str:
    """
    The same server with ``master`` as the database.

    RESTORE cannot run from a session connected to the database being
    restored, so the restore step connects here instead. Both URL forms the
    MSSQL factory accepts are handled: a URL and a raw ODBC string.
    """
    if url.startswith("DRIVER=") or url.startswith("Driver="):
        if re.search(r"(?i)(^|;)\s*DATABASE\s*=", url):
            return re.sub(r"(?i)((?:^|;)\s*DATABASE\s*=)[^;]*", r"\1master", url)
        return url.rstrip(";") + ";DATABASE=master"
    parsed = urlparse(url)
    master = f"{parsed.scheme}://{parsed.netloc}/master"
    if parsed.query:
        master += f"?{parsed.query}"
    return master


def find_binary(name: str) -> str:
    """Absolute path of a client tool, or BackupToolNotFound naming it."""
    found = shutil.which(name)
    if found is None:
        raise BackupToolNotFound(
            f"{name} was not found on PATH. Install the database client tools "
            f"or add the directory containing {name} to PATH. The portable "
            "export_tables()/import_tables() need no external binary."
        )
    return found


def run_tool(argv: List[str], env: Dict[str, str], *, stdin=None) -> None:
    """
    Run a client tool and turn a non-zero exit into BackupError.

    The password rides in ``env`` only. Argument vectors are visible to every
    user on the machine through the process list, environment blocks are not.
    """
    try:
        completed = subprocess.run(
            argv, env=env, stdin=stdin, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise BackupToolNotFound(f"{argv[0]} could not be started: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise BackupError(
            f"{os.path.basename(argv[0])} exited with status "
            f"{completed.returncode}: {detail}"
        )


# ---------------------------------------------------------------------------
# Native backup and restore
# ---------------------------------------------------------------------------

def backup_database(manager, path: str, tables: Optional[List[str]] = None) -> str:
    """
    Write a native backup of the connected database to ``path``.

    Returns the path written. ``tables`` narrows a PostgreSQL or MySQL dump
    to those tables; SQLite and SQL Server backups are whole-database by
    nature, so passing ``tables`` there is an error rather than a silent
    full backup.
    """
    db_type = manager.config.database_type
    if db_type == DatabaseType.SQLITE:
        _reject_tables(tables, "SQLite")
        _sqlite_backup(manager, path)
    elif db_type == DatabaseType.POSTGRESQL:
        _postgres_backup(manager, path, tables)
    elif db_type == DatabaseType.MYSQL:
        _mysql_backup(manager, path, tables)
    elif db_type == DatabaseType.MSSQL:
        _reject_tables(tables, "SQL Server")
        _mssql_backup(manager, path)
    else:
        raise NotImplementedError(f"backup() is not implemented for {db_type.value}")
    return str(path)


def restore_database(manager, path: str, *, replace: bool = False) -> None:
    """
    Restore a backup written by ``backup_database`` into the connected database.

    ``replace`` drops objects that already exist before recreating them:
    ``pg_restore --clean --if-exists`` and SQL Server ``WITH REPLACE``.
    A MySQL dump carries its own ``DROP TABLE IF EXISTS`` and a SQLite
    restore overwrites the database file's contents, so those two always
    replace what they contain.
    """
    db_type = manager.config.database_type
    if db_type == DatabaseType.SQLITE:
        _sqlite_restore(manager, path)
    elif db_type == DatabaseType.POSTGRESQL:
        _postgres_restore(manager, path, replace)
    elif db_type == DatabaseType.MYSQL:
        _mysql_restore(manager, path)
    elif db_type == DatabaseType.MSSQL:
        _mssql_restore(manager, path, replace)
    else:
        raise NotImplementedError(f"restore() is not implemented for {db_type.value}")


def _reject_tables(tables, backend: str) -> None:
    if tables:
        raise ValueError(
            f"{backend} backups are whole-database; use export_tables() to "
            "copy a subset of tables"
        )


# --- SQLite -----------------------------------------------------------------

def _sqlite_backup(manager, path: str) -> None:
    # The online backup API copies pages under SQLite's own locking, so the
    # copy is consistent even while the manager keeps writing; a plain file
    # copy of a database with a hot journal or WAL would not be.
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with manager._borrow() as conn:
        target = sqlite3.connect(str(path))
        try:
            conn.backup(target)
        finally:
            target.close()


def _sqlite_restore(manager, path: str) -> None:
    # Backing up *from* the file *into* the live connection keeps every
    # handle valid and lets SQLite serialise against other readers, which
    # replacing the file underneath an open connection would not. It does
    # overwrite the whole database, as any restore does.
    if not Path(path).is_file():
        raise FileNotFoundError(f"backup file not found: {path}")
    if manager._in_transaction:
        raise BackupError("restore() cannot run inside an open transaction")
    source = sqlite3.connect(str(path))
    try:
        with manager._borrow() as conn:
            source.backup(conn)
    finally:
        source.close()


# --- PostgreSQL -------------------------------------------------------------

def postgres_env(manager) -> Dict[str, str]:
    """
    Environment for the libpq tools: the caller's environment plus PGPASSWORD.

    Inheriting the environment keeps PGSSLMODE and friends working for
    anyone who relies on them; only the password is added, never put on the
    command line.
    """
    env = dict(os.environ)
    password = connection_fields(manager)["password"]
    if password:
        env["PGPASSWORD"] = password
    return env


def postgres_dump_argv(manager, binary: str, path: str, tables: Optional[List[str]]) -> List[str]:
    fields = connection_fields(manager)
    argv = [binary, "--format=custom", "--no-password"]
    argv += _libpq_connection_flags(fields)
    argv.append(f"--file={path}")
    for table in tables or []:
        argv.append(f"--table={table}")
    return argv


def postgres_restore_argv(manager, binary: str, path: str, replace: bool) -> List[str]:
    fields = connection_fields(manager)
    argv = [binary, "--no-password"]
    argv += _libpq_connection_flags(fields)
    if replace:
        argv += ["--clean", "--if-exists"]
    argv.append(str(path))
    return argv


def _libpq_connection_flags(fields: Dict[str, Optional[str]]) -> List[str]:
    flags = [f"--host={fields['host']}"]
    if fields["port"]:
        flags.append(f"--port={fields['port']}")
    if fields["user"]:
        flags.append(f"--username={fields['user']}")
    flags.append(f"--dbname={fields['database']}")
    return flags


def _postgres_backup(manager, path: str, tables: Optional[List[str]]) -> None:
    binary = find_binary("pg_dump")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    run_tool(postgres_dump_argv(manager, binary, str(path), tables), postgres_env(manager))


def _postgres_restore(manager, path: str, replace: bool) -> None:
    if not Path(path).is_file():
        raise FileNotFoundError(f"backup file not found: {path}")
    binary = find_binary("pg_restore")
    run_tool(postgres_restore_argv(manager, binary, str(path), replace), postgres_env(manager))


# --- MySQL ------------------------------------------------------------------

def mysql_env(manager) -> Dict[str, str]:
    """The caller's environment plus MYSQL_PWD; see postgres_env."""
    env = dict(os.environ)
    password = connection_fields(manager)["password"]
    if password:
        env["MYSQL_PWD"] = password
    return env


def mysql_dump_argv(manager, binary: str, path: str, tables: Optional[List[str]]) -> List[str]:
    fields = connection_fields(manager)
    # --single-transaction gives a consistent InnoDB snapshot without
    # locking the tables. --protocol=TCP stops the client from preferring
    # a local socket when the URL said localhost:port.
    argv = [binary, "--single-transaction", "--protocol=TCP"]
    argv += _mysql_connection_flags(fields)
    argv.append(f"--result-file={path}")
    argv.append(fields["database"])
    argv += list(tables or [])
    return argv


def mysql_restore_argv(manager, binary: str) -> List[str]:
    fields = connection_fields(manager)
    argv = [binary, "--protocol=TCP"]
    argv += _mysql_connection_flags(fields)
    argv.append(fields["database"])
    return argv


def _mysql_connection_flags(fields: Dict[str, Optional[str]]) -> List[str]:
    flags = [f"--host={fields['host']}"]
    if fields["port"]:
        flags.append(f"--port={fields['port']}")
    if fields["user"]:
        flags.append(f"--user={fields['user']}")
    return flags


def _mysql_backup(manager, path: str, tables: Optional[List[str]]) -> None:
    binary = find_binary("mysqldump")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    run_tool(mysql_dump_argv(manager, binary, str(path), tables), mysql_env(manager))


def _mysql_restore(manager, path: str) -> None:
    if not Path(path).is_file():
        raise FileNotFoundError(f"backup file not found: {path}")
    binary = find_binary("mysql")
    with open(path, "rb") as dump:
        run_tool(mysql_restore_argv(manager, binary), mysql_env(manager), stdin=dump)


# --- SQL Server -------------------------------------------------------------

def mssql_backup_sql(database: str) -> str:
    return f"BACKUP DATABASE {quote_identifier(DatabaseType.MSSQL, database)} TO DISK = ?"


def mssql_restore_sql(database: str, replace: bool) -> str:
    options = "WITH RECOVERY, REPLACE" if replace else "WITH RECOVERY"
    return (
        f"RESTORE DATABASE {quote_identifier(DatabaseType.MSSQL, database)} "
        f"FROM DISK = ? {options}"
    )


def _run_mssql_admin(conn, sql: str, path: str) -> None:
    # BACKUP and RESTORE refuse to run inside a transaction, and pyodbc
    # opens one implicitly unless autocommit is on. The informational
    # result sets have to be drained too, or the statement is cancelled
    # when the cursor closes before the server has finished.
    previous = conn.autocommit
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, (str(path),))
            while cursor.nextset():
                pass
        finally:
            cursor.close()
    finally:
        conn.autocommit = previous


def _mssql_backup(manager, path: str) -> None:
    database = connection_fields(manager)["database"] or "master"
    with manager._borrow() as conn:
        _run_mssql_admin(conn, mssql_backup_sql(database), path)


def _open_manager(url: str):
    """A connected DatabaseManager for a side connection, or BackupError."""
    from .manager import DatabaseManager

    side = DatabaseManager(url, pool_min=1, pool_max=1)
    if not side.connect():
        raise BackupError(f"could not connect to {url.split('@')[-1]} for restore")
    return side


def _mssql_restore(manager, path: str, replace: bool) -> None:
    if manager._in_transaction:
        raise BackupError("restore() cannot run inside an open transaction")
    database = connection_fields(manager)["database"]
    if not database or database.lower() == "master":
        raise BackupError("restore() needs a URL that names the database to restore")

    # Our own pooled sessions would block the exclusive lock RESTORE takes,
    # so the pool is closed for the duration and rebuilt afterwards. Other
    # clients' sessions are left alone: forcing them off (SINGLE_USER WITH
    # ROLLBACK IMMEDIATE) is an operator's call, not a library's.
    was_connected = manager._pool is not None
    if was_connected:
        manager.disconnect()
    master = _open_manager(mssql_master_url(manager.database_url))
    try:
        with master._borrow() as conn:
            _run_mssql_admin(conn, mssql_restore_sql(database, replace), path)
    finally:
        master.disconnect()
        if was_connected:
            manager.connect()


# ---------------------------------------------------------------------------
# Portable JSON-lines export and import
# ---------------------------------------------------------------------------

# Values JSON cannot carry are written as one-key objects with a ``$``
# prefix. A real JSON column holding such an object would be misread on
# import; in practice column values are documents with several keys or
# keys that do not start with ``$``.
_TAG_BYTES = "$bytes"
_TAG_DATETIME = "$datetime"
_TAG_DATE = "$date"
_TAG_TIME = "$time"
_TAG_DECIMAL = "$decimal"
_TAG_UUID = "$uuid"


def encode_value(value: Any) -> Any:
    """JSON-safe form of a database value; ``json.dumps`` default hook."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_TAG_BYTES: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dt.datetime):
        return {_TAG_DATETIME: value.isoformat()}
    if isinstance(value, dt.date):
        return {_TAG_DATE: value.isoformat()}
    if isinstance(value, dt.time):
        return {_TAG_TIME: value.isoformat()}
    if isinstance(value, decimal.Decimal):
        return {_TAG_DECIMAL: str(value)}
    if isinstance(value, uuid.UUID):
        return {_TAG_UUID: str(value)}
    raise TypeError(f"export_tables cannot encode {type(value).__name__}")


def decode_value(obj: Dict[str, Any]) -> Any:
    """Inverse of encode_value; ``json.loads`` object hook."""
    if len(obj) != 1:
        return obj
    (key, raw), = obj.items()
    if key == _TAG_BYTES:
        return base64.b64decode(raw)
    if key == _TAG_DATETIME:
        return dt.datetime.fromisoformat(raw)
    if key == _TAG_DATE:
        return dt.date.fromisoformat(raw)
    if key == _TAG_TIME:
        return dt.time.fromisoformat(raw)
    if key == _TAG_DECIMAL:
        return decimal.Decimal(raw)
    if key == _TAG_UUID:
        return uuid.UUID(raw)
    return obj


def dumps_row(values: List[Any]) -> str:
    return json.dumps(values, default=encode_value, ensure_ascii=False, separators=(",", ":"))


def loads_row(line: str) -> List[Any]:
    return json.loads(line, object_hook=decode_value)


def discover_tables(manager) -> List[str]:
    """
    Base tables in the connected database's current schema, sorted.

    SQLite's own bookkeeping tables (sqlite_sequence and friends) are left
    out: they are rebuilt by the engine and INSERTing into them fails.
    """
    db_type = manager.config.database_type
    if db_type == DatabaseType.SQLITE:
        rows = manager.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r["name"] for r in rows]
    if db_type == DatabaseType.POSTGRESQL:
        where = "table_schema = current_schema()"
    elif db_type == DatabaseType.MYSQL:
        where = "table_schema = DATABASE()"
    elif db_type == DatabaseType.MSSQL:
        where = "table_schema = SCHEMA_NAME()"
    else:
        raise NotImplementedError(f"table discovery is not implemented for {db_type.value}")
    rows = manager.execute(
        "SELECT table_name AS name FROM information_schema.tables "
        f"WHERE table_type = 'BASE TABLE' AND {where} ORDER BY table_name"
    )
    return [r["name"] for r in rows]


def current_schema(manager) -> str:
    db_type = manager.config.database_type
    if db_type == DatabaseType.SQLITE:
        return "main"
    if db_type == DatabaseType.POSTGRESQL:
        sql = "SELECT current_schema() AS s"
    elif db_type == DatabaseType.MYSQL:
        sql = "SELECT DATABASE() AS s"
    elif db_type == DatabaseType.MSSQL:
        sql = "SELECT SCHEMA_NAME() AS s"
    else:
        return ""
    row = manager.fetch_one(sql)
    return (row or {}).get("s") or ""


def _row_values(row, columns: List[str]) -> List[Any]:
    # psycopg (dict_row) and pymysql (DictCursor) return mappings; sqlite3.Row
    # and pyodbc.Row index by position.
    if isinstance(row, dict):
        return [row[c] for c in columns]
    return list(row)


def _read_table(manager, table: str):
    """(columns, rows) for a whole table, columns from the cursor so an
    empty table still reports its shape."""
    quoted = quote_identifier(manager.config.database_type, table)
    with manager._borrow() as conn:
        cursor = manager._execute_raw(f"SELECT * FROM {quoted}", None, conn)
        try:
            columns = [d[0] for d in cursor.description]
            rows = [_row_values(r, columns) for r in cursor.fetchall()]
        finally:
            cursor.close()
        if not manager._in_transaction:
            # psycopg opens a transaction for the SELECT; end it so the
            # snapshot is not held while the rows are written out.
            conn.rollback()
    return columns, rows


def _safe_filename(name: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "table"
    candidate = base
    n = 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{base}_{n}"
    taken.add(candidate.lower())
    return candidate + ".jsonl"


def export_tables(manager, path: str, tables: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Write ``tables`` (default: every base table) as JSON lines under ``path``.

    ``path`` becomes a directory holding ``manifest.json`` and one
    ``<table>.jsonl`` per table, each line a JSON array in manifest column
    order. Returns the manifest. Tables are read one statement at a time,
    so the export is not a single snapshot across tables; the native
    ``backup()`` is the consistent copy.
    """
    from . import __version__

    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    names = list(tables) if tables is not None else discover_tables(manager)

    manifest: Dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "nexusql_version": __version__,
        "dialect": manager.config.database_type.value,
        "schema": current_schema(manager),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tables": [],
    }
    taken: set = set()
    for name in names:
        columns, rows = _read_table(manager, name)
        filename = _safe_filename(name, taken)
        with open(out / filename, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(dumps_row(row))
                fh.write("\n")
        manifest["tables"].append(
            {"name": name, "file": filename, "columns": columns, "row_count": len(rows)}
        )

    with open(out / MANIFEST_NAME, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return manifest


def read_manifest(path: str) -> Dict[str, Any]:
    manifest_path = Path(path) / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} under {path}")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("format") != EXPORT_FORMAT:
        raise BackupError(f"{manifest_path} is not a {EXPORT_FORMAT} manifest")
    if manifest.get("format_version", 0) > EXPORT_FORMAT_VERSION:
        raise BackupError(
            f"{manifest_path} is format version {manifest['format_version']}; "
            f"this nexusql reads up to {EXPORT_FORMAT_VERSION}"
        )
    return manifest


def _iter_rows(file_path: Path) -> Iterable[List[Any]]:
    with open(file_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield loads_row(line)


def _adapt_for_insert(db_type: DatabaseType, value: Any) -> Any:
    # psycopg returns jsonb as dict/list but will not adapt them back
    # without a wrapper; the other drivers hand JSON columns over as text.
    if db_type == DatabaseType.POSTGRESQL and isinstance(value, (dict, list)):
        from psycopg.types.json import Jsonb
        return Jsonb(value)
    return value


def _mssql_has_identity(manager, table: str) -> bool:
    row = manager.fetch_one(
        "SELECT OBJECTPROPERTY(OBJECT_ID(:table), 'TableHasIdentity') AS has_identity",
        {"table": table},
    )
    return bool(row and row.get("has_identity"))


def _postgres_resync_sequences(manager, table: str, columns: List[str]) -> None:
    # Explicit key values do not move a SERIAL/IDENTITY sequence, so without
    # this the next plain INSERT would collide with an imported row. MySQL,
    # SQLite and SQL Server all advance their counters on explicit inserts.
    quoted = quote_identifier(DatabaseType.POSTGRESQL, table)
    for column in columns:
        row = manager.fetch_one(
            "SELECT pg_get_serial_sequence(:table, :column) AS seq",
            {"table": table, "column": column},
        )
        seq = row and row.get("seq")
        if not seq:
            continue
        qcol = quote_identifier(DatabaseType.POSTGRESQL, column)
        manager.fetch_one(
            f"SELECT setval(:seq, COALESCE((SELECT MAX({qcol}) FROM {quoted}), 1), "
            f"(SELECT MAX({qcol}) FROM {quoted}) IS NOT NULL) AS v",
            {"seq": seq},
        )


def _import_one_table(manager, entry: Dict[str, Any], file_path: Path, truncate: bool) -> int:
    db_type = manager.config.database_type
    table = entry["name"]
    columns = entry["columns"]
    quoted = quote_identifier(db_type, table)
    names = [f"c{i}" for i in range(len(columns))]
    insert = (
        f"INSERT INTO {quoted} ("
        + ", ".join(quote_identifier(db_type, c) for c in columns)
        + ") VALUES ("
        + ", ".join(f":{n}" for n in names)
        + ")"
    )
    translated = manager._translate_sql(insert)

    inserted = 0
    with manager.transaction():
        if truncate:
            # DELETE rather than TRUNCATE: it is transactional on every
            # backend, so a failed import leaves the old rows in place.
            manager.execute(f"DELETE FROM {quoted}")
        identity_insert = db_type == DatabaseType.MSSQL and _mssql_has_identity(manager, table)
        if identity_insert:
            manager.execute(f"SET IDENTITY_INSERT {quoted} ON")
        try:
            conn = manager._connection
            batch: List[Any] = []
            query = translated
            for row in _iter_rows(file_path):
                if len(row) != len(columns):
                    raise BackupError(
                        f"{file_path.name}: row {inserted + len(batch) + 1} has "
                        f"{len(row)} values, manifest lists {len(columns)} columns"
                    )
                params = {n: _adapt_for_insert(db_type, v) for n, v in zip(names, row)}
                query, converted = manager._convert_params(translated, params)
                batch.append(converted)
                if len(batch) >= IMPORT_BATCH_SIZE:
                    inserted += _flush(conn, query, batch)
                    batch = []
            if batch:
                inserted += _flush(conn, query, batch)
            if db_type == DatabaseType.POSTGRESQL:
                _postgres_resync_sequences(manager, table, columns)
        finally:
            if identity_insert:
                manager.execute(f"SET IDENTITY_INSERT {quoted} OFF")
    return inserted


def _flush(conn, query: str, batch: List[Any]) -> int:
    cursor = conn.cursor()
    try:
        cursor.executemany(query, batch)
    finally:
        cursor.close()
    return len(batch)


def import_tables(manager, path: str, truncate: bool = False,
                  tables: Optional[List[str]] = None) -> Dict[str, int]:
    """
    Re-insert an ``export_tables`` directory into the connected database.

    Each table is loaded in its own transaction, so a failure leaves the
    tables before it loaded and the failing one untouched. ``truncate``
    deletes existing rows first. ``tables`` restricts the load to those
    manifest entries. Returns rows inserted per table. The tables must
    already exist; the export carries data, not schema.
    """
    manifest = read_manifest(path)
    wanted = set(tables) if tables is not None else None
    counts: Dict[str, int] = {}
    for entry in manifest["tables"]:
        if wanted is not None and entry["name"] not in wanted:
            continue
        file_path = Path(path) / entry["file"]
        if not file_path.is_file():
            raise FileNotFoundError(f"{file_path} listed in manifest is missing")
        counts[entry["name"]] = _import_one_table(manager, entry, file_path, truncate)
        logger.info("Imported %d rows into %s", counts[entry["name"]], entry["name"])
    if wanted is not None:
        missing = wanted - set(counts)
        if missing:
            raise BackupError(
                f"tables not present in the export manifest: {', '.join(sorted(missing))}"
            )
    return counts
