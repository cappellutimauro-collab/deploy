from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional deploy dependency
    psycopg = None
    dict_row = None


ID_TABLES = {
    "studios",
    "users",
    "slots",
    "service_types",
    "appointments",
    "payments",
    "events",
    "password_resets",
}


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def postgres_enabled() -> bool:
    return bool(database_url())


def postgres_dsn() -> str:
    dsn = database_url()
    if not dsn or "sslmode=" in dsn:
        return dsn
    parsed = urlsplit(dsn)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def db_error_types() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [sqlite3.Error]
    if psycopg is not None:
        errors.append(psycopg.Error)
    return tuple(errors)


def db_integrity_error_types() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [sqlite3.IntegrityError]
    if psycopg is not None:
        errors.append(psycopg.IntegrityError)
    return tuple(errors)


def db_operational_error_types() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [sqlite3.OperationalError]
    if psycopg is not None:
        errors.append(psycopg.OperationalError)
    return tuple(errors)


class StaticResult:
    def __init__(self, rows: Iterable[dict[str, Any]] | None = None, lastrowid: int | None = None):
        self._rows = list(rows or [])
        self.lastrowid = lastrowid

    def fetchone(self) -> dict[str, Any] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self._rows
        self._rows = []
        return rows


class PgResult:
    def __init__(self, cursor: Any, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self) -> dict[str, Any] | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()


class PgConnection:
    def __init__(self, dsn: str):
        if psycopg is None or dict_row is None:
            raise RuntimeError("DATABASE_URL configurato, ma psycopg non e installato")
        self._conn = psycopg.connect(dsn, row_factory=dict_row)

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> PgResult | StaticResult:
        handled = self._handle_sqlite_compat(sql)
        if handled is not None:
            return handled
        translated, wants_lastrowid = translate_sql(sql)
        cursor = self._conn.execute(translated, tuple(params or ()))
        lastrowid = None
        if wants_lastrowid:
            row = cursor.fetchone()
            lastrowid = int(row["id"]) if row and row.get("id") is not None else None
        return PgResult(cursor, lastrowid=lastrowid)

    def executemany(self, sql: str, params_seq: Iterable[Iterable[Any]]) -> None:
        translated, _ = translate_sql(sql, force_no_returning=True)
        with self._conn.cursor() as cursor:
            cursor.executemany(translated, [tuple(params or ()) for params in params_seq])

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def _handle_sqlite_compat(self, sql: str) -> StaticResult | None:
        stripped = sql.strip()
        lowered = stripped.lower()
        if lowered.startswith("pragma "):
            table_info = re.match(r"pragma\s+table_info\((?P<table>[a-zA-Z_][a-zA-Z0-9_]*)\)", stripped, flags=re.I)
            if table_info:
                rows = self._conn.execute(
                    """
                    SELECT column_name AS name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table_info.group("table"),),
                ).fetchall()
                return StaticResult(rows)
            return StaticResult([])
        if "sqlite_master" in lowered:
            return StaticResult([{"sql": ""}])
        return None


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    previous = ""
    for char in script:
        if char == "'" and not in_double and previous != "\\":
            in_single = not in_single
        elif char == '"' and not in_single and previous != "\\":
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        previous = char
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def translate_sql(sql: str, force_no_returning: bool = False) -> tuple[str, bool]:
    translated = sql.strip().rstrip(";")
    translated = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        translated,
        flags=re.I,
    )
    translated = re.sub(r"\bREAL\b", "DOUBLE PRECISION", translated, flags=re.I)

    insert_or_ignore = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", translated, flags=re.I))
    translated = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", translated, flags=re.I)
    if insert_or_ignore and " on conflict " not in f" {translated.lower()} ":
        translated = f"{translated} ON CONFLICT DO NOTHING"

    wants_lastrowid = False
    if not force_no_returning and " returning " not in f" {translated.lower()} ":
        match = re.match(r"\s*INSERT\s+INTO\s+(?P<table>[a-zA-Z_][a-zA-Z0-9_]*)\b", translated, flags=re.I)
        if match and match.group("table").lower() in ID_TABLES:
            translated = f"{translated} RETURNING id"
            wants_lastrowid = True

    return replace_qmarks(translated), wants_lastrowid


def replace_qmarks(sql: str) -> str:
    output: list[str] = []
    in_single = False
    in_double = False
    previous = ""
    for char in sql:
        if char == "'" and not in_double and previous != "\\":
            in_single = not in_single
        elif char == '"' and not in_single and previous != "\\":
            in_double = not in_double
        output.append("%s" if char == "?" and not in_single and not in_double else char)
        previous = char
    return "".join(output)


def connect_postgres() -> PgConnection:
    return PgConnection(postgres_dsn())
