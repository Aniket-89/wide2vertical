"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from clipforge.infra.config import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True, future=True)


def session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str | None = None, *, commit_on_error: bool = False) -> Iterator[Session]:
    """Commit on success, roll back on error.

    ``commit_on_error=True`` is for pipeline stages: they write their outputs last, so on
    failure the only pending writes are the job_run/audit records explaining the failure,
    which must survive.
    """
    session = session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        if commit_on_error:
            try:
                session.commit()
            except Exception:
                session.rollback()
        else:
            session.rollback()
        raise
    finally:
        session.close()
