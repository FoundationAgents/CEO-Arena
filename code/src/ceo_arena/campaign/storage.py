"""SQLite connection policy for durable Campaign state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike
from pathlib import Path


class SQLiteStorage:
    """Open short-lived SQLite connections with one strict durability policy."""

    def __init__(self, database: str | PathLike[str], *, busy_timeout_ms: int = 30_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(database)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(
        self,
        schema: str,
        *,
        schema_version: int,
    ) -> None:
        """Create the current schema; reject existing unsupported versions."""

        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version not in (0, schema_version):
                raise RuntimeError(
                    f"unsupported Campaign database schema version: {current_version}"
                )
            existing = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            ).fetchone()
            if current_version == 0 and existing is not None:
                raise RuntimeError("unversioned existing Campaign databases are unsupported")
            connection.executescript(schema)
            connection.execute(f"PRAGMA user_version = {schema_version}")
            connection.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one configured connection and always close it."""

        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=self.busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run one short write transaction with an immediate reservation."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
