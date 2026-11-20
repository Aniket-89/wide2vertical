"""DB integration: migrations apply, seed is idempotent, tenancy is enforced."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.domain.ids import new_id
from clipforge.domain.values import WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db import models as m
from clipforge.infra.db.repositories import (
    ALL_SCOPED_REPOS,
    CrossWorkspaceWrite,
    SourceRepo,
)
from clipforge.infra.seed import seed

pytestmark = pytest.mark.db


def _workspace(db: Session, slug: str) -> WorkspaceId:
    ws = m.Workspace(id=new_id(), name=slug, slug=slug)
    db.add(ws)
    db.flush()
    return WorkspaceId(ws.id)


def test_seed_is_idempotent(db: Session) -> None:
    s = get_settings()
    seed(db, s)
    seed(db, s)
    ws = UUID(s.default_workspace_id)
    assert db.scalar(select(func.count()).select_from(m.Workspace).where(m.Workspace.id == ws)) == 1
    assert (
        db.scalar(
            select(func.count()).select_from(m.Membership).where(m.Membership.workspace_id == ws)
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(m.ScoringProfile)
            .where(m.ScoringProfile.workspace_id == ws)
        )
        == 1
    )


def test_every_business_table_has_workspace_id() -> None:
    unscoped_by_design = {"workspaces", "users"}
    for table in m.Base.metadata.sorted_tables:
        if table.name in unscoped_by_design:
            continue
        assert "workspace_id" in table.c, f"{table.name} lacks workspace_id"


def test_no_cross_workspace_reads(db: Session) -> None:
    a, b = _workspace(db, "a"), _workspace(db, "b")
    src_a = SourceRepo(db, a).add(
        m.Source(workspace_id=a, platform="twitch", channel_handle="streamer")
    )

    repo_b = SourceRepo(db, b)
    assert repo_b.get(src_a.id) is None
    assert repo_b.list() == []
    assert repo_b.find("twitch", "streamer") is None
    assert SourceRepo(db, a).get(src_a.id) is not None

    # Every scoped repo starts empty for a fresh workspace, i.e. no leakage via list().
    for repo_cls in ALL_SCOPED_REPOS:
        assert list(repo_cls(db, b).list()) == [], repo_cls.__name__


def test_cross_workspace_write_is_refused(db: Session) -> None:
    a, b = _workspace(db, "wa"), _workspace(db, "wb")
    with pytest.raises(CrossWorkspaceWrite):
        SourceRepo(db, b).add(m.Source(workspace_id=a, platform="twitch", channel_handle="x"))


def test_soft_delete_hides_rows(db: Session) -> None:
    a = _workspace(db, "sd")
    repo = SourceRepo(db, a)
    src = repo.add(m.Source(workspace_id=a, platform="youtube", channel_handle="me"))
    assert repo.soft_delete(src.id)
    assert repo.get(src.id) is None
    assert repo.get(src.id, include_deleted=True) is not None
