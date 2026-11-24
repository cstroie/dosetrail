"""Engine and session factory."""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, load_settings


def make_engine(url: str | None = None, echo: bool = False) -> Engine:
    url = url or load_settings().database_url
    kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 3600
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.isolation_level = None  # we issue BEGIN ourselves (below)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - trivial
            # take the write lock up front: a deferred transaction that later upgrades to
            # a writer fails at once with "database is locked" when another writer exists
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def from_settings(settings: Settings | None = None) -> tuple[Engine, sessionmaker[Session]]:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url, settings.echo_sql)
    return engine, make_sessionmaker(engine)
