"""Fixtures: every DB test runs on SQLite and, when DOSETRAIL_TEST_DATABASE_URL points to
a MariaDB test database, on MariaDB too (schema is dropped and recreated per session)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from dosetrail.db.migrate import upgrade
from dosetrail.db.models import Base
from dosetrail.db.session import make_engine, make_sessionmaker

BACKENDS = ["sqlite"]
if os.environ.get("DOSETRAIL_TEST_DATABASE_URL"):
    BACKENDS.append("mariadb")


def _reset_mariadb(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"DROP TABLE IF EXISTS `{table.name}`"))
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


@pytest.fixture(scope="session", params=BACKENDS)
def engine(request, tmp_path_factory):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.sqlite3'}"
        eng = make_engine(url)
    else:
        url = os.environ["DOSETRAIL_TEST_DATABASE_URL"]
        eng = make_engine(url)
        _reset_mariadb(eng)
    upgrade(url)
    eng.info_url = url
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """A session wrapped in a transaction that is rolled back after the test."""
    conn = engine.connect()
    trans = conn.begin()
    sess = make_sessionmaker(engine)(bind=conn, join_transaction_mode="create_savepoint")
    yield sess
    sess.close()
    trans.rollback()
    conn.close()
