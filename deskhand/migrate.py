"""Migration runner: apply every migrations/*.sql file, once, in order.

    python -m deskhand.migrate

Each file runs inside its own transaction and is recorded in
`schema_migrations` on success, so a re-run is a no-op and a failure part-way
through a file leaves nothing half-applied. Files are immutable once applied —
to change the schema, add a new one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import LiteralString, cast

import psycopg

from deskhand.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_BOOTSTRAP = """
create table if not exists schema_migrations (
    filename    text primary key,
    applied_at  timestamptz not null default now()
)
"""


def pending(conn: psycopg.Connection) -> list[Path]:
    conn.execute(_BOOTSTRAP)
    conn.commit()
    applied = {r[0] for r in conn.execute("select filename from schema_migrations").fetchall()}
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in applied]


def run() -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"no migrations directory at {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    with psycopg.connect(settings.database_url) as conn:
        todo = pending(conn)
        if not todo:
            print("schema up to date")
            return 0

        for path in todo:
            print(f"applying {path.name} ... ", end="", flush=True)
            try:
                # The one place SQL is not a literal. These are .sql files
                # committed to this repository and applied in filename order —
                # not input, and not reachable from a request. Everywhere else
                # the LiteralString requirement stands; see deskhand/db.py.
                # The cast is redundant to mypy and required by pyright: they
                # model LiteralString differently. Kept for the stricter of the
                # two, with the other silenced on this line only.
                conn.execute(
                    cast("LiteralString", path.read_text())  # type: ignore[redundant-cast]
                )
                conn.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - the message is the product here
                conn.rollback()
                print("failed")
                print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            print("ok")

    print(f"applied {len(todo)} migration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
