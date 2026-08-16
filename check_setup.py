#!/usr/bin/env python
"""Preflight: tell me what is and is not wired up, before I waste a run on it.

    python check_setup.py

Exits nonzero only for things that actually stop the app. A missing model key
is reported, not failed — running keyless against the scripted mock provider
is a supported mode, not a broken install.
"""

from __future__ import annotations

import sys

OK, WARN, FAIL = "  ok  ", " note ", " fail "


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    failures = 0

    # ruff flags this as dead code because the project targets 3.11+. It is not:
    # this script exists to be run by someone whose interpreter might be older,
    # and a clear message beats a SyntaxError from somewhere deeper.
    if sys.version_info < (3, 11):  # noqa: UP036
        line(FAIL, "python", f"3.11+ required, running {sys.version.split()[0]}")
        failures += 1
    else:
        line(OK, "python", sys.version.split()[0])

    try:
        import psycopg  # noqa: F401

        from deskhand.config import settings
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "imports", f"{type(exc).__name__}: {exc}")
        print("\nrun: pip install -r requirements.txt && pip install -e .")
        return 1
    line(OK, "imports", "deskhand package importable")

    try:
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            version = conn.execute("select version()").fetchone()[0]
        line(OK, "database", version.split(",")[0])
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "database", f"{type(exc).__name__}: {exc}")
        print("\nrun: docker compose up -d db")
        failures += 1
    else:
        try:
            with psycopg.connect(settings.database_url) as conn:
                n = conn.execute(
                    "select count(*) from information_schema.tables"
                    " where table_schema = 'public'"
                ).fetchone()[0]
            if n:
                line(OK, "schema", f"{n} table(s)")
            else:
                line(WARN, "schema", "empty — run: python -m deskhand.migrate")
        except Exception as exc:  # noqa: BLE001
            line(WARN, "schema", f"could not inspect: {exc}")

    if settings.has_model_key:
        line(OK, "model", f"{settings.model_id}, effort={settings.model_effort}")
    else:
        line(WARN, "model", "no ANTHROPIC_API_KEY — runs will use the scripted mock")

    line(OK, "tracing", "runs are traced in the step log; events go to stdout")

    print()
    if failures:
        print(f"{failures} blocking problem(s).")
        return 1
    print("ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
