"""Shared test fixtures.

Tests run against a real Postgres, not a fake. The runtime's whole subject is
what the database guarantees under concurrency and crashes, and none of that
survives being mocked out.
"""

from __future__ import annotations

import psycopg
import pytest

from deskhand import migrate, seed
from deskhand.config import settings


def _reseed() -> None:
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        seed.seed(cur)
        conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Bring the schema up to date once, then seed it once."""
    assert migrate.run() == 0, "migrations failed"
    _reseed()


@pytest.fixture
def fresh() -> None:
    """Reseed before this test. Request it from any test that writes."""
    _reseed()


@pytest.fixture
def northwind_id() -> str:
    from deskhand.db import fetch_one

    row = fetch_one("select id from orgs where slug = 'northwind'")
    assert row is not None
    return str(row["id"])


@pytest.fixture
def lumen_id() -> str:
    from deskhand.db import fetch_one

    row = fetch_one("select id from orgs where slug = 'lumen'")
    assert row is not None
    return str(row["id"])
