"""`dosetrail` command-line interface (stdlib argparse)."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import load_settings


def _cmd_migrate(args: argparse.Namespace) -> int:
    from .db.migrate import upgrade

    url = args.url or load_settings().database_url
    upgrade(url, args.revision)
    print(f"database at {args.revision}")
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    from .db.session import make_engine, make_sessionmaker
    from .seeds.loader import load_all

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        report = load_all(session)
        session.commit()
    for line in report:
        print(line)
    return 0


def _cmd_info(_args: argparse.Namespace) -> int:
    s = load_settings()
    print(f"DoseTrail {__version__}")
    print(f"config file : {s.source or '(none)'}")
    print(f"database    : {_redact(s.database_url)}")
    return 0


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dosetrail", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="create/upgrade the database schema")
    m.add_argument("--url", help="database URL (default: from settings)")
    m.add_argument("--revision", default="head")
    m.set_defaults(func=_cmd_migrate)

    s = sub.add_parser("seed", help="load/refresh bundled vocabularies, group schemes, DRL sets")
    s.add_argument("--url")
    s.set_defaults(func=_cmd_seed)

    i = sub.add_parser("info", help="show configuration")
    i.set_defaults(func=_cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
