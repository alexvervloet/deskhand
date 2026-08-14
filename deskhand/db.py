"""Database access: a small connection pool and two context managers.

Deliberately thin. The interesting persistence rules — append-only steps,
lease-then-work, idempotent tool execution — live in the modules that own
them, not here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from deskhand.config import settings

log = logging.getLogger("deskhand")

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """The process-wide pool, opened on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection in its own transaction, committed on clean exit."""
    with pool().connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """A cursor on a pooled connection. Commits on clean exit, rolls back on
    an exception — the default psycopg context-manager semantics, named here
    so call sites read as one unit of work."""
    with connection() as conn, conn.cursor() as cur:
        yield cur


# The pool is built with row_factory=dict_row, so every row really is a dict.
# mypy cannot see that through the pool's constructor kwargs, hence the casts.
def fetch_one(sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cast(dict | None, cur.fetchone())


def fetch_all(sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cast(list[dict], cur.fetchall())


def execute(sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
