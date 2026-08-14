"""Database access: a small connection pool and two context managers.

Deliberately thin. The interesting persistence rules — append-only steps,
lease-then-work, idempotent tool execution — live in the modules that own
them, not here.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
from psycopg.rows import DictRow, dict_row
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
        # The pool runs worker threads that outlive the interpreter's own
        # shutdown, which makes every short-lived script (seed, migrate, a
        # one-off query) end in a wall of "couldn't stop thread" warnings.
        # Closing it at exit is the whole fix.
        atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection[DictRow]]:
    """A pooled connection in its own transaction, committed on clean exit.

    Parameterised on DictRow to match the pool's row_factory, so callers can
    index rows by column name without the type checker objecting.
    """
    with pool().connection() as conn:
        # The row factory is set in the pool's kwargs, which the type checker
        # cannot see through, so the parameterisation is asserted here once
        # rather than at every call site.
        yield cast(psycopg.Connection[DictRow], conn)


@contextmanager
def cursor() -> Iterator[psycopg.Cursor[DictRow]]:
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
