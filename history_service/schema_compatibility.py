"""Read-only semantic classification, not a pathname admission or migration API.

The caller owns connection validation, WAL visibility, lifecycle locking and
identity pinning through later use. This module never opens the source database,
changes its pragmas, executes its SQL, or reads application rows. A supported
shape is not an integrity check or permission to replace history.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3


@dataclass(frozen=True)
class SchemaClassification:
    state: str
    backfill_marker: int


class SchemaIncompatibility(ValueError):
    """Bounded public reason, deliberately separate from SQLite corruption."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"History schema is unsupported ({reason}).")


# Keep string literals intact: whitespace/case changes inside CHECK or trigger
# strings are semantic changes. Quoted identifiers and unquoted identifiers may
# compare equally; single-quoted strings never compare as identifiers.
_TOKEN = re.compile(r"'(?:(?:'')|[^'])*'|\"(?:(?:\"\")|[^\"])*\"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\d+|[^\s]", re.DOTALL)
MAX_SCHEMA_OBJECTS = 128
MAX_SCHEMA_SQL_BYTES = 65536


def _tokens(sql: str) -> tuple[str, ...]:
    result = []
    for token in _TOKEN.findall(sql):
        if token.startswith("'"):
            result.append(token)
        elif token.startswith(('"', '`', '[')):
            result.append(token[1:-1].replace('""', '"').lower())
        else:
            result.append(token.lower())
    return tuple(result)


def _definition(kind: str, sql: str) -> tuple:
    tokens = _tokens(sql)
    if kind != "table" or "(" not in tokens:
        return tokens
    start = tokens.index("(")
    depth = 0
    parts = []
    part = []
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token == ")" and depth == 0:
            parts.append(tuple(part))
            return tokens[:start], tuple(sorted(parts)), tokens[index + 1:]
        if token == "," and depth == 0:
            parts.append(tuple(part))
            part = []
            continue
        depth += (token == "(") - (token == ")")
        part.append(token)
    # SQLite normally rejects malformed CREATE text before returning it.
    raise SchemaIncompatibility("object_definition")


def _shape(connection: sqlite3.Connection) -> tuple:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name LIMIT ?",
        (MAX_SCHEMA_OBJECTS + 1,),
    ).fetchall()
    if len(rows) > MAX_SCHEMA_OBJECTS or sum(len(row[3] or "") for row in rows) > MAX_SCHEMA_SQL_BYTES:
        raise SchemaIncompatibility("schema_bounds")
    objects = []
    for kind, name, table, sql in rows:
        columns = ()
        if kind == "table":
            quoted = '"' + name.replace('"', '""') + '"'
            columns = tuple(sorted(
                tuple(row[1:]) for row in connection.execute(f"PRAGMA table_xinfo({quoted})")
            ))
        objects.append((kind, name, table, _definition(kind, sql or ""), columns))
    return tuple(objects)


def _statements(script: str):
    statement = ""
    for character in script:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


def _initialize_reference(connection: sqlite3.Connection, contract: dict):
    for statement in _statements(contract["schema"]):
        connection.execute(statement)
        yield _shape(connection)
    for table, columns in contract["optional_columns"].items():
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                yield _shape(connection)
    for sql in contract["identity_indexes"]:
        connection.execute(sql)
        yield _shape(connection)


@lru_cache(maxsize=1)
def _supported_shapes() -> dict[tuple, str]:
    contracts = json.loads(Path(__file__).with_name("schema_contracts.json").read_text(encoding="utf-8"))
    shapes = {}
    # Enumerate actual DDL/ALTER prefixes, never arbitrary subsets. Upgrades
    # start at each released initialized predecessor. Column order can differ.
    for target_index, target in enumerate(contracts):
        for predecessor in (None, *contracts[:target_index]):
            with closing(sqlite3.connect(":memory:")) as connection:
                if predecessor is not None:
                    for _ in _initialize_reference(connection, predecessor):
                        pass
                for shape in _initialize_reference(connection, target):
                    shapes.setdefault(shape, "intermediate")
                shapes[_shape(connection)] = "supported"
    return shapes


def classify_schema(connection: sqlite3.Connection) -> SchemaClassification:
    """Classify an already validated, query-only connection without mutating it.

    SQLite permission, contention and unreadable-database errors propagate as
    SQLite errors. Unsupported marker/shape errors never use a SQLite error type
    and contain no source path, SQL definition, object name or database row.
    The observed user_version 0/1 values only mark identity-backfill completion.
    """
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("History schema classification requires a query-only connection.")
    marker = connection.execute("PRAGMA user_version").fetchone()[0]
    if marker not in (0, 1):
        raise SchemaIncompatibility("backfill_marker")
    shape = _shape(connection)
    if not shape:
        return SchemaClassification("empty", marker)
    state = _supported_shapes().get(shape)
    if state is None:
        raise SchemaIncompatibility("object_definition")
    return SchemaClassification(state, marker)
