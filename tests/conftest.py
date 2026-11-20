from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

os.environ.setdefault("CLIPFORGE_ENV", "test")
os.environ.setdefault("CLIPFORGE_API_TOKEN", "test-token")
os.environ.setdefault("CLIPFORGE_LOG_JSON", "false")

TEST_DB_URL = os.environ.get(
    "CLIPFORGE_TEST_DATABASE_URL",
    "postgresql+psycopg://clipforge:clipforge@127.0.0.1:5432/clipforge_test",
)


def _admin_url(url: str) -> str:
    return url.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """A fresh test database migrated with Alembic (not create_all) to test migrations too."""
    dbname = TEST_DB_URL.rsplit("/", 1)[1]
    try:
        admin = create_engine(_admin_url(TEST_DB_URL), isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{dbname}"'))
        admin.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable: {type(exc).__name__}")

    from alembic import command
    from alembic.config import Config

    os.environ["CLIPFORGE_DATABASE_URL"] = TEST_DB_URL
    from clipforge.infra.config import get_settings

    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")

    engine = create_engine(TEST_DB_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    """Each test runs in a transaction that is rolled back."""
    conn = db_engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()
