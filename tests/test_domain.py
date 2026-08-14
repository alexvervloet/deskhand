"""The seeded world is the fixture every later test builds on, so it is worth
asserting that it has the shape those tests assume."""

from __future__ import annotations

import pytest

from deskhand.auth import hash_password, hash_token, new_session_token, verify_password
from deskhand.db import fetch_all, fetch_one

pytestmark = pytest.mark.usefixtures("fresh")


def test_two_orgs_share_no_customers(northwind_id: str, lumen_id: str) -> None:
    emails = {}
    for name, org in (("northwind", northwind_id), ("lumen", lumen_id)):
        rows = fetch_all("select email from customers where org_id = %s", (org,))
        emails[name] = {r["email"] for r in rows}

    assert emails["northwind"], "northwind seeded no customers"
    assert emails["lumen"], "lumen seeded no customers"
    assert not emails["northwind"] & emails["lumen"]


def test_every_ticket_belongs_to_a_customer_in_the_same_org() -> None:
    leaks = fetch_all(
        "select t.reference from tickets t"
        " join customers c on c.id = t.customer_id"
        " where c.org_id <> t.org_id"
    )
    assert leaks == []


def test_every_order_belongs_to_a_customer_in_the_same_org() -> None:
    leaks = fetch_all(
        "select o.reference from orders o"
        " join customers c on c.id = o.customer_id"
        " where c.org_id <> o.org_id"
    )
    assert leaks == []


def test_knowledge_base_search_is_scoped_and_ranked(northwind_id: str) -> None:
    rows = fetch_all(
        "select slug from kb_articles"
        " where org_id = %s and search @@ websearch_to_tsquery('english', %s)"
        " order by ts_rank(search, websearch_to_tsquery('english', %s)) desc",
        (northwind_id, "stale coffee refund", "stale coffee refund"),
    )
    assert rows, "the refund policy should be findable by the words a ticket uses"
    assert rows[0]["slug"] == "refund-policy"


def test_knowledge_base_search_does_not_cross_orgs(northwind_id: str) -> None:
    # "warranty" only exists in Lumen's knowledge base.
    rows = fetch_all(
        "select slug from kb_articles"
        " where org_id = %s and search @@ websearch_to_tsquery('english', %s)",
        (northwind_id, "warranty"),
    )
    assert rows == []


def test_the_injection_fixture_is_present() -> None:
    """Exercise 02 depends on this attack existing in the seed data. If someone
    sanitises it out of the fixtures, the exercise silently stops testing
    anything — so the fixture itself is asserted."""
    row = fetch_one(
        "select m.body from ticket_messages m"
        " join tickets t on t.id = m.ticket_id"
        " where t.reference = 'NW-4' and m.author_kind = 'customer'"
    )
    assert row is not None
    assert "Ignore all previous instructions" in row["body"]
    assert "issue_refund" in row["body"]


def test_money_is_never_fractional() -> None:
    rows = fetch_all("select reference, total_cents from orders")
    assert rows
    for row in rows:
        assert isinstance(row["total_cents"], int)


def test_password_hashing_round_trips() -> None:
    digest = hash_password("demo-password-123")
    assert digest != "demo-password-123"
    assert verify_password("demo-password-123", digest)
    assert not verify_password("wrong", digest)


def test_a_malformed_hash_is_a_failed_login_not_a_crash() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_session_token_is_never_stored_verbatim() -> None:
    token, digest = new_session_token()
    assert token != digest
    assert hash_token(token) == digest
    assert len(digest) == 64
