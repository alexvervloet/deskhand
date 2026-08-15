"""Authentication and the tenant scope.

Two rules, enforced here rather than remembered at each call site:

1. A request is authenticated by a bearer token whose SHA-256 digest matches a
   live session row. The token itself is never stored, so this table is not a
   credential store.
2. Every authenticated request carries an org, and every query in the API
   filters on it. The scope is a value handlers receive, not a value they
   look up — a handler cannot forget to ask which merchant it is serving.

Approving an irreversible action is gated separately. `viewer` can watch a run
spend money and cannot authorise a penny of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from deskhand.auth import hash_token
from deskhand.db import fetch_one

APPROVER_ROLES = frozenset({"owner", "agent"})


@dataclass(frozen=True, slots=True)
class Caller:
    user_id: str
    email: str
    role: str
    org_id: str
    org_slug: str
    org_name: str

    @property
    def can_approve(self) -> bool:
        return self.role in APPROVER_ROLES


def current_caller(
    authorization: Annotated[str | None, Header()] = None,
) -> Caller:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    row = fetch_one(
        "select u.id, u.email, u.role::text as role, o.id as org_id, o.slug, o.name"
        "  from sessions s"
        "  join users u on u.id = s.user_id"
        "  join orgs o on o.id = u.org_id"
        " where s.token_hash = %s and s.expires_at > now()",
        (hash_token(token),),
    )
    if row is None:
        # One message for "no such token" and "expired token" alike: telling
        # the difference apart is useful to an attacker and to nobody else.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")

    return Caller(
        user_id=str(row["id"]),
        email=row["email"],
        role=row["role"],
        org_id=str(row["org_id"]),
        org_slug=row["slug"],
        org_name=row["name"],
    )


def require_approver(
    caller: Annotated[Caller, Depends(current_caller)],
) -> Caller:
    """Gate for the one action that commits the merchant to something."""
    if not caller.can_approve:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"role {caller.role!r} may watch runs but not approve irreversible actions",
        )
    return caller


CallerDep = Annotated[Caller, Depends(current_caller)]
ApproverDep = Annotated[Caller, Depends(require_approver)]
