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


def _cmd_openrem_dump(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .legacy.openrem.dump import Source, dump_all

    src = Source(db=args.db, ssh=args.ssh, sudo_postgres=not args.no_sudo)
    dump_all(src, Path(args.out), only=args.only)
    return 0


def _cmd_openrem_import(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .db.session import make_engine, make_sessionmaker
    from .legacy.openrem.importer import OpenREMImporter
    from .legacy.openrem.reconcile import reconcile, reconcile_by_year

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        stats = OpenREMImporter(session, Path(args.dump), args.institution).run()
        session.commit()
        for line in stats.lines():
            print(line)
        detail, _ = reconcile(session, Path(args.dump))
        lines, ok = reconcile_by_year(session, Path(args.dump))
    print("\nPer modality/year (informational; OpenREM CR/DX copies are relabelled):")
    print("\n".join(detail))
    print("\nPer year (authoritative):")
    print("\n".join(lines))
    print("\nRECONCILIATION", "OK" if ok else "FAILED")
    return 0 if ok else 2


def _cmd_openrem_reconcile(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .db.session import make_engine, make_sessionmaker
    from .legacy.openrem.reconcile import reconcile_by_year

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        lines, ok = reconcile_by_year(session, Path(args.dump))
    print("\n".join(lines))
    return 0 if ok else 2


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

    d = sub.add_parser("openrem-dump", help="dump an OpenREM database to CSV (read-only)")
    d.add_argument("--db", default="openremdb", help="PostgreSQL database name")
    d.add_argument("--ssh", help="run psql on this host via ssh, e.g. root@server")
    d.add_argument("--no-sudo", action="store_true", help="do not run psql as 'postgres'")
    d.add_argument("--out", default="var/openrem-dump", help="output directory")
    d.add_argument("--only", nargs="*", help="dump only these parts")
    d.set_defaults(func=_cmd_openrem_dump)

    im = sub.add_parser("openrem-import", help="import an OpenREM dump, then reconcile")
    im.add_argument("dump", nargs="?", default="var/openrem-dump")
    im.add_argument("--url")
    im.add_argument("--institution", default="Default", help="name of the local institution")
    im.set_defaults(func=_cmd_openrem_import)

    rc = sub.add_parser("openrem-reconcile", help="compare imported data with the dump summary")
    rc.add_argument("dump", nargs="?", default="var/openrem-dump")
    rc.add_argument("--url")
    rc.set_defaults(func=_cmd_openrem_reconcile)

    i = sub.add_parser("info", help="show configuration")
    i.set_defaults(func=_cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
