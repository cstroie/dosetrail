"""`dosetrail` command-line interface (stdlib argparse)."""

from __future__ import annotations

import argparse
import contextlib
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
        secret = _secret()
        stats = OpenREMImporter(session, Path(args.dump), args.institution, secret).run()
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


def _cmd_recompute(args: argparse.Namespace) -> int:
    from .db.session import make_engine, make_sessionmaker
    from .maintenance import recompute_all

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        for line in recompute_all(session, _secret()):
            print(line)
        session.commit()
    return 0


def _secret() -> bytes:
    from .security import load_or_create_secret

    return load_or_create_secret(load_settings().secret_file)


ROLES = ("viewer", "exporter", "pid", "importer", "admin")


def _cmd_user(args: argparse.Namespace) -> int:
    import getpass

    from sqlalchemy import select

    from .db.models import User
    from .db.session import make_engine, make_sessionmaker
    from .security import hash_password

    roles = [r.strip() for r in (args.roles or "viewer").split(",") if r.strip()]
    bad = [r for r in roles if r not in ROLES]
    if bad:
        print(f"unknown role(s): {', '.join(bad)}; valid: {', '.join(ROLES)}")
        return 2
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        user = session.scalar(select(User).filter_by(username=args.username))
        if args.action == "list":
            for u in session.scalars(select(User).order_by(User.username)):
                print(f"{u.username:20} {u.roles:30} {'active' if u.is_active else 'disabled'}")
            return 0
        if args.action == "disable":
            if user:
                user.is_active = False
                session.commit()
            return 0
        password = args.password or getpass.getpass(f"password for {args.username}: ")
        if len(password) < 10:
            print("password must have at least 10 characters")
            return 2
        if user is None:
            user = User(username=args.username)
            session.add(user)
        user.password_hash = hash_password(password)
        user.roles = ",".join(roles)
        user.display_name = args.name or user.display_name or args.username
        user.is_active = True
        session.commit()
        print(f"user {args.username}: roles {user.roles}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {args.host}; put DoseTrail behind an HTTPS reverse proxy")
    # access_log off: query strings may contain search terms with identifiers
    uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


def _cmd_devices_origin(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from .db.models import Device
    from .db.session import make_engine, make_sessionmaker

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        devs = list(session.scalars(select(Device).order_by(Device.n_studies.desc())))
        local = [d for d in devs if (d.n_studies or 0) >= args.min_studies]
        for d in local:
            print(f"local    {d.id:5} {d.n_studies:8} {d.manufacturer} | {d.model_name} | "
                  f"{d.station_name} | {d.display_name}")
        print(f"{len(local)} local, {len(devs) - len(local)} external "
              f"(threshold {args.min_studies} studies)")
        if args.apply:
            for d in devs:
                d.is_local = d in local
            session.commit()
            print("applied")
        else:
            print("dry run; add --apply to save")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    from .db.session import make_engine, make_sessionmaker
    from .ingest.files import import_paths

    def progress(i, path, result):
        if args.verbose or result.status == "failed":
            print(f"{i:6} {result.status:10} {path} {result.message}")
        elif i % 500 == 0:
            print(f"{i} files…")

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        store = import_paths(session, args.paths, _secret(), progress=progress)
    for k, v in sorted(store.stats.items()):
        print(f"{k}: {v}")
    return 1 if store.stats.get("failed") else 0


def _cmd_receive(args: argparse.Namespace) -> int:
    import logging
    import signal
    import threading

    from .db.session import make_engine, make_sessionmaker
    from .net.scp import Receiver
    from .net.spool import Spool, SpoolWorker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    s = load_settings()
    spool = Spool(s.spool_dir)
    maker = make_sessionmaker(make_engine(args.url or s.database_url))
    worker = SpoolWorker(spool, maker, _secret(), keep=s.keep_received)
    rx = Receiver(spool, args.aet or s.dicom_aet, args.port or s.dicom_port,
                  args.bind or s.dicom_bind, s.dicom_allowed_aets)
    worker.start()
    rx.start()
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    with contextlib.suppress(KeyboardInterrupt):
        done.wait()
    rx.stop()
    worker.stop()
    worker.join(timeout=10)
    print(f"received {rx.received}; imported {worker.stats}")
    return 0


def _cmd_qr(args: argparse.Namespace) -> int:
    import datetime as dt
    import logging

    from .db.session import make_engine, make_sessionmaker
    from .net import qr
    from .net.spool import Spool, SpoolWorker

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    s = load_settings()
    host, aet = args.host or s.pacs_host, args.aec or s.pacs_aet
    if not host or not aet:
        print("PACS not configured: set [pacs] host/aet in dosetrail.toml or use --host/--aec")
        return 2
    node = qr.Node(host, args.port or s.pacs_port, aet, args.aet or s.dicom_aet)
    today = dt.date.today()
    if args.date_from:
        start = dt.date.fromisoformat(args.date_from)
        end = dt.date.fromisoformat(args.date_to) if args.date_to else today
    else:
        start, end = today - dt.timedelta(days=args.days - 1), today
    maker = make_sessionmaker(make_engine(args.url or s.database_url))
    worker = SpoolWorker(Spool(s.spool_dir), maker, _secret(), keep=s.keep_received)

    def drain() -> int:
        n = total = 0
        while (n := worker.drain()):
            total += n
        return total

    mods = {m.strip().upper() for m in args.modalities.split(",")} if args.modalities else None
    with maker() as session:
        done = set() if args.redo else qr.done_days(session)
    day = end
    while day >= start:  # newest first
        if day.isoformat() in done and day < today - dt.timedelta(days=1):
            print(f"{day}: already done", flush=True)
        else:
            stats = qr.QRStats()
            qr.run_day(node, day, worker.spool, drain, maker, stats, mods, args.dry_run,
                       args.pause, progress=lambda m: print(m, flush=True),
                       workers=args.workers)
            print(f"{day}: {dict(stats.counts)}; imported {worker.stats}", flush=True)
            # a day is finished only after a full run (no modality subset) without errors
            if not args.dry_run and mods is None and not stats.counts.get("study_errors"):
                with maker() as session:
                    qr.mark_day_done(session, day)
        day -= dt.timedelta(days=1)
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

    r = sub.add_parser("recompute", help="recompute derived study and device columns")
    r.add_argument("--url")
    r.set_defaults(func=_cmd_recompute)

    u = sub.add_parser("user", help="manage users")
    u.add_argument("action", choices=["set", "disable", "list"])
    u.add_argument("username", nargs="?", default="")
    u.add_argument("--roles", help=f"comma-separated: {', '.join(ROLES)}")
    u.add_argument("--name", help="display name")
    u.add_argument("--password", help="(insecure: visible in process list) prompt if omitted")
    u.add_argument("--url")
    u.set_defaults(func=_cmd_user)

    sv = sub.add_parser("serve", help="run the web UI and API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=_cmd_serve)

    do = sub.add_parser("devices-origin",
                        help="suggest local vs external devices by study volume")
    do.add_argument("--min-studies", type=int, default=100)
    do.add_argument("--apply", action="store_true")
    do.add_argument("--url")
    do.set_defaults(func=_cmd_devices_origin)

    ip = sub.add_parser("import", help="import DICOM files or folders (RDSR, DX/CR headers)")
    ip.add_argument("paths", nargs="+")
    ip.add_argument("-v", "--verbose", action="store_true")
    ip.add_argument("--url")
    ip.set_defaults(func=_cmd_import)

    rv = sub.add_parser("receive", help="run the DICOM receiver (C-STORE/C-ECHO SCP)")
    rv.add_argument("--aet", help="AE title (default from settings: DOSETRAIL)")
    rv.add_argument("--port", type=int, help="port (default 11112)")
    rv.add_argument("--bind", help="address to listen on (default 0.0.0.0)")
    rv.add_argument("--url")
    rv.set_defaults(func=_cmd_receive)

    q = sub.add_parser("qr", help="query/retrieve dose objects from the PACS (C-FIND/C-GET)")
    q.add_argument("--from", dest="date_from", help="first study date YYYY-MM-DD")
    q.add_argument("--to", dest="date_to", help="last study date (default today)")
    q.add_argument("--days", type=int, default=2, help="without --from: last N days")
    q.add_argument("--modalities", help="comma list, default all X-ray modalities")
    q.add_argument("--dry-run", action="store_true", help="query only, retrieve nothing")
    q.add_argument("--redo", action="store_true", help="also re-run days already done")
    q.add_argument("--pause", type=float, default=0.2, help="seconds between studies")
    q.add_argument("--workers", type=int, default=1,
                   help="studies retrieved in parallel (mind the PACS load)")
    q.add_argument("--host")
    q.add_argument("--port", type=int)
    q.add_argument("--aec", help="PACS AE title")
    q.add_argument("--aet", help="our AE title")
    q.add_argument("--url")
    q.set_defaults(func=_cmd_qr)

    i = sub.add_parser("info", help="show configuration")
    i.set_defaults(func=_cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
