"""Fixtures: every DB test runs on SQLite and, when [database] test_url in the config file
points to a MariaDB test database, on MariaDB too (the schema is dropped and recreated per
session)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from dosimetrix.config import load_settings
from dosimetrix.db.migrate import upgrade
from dosimetrix.db.models import Base
from dosimetrix.db.session import make_engine, make_sessionmaker

BACKENDS = ["sqlite"]
TEST_URL = load_settings().test_database_url
if TEST_URL:
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
        url = TEST_URL
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


@pytest.fixture(autouse=True)
def _no_explicit_config():
    """`dosimetrix --config` sets a process-wide path: never let one test's leak into the next."""
    yield
    from dosimetrix import config

    config.set_config_path(None)
