"""FastAPI dependencies. ``current_principal`` is THE auth seam: replacing static-token
auth with real auth (OAuth/sessions/API keys per user) must only touch this function.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from clipforge.domain.values import Principal, Role, UserId, WorkspaceId
from clipforge.infra.config import Settings, get_settings
from clipforge.infra.db.session import session_factory


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def current_principal(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.api_token.get_secret_value()
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(
        user_id=UserId(UUID(settings.default_user_id)),
        workspace_id=WorkspaceId(UUID(settings.default_workspace_id)),
        role=Role.OWNER,
    )


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def db_session() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(db_session)]
