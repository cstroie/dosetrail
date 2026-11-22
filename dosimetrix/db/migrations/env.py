"""Alembic environment. The database URL comes from Dosimetrix settings (or -x url=...)."""

from __future__ import annotations

from alembic import context

from dosimetrix.db.models import Base
from dosimetrix.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    return x.get("url") or config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        engine = make_engine(_url() or None)
        with engine.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
