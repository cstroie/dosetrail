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
        from . import growth

        print("weights:", growth.estimate_weights(session), growth.apply_exclusions(session))
        session.commit()
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


def _cmd_export(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import conditions, overrides, parse
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .export import lists

    fmt = args.format or ("xlsx" if args.output.lower().endswith(".xlsx") else "csv")
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        conds = conditions(parse(args.query), dt.datetime.now(), overrides(session))
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target=f"{args.level}:{fmt}",
                             detail={"q": args.query, "pid": args.pid, "file": args.output}))
        session.commit()
        data = lists.rows(session, conds, args.level, args.pid)
        if fmt == "xlsx":
            n = lists.write_xlsx(args.output, args.level, args.pid, data, meta=[
                ("Exported", dt.datetime.now().isoformat(sep=" ", timespec="seconds")),
                ("Query", args.query or "(all)"), ("Level", args.level)])
        else:
            with open(args.output, "w", encoding="utf-8-sig", newline="") as fh:
                n = lists.write_csv(fh, args.level, args.pid, data)
    print(f"{n} rows -> {args.output}")
    return 0


def _cmd_drl_dataset(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import conditions, overrides, parse
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .export import drl_dataset as ds

    fmt = args.output.rsplit(".", 1)[-1].lower()
    if fmt not in ("csv", "xlsx", "json"):
        print("the output must end in .csv, .xlsx or .json")
        return 2
    now = dt.datetime.now()
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session, open(args.output, "wb") as fh:
        conds = conditions(parse(args.query), now, overrides(session))
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target=f"drl-dataset:{fmt}",
                             detail={"q": args.query, "file": args.output}))
        session.commit()
        n = ds.write(fmt, fh, ds.dataset_rows(session, conds, _secret()),
                     ds.metadata(args.query, now))
    print(f"{n} rows -> {args.output}")
    return 0


def _cmd_survey_export(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import conditions, overrides, parse
    from .db.models import AuditLog, Study
    from .db.session import make_engine, make_sessionmaker
    from .export import drl_dataset as ds
    from .export import survey

    fmt = args.output.rsplit(".", 1)[-1].lower()
    if fmt not in ("csv", "xlsx", "json"):
        print("the output must end in .csv, .xlsx or .json")
        return 2
    start, end = survey.month_range(args.month)
    now = dt.datetime.now()
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session, open(args.output, "wb") as fh:
        conds = [*conditions(parse(args.query), now, overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="export",
                             target=f"survey:{args.month}:{fmt}",
                             detail={"q": args.query, "file": args.output}))
        session.commit()
        meta = {**ds.metadata(args.query, now), "dataset": "dosetrail-paediatric-survey",
                "version": survey.SURVEY_VERSION, "month": args.month}
        n = ds.write(fmt, fh, survey.rows(session, conds, _secret()), meta, survey.DICTIONARY)
    print(f"{n} rows -> {args.output}")
    return 0


def _cmd_report_1255(args: argparse.Namespace) -> int:
    import datetime as dt
    import getpass

    from .api.data import conditions, overrides, parse
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker
    from .reports import insp1255

    now = dt.datetime.now()
    start, end = insp1255.quarter_range(args.year, args.quarter)
    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        conds = conditions(parse(args.query), now, overrides(session))
        report = insp1255.build(session, conds, start, end)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="report",
                             target=f"insp1255:{args.year}Q{args.quarter}:xlsx",
                             detail={"q": args.query, "file": args.output}))
        session.commit()
    with open(args.output, "wb") as fh:
        insp1255.write_xlsx(report, fh, {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                                         "Selecție": args.query})
    for s in report["sheets"]:
        print(f"{s.title}: {sum(r['total'] for r in s.rows)} examinations in "
              f"{sum(1 for r in s.rows if r['total'])} codes")
    print(f"unclassified studies: {report['notes']['unclassified']} -> {args.output}")
    return 0


def _cmd_growth(args: argparse.Namespace) -> int:
    from . import growth

    s = load_settings()
    directory = s.data_dir / "growth"
    if args.action == "install":
        for line in growth.install(directory):
            print(line)
        print(f"installed in {directory} (see SOURCE.txt for origin and terms)")
        return 0
    if args.action == "status":
        g = growth.tables(directory)
        print(f"{directory}: " + ("installed" if g else "NOT installed: dosetrail growth install"))
        return 0 if g else 1
    from .db.session import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(args.url or s.database_url))() as session:
        est = growth.estimate_weights(session)  # missing / implausible -> estimate
        stats = growth.apply_exclusions(session)
        session.commit()
    print("estimates:", est)
    print("DRL exclusions:", stats)
    return 0 if "error" not in stats else 1


def _cmd_cnp(args: argparse.Namespace) -> int:
    """Sex, birth date and exact ages from national IDs (offline; CNP > HIS > DICOM)."""
    import getpass

    from . import demographics, growth
    from .db.models import AuditLog
    from .db.session import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        counts, studies = demographics.backfill(session)
        if not args.apply:
            session.rollback()
            print("dry run (--apply writes):", counts)
            return 0
        for i in range(0, len(studies), 5000):
            chunk = studies[i:i + 5000]
            growth.estimate_weights(session, chunk)
            growth.apply_exclusions(session, chunk)
        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="cnp_backfill",
                             target="patients", detail=counts))
        session.commit()
    print(counts)
    return 0


def _cmd_his(args: argparse.Namespace) -> int:
    """HIS servers: check the login; run the lookup worker; queue the history."""
    from .db.models import HisServer
    from .db.session import make_engine, make_sessionmaker
    from .his import servers

    s = load_settings()
    secret = _secret()
    with make_sessionmaker(make_engine(args.url or s.database_url))() as session:
        if args.action == "check":
            rows = list(session.query(HisServer).order_by(HisServer.id))
            if not rows:
                print("no HIS server configured (Admin → Integration)")
                return 1
            bad = 0
            for srv in rows:
                res = servers.check(session, secret, srv.id)
                bad += not res["ok"]
                print(f"{srv.name}: {'OK' if res['ok'] else 'FAILED'} {res['ms']} ms"
                      + (f" – {res['error']}" if res["error"] else ""))
            session.commit()
            return 1 if bad else 0
    return 2


def _cmd_refetch(args: argparse.Namespace) -> int:
    import datetime as dt

    from sqlalchemy import select

    from .db.models import AuditLog, Study
    from .db.session import make_engine, make_sessionmaker
    from .maintenance import forget_studies

    if not args.missing and not args.device:
        print("give --missing dap|dlp or --device ID")
        return 2
    kind, col = (("ct_rdsr", Study.total_dlp) if args.missing == "dlp"
                 else ("proj_rdsr", Study.total_dap))
    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        q = select(Study.id).where(Study.source_kind == kind)
        if args.missing:
            q = q.where(col.is_(None))
        if args.device:
            from .db.models import Device

            ids_ = [args.device, *session.scalars(select(Device.id).where(
                Device.merged_into_id == args.device))]
            q = q.where(Study.device_id.in_(ids_))
        if args.since:
            q = q.where(Study.study_datetime >= dt.datetime.fromisoformat(args.since))
        ids = list(session.scalars(q))
        if not args.apply:
            what = f"without {args.missing}" if args.missing else f"of device {args.device}"
            print(f"{len(ids)} {kind} studies {what} (dry run; --apply deletes them so that "
                  "qr fetches them again)")
            return 0
        out = forget_studies(session, ids)
        import getpass

        session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="refetch",
                             target=f"{kind}:{args.missing or f'device {args.device}'}",
                             detail=out))
        session.commit()
    print(f"forgot {out['studies']} studies ({out.get('objects', 0)} objects) on "
          f"{len(out['days'])} days: {', '.join(out['days'])}")
    print("run 'dosetrail qr' over these days to fetch them again")
    return 0


def _cmd_skin(args: argparse.Namespace) -> int:
    from . import skindose
    from .db.session import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(args.url or load_settings().database_url))() as session:
        if args.study:
            r = skindose.for_study(session, args.study)
            if r is None:
                print("study not found")
                return 1
            print(f"peak skin dose {r['psd_mgy']} mGy at {r['psd_at']} (estimate), "
                  f"Ka,r total {r['kar_total_mgy']} mGy, {r['events_used']} events, "
                  f"{r['geometries']} geometries")
            for k, v in r["assumptions"].items():
                print(f"  {k}: {v}")
            return 0
        print(skindose.update_psd(session))
        session.commit()
    return 0


def _cmd_alerts(args: argparse.Namespace) -> int:
    import datetime as dt

    from . import alerts, notify
    from .db.session import make_engine, make_sessionmaker

    s = load_settings()
    cfg = notify.NotifySettings.from_dict(s.notify)
    engine = make_engine(args.url or s.database_url)
    with make_sessionmaker(engine)() as session:
        if args.action == "evaluate":
            start = dt.datetime.fromisoformat(args.since) if args.since else None
            end = dt.datetime.fromisoformat(args.until) if args.until else None
            stats = alerts.evaluate_range(session, start, end, progress=print)
            print(", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
        elif args.action == "notify":
            stats = notify.dispatch(session, cfg)
            session.commit()
            print(dict(stats) or "nothing to send", "channels:", cfg.channels or "none")
        elif args.action == "test-notify":
            if not cfg.channels:
                print("no channel configured: set [notify] smtp_host + email_to, or ntfy_url")
                return 1
            m = notify.Message("[DoseTrail] Test notification",
                               "This is a test message from DoseTrail.", cfg.base_url, False)
            for ch in cfg.channels:
                try:
                    notify.SENDERS[ch](cfg, m)
                    print(f"{ch}: sent")
                except Exception as e:  # noqa: BLE001
                    print(f"{ch}: FAILED {e}")
        else:
            for a in alerts.listing(session, "open", 50):
                print(f"{a['time']}  {a['level']:5} {a['kind']:7} {a['message']}  "
                      f"[{a['title']}] study {a['study_id']}")
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    from . import classify
    from .db.session import make_engine, make_sessionmaker

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        stats = classify.run(session, args.study, progress=print)
        session.commit()
    print(", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
    return 0


def _cmd_openrem_fix(args: argparse.Namespace) -> int:
    import getpass

    from .db.session import make_engine, make_sessionmaker
    from .legacy.openrem.fixes import apply_known_fixes, pending

    engine = make_engine(args.url or load_settings().database_url)
    with make_sessionmaker(engine)() as session:
        if args.dry_run:
            todo = pending(session)
            for name, n in todo.items():
                print(f"{name}: {n} event(s) to correct")
            if not todo:
                print("nothing to correct")
            return 0
        for line in apply_known_fixes(session, username=f"cli:{getpass.getuser()}"):
            print(line)
        session.commit()
    return 0


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

    s = load_settings()
    host, port = args.host or s.web_host, args.port or s.web_port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {host}; put DoseTrail behind an HTTPS reverse proxy")
    # access_log off: query strings may contain search terms with identifiers.
    # X-Forwarded-For/-Proto are honoured only from the configured reverse proxies.
    uvicorn.run(create_app(), host=host, port=port, access_log=False,
                proxy_headers=True, forwarded_allow_ips=",".join(s.web_trusted_proxies))
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
    from .notify import NotifySettings

    worker = SpoolWorker(spool, maker, _secret(), keep=s.keep_received,
                         notify=NotifySettings.from_dict(s.notify))
    from .net import nodes

    with maker() as session:
        nodes.import_config(session, s)
        allowed = nodes.allowed_senders(session, s.dicom_allowed_aets)
        session.commit()
    rx = Receiver(spool, args.aet or s.dicom_aet, args.port or s.dicom_port,
                  args.bind or s.dicom_bind, allowed)
    worker.start()
    rx.start()
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())

    def status_loop():
        """Every 30 s: heartbeat for the UI, and the accepted senders from the database."""
        while not done.wait(30 if status_loop.ran else 0):
            status_loop.ran = True
            try:
                with maker() as session:
                    rx.set_allowed(nodes.allowed_senders(session, s.dicom_allowed_aets))
                    nodes.write_status(session, nodes.RECEIVER_KEY, {
                        "aet": rx.aet, "port": rx.port, "bind": rx.bind,
                        "received": rx.received, "by_aet": rx.by_aet,
                        "allowed": list(rx.ae.require_calling_aet), "spool": worker.stats})
                    session.commit()
            except Exception:  # noqa: BLE001 – status must never stop the receiver
                logging.getLogger("dosetrail.receive").exception("status update failed")

    status_loop.ran = False
    threading.Thread(target=status_loop, name="dosetrail-status", daemon=True).start()
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
    maker = make_sessionmaker(make_engine(args.url or s.database_url))
    if args.host:  # explicit command-line node
        if not args.aec:
            print("--host needs --aec (the PACS AE title)")
            return 2
        node = qr.Node(args.host, args.port or s.pacs_port, args.aec, args.aet or s.dicom_aet,
                       retrieve=args.retrieve or s.pacs_retrieve,
                       move_destination=s.pacs_move_destination,
                       max_results=s.pacs_max_results)
        node_mods = list(s.pacs_modalities)
    else:
        from .net import nodes

        with maker() as session:
            nodes.import_config(session, s)
            node, node_mods = nodes.qr_node(session, s, args.node)
            session.commit()
        if node is None:
            print(f"no query/retrieve node {args.node!r}" if args.node else
                  "no PACS configured: add a DICOM node (Admin > DICOM nodes) or [pacs]")
            return 2
        if args.aet:
            node.calling_aet = args.aet
        if args.retrieve:
            node.retrieve = args.retrieve
    mode = node.retrieve
    if mode not in ("get", "move"):
        print("retrieve mode must be 'get' or 'move'")
        return 2
    today = dt.date.today()
    if args.date_from:
        start = dt.date.fromisoformat(args.date_from)
        end = dt.date.fromisoformat(args.date_to) if args.date_to else today
    else:
        start, end = today - dt.timedelta(days=args.days - 1), today
    worker = SpoolWorker(Spool(s.spool_dir), maker, _secret(), keep=s.keep_received)

    def drain() -> int:
        n = total = 0
        while (n := worker.drain()):
            total += n
        return total

    mods = {m.strip().upper() for m in args.modalities.split(",")} if args.modalities else None
    configured = {m.upper() for m in node_mods}
    from .net.nodes import QR_KEY, write_status

    last = [0.0]

    def report(message: str, force: bool = False) -> None:
        """Print, and every 15 s store the progress for the UI (DICOM nodes page)."""
        import time

        print(message, flush=True)
        if force or time.monotonic() - last[0] > 15:
            last[0] = time.monotonic()
            try:
                with maker() as session:
                    write_status(session, QR_KEY, {
                        "node": f"{node.aet}@{node.host}", "from": str(start), "to": str(end),
                        "message": message, "dry_run": bool(args.dry_run)})
                    session.commit()
            except Exception:  # noqa: BLE001 – status only
                pass
    receiver = None
    if mode == "move" and not args.dry_run and not _listening(s.dicom_port):
        from .net.scp import Receiver

        receiver = Receiver(worker.spool, s.dicom_aet, s.dicom_port, s.dicom_bind,
                            s.dicom_allowed_aets)
        receiver.start()
        print(f"C-MOVE: started a receiver on port {s.dicom_port} for this run")
    with maker() as session:
        done = set() if args.redo else qr.done_days(session)
    day = end
    while day >= start:  # newest first
        if day.isoformat() in done and day < today - dt.timedelta(days=1):
            report(f"{day}: already done")
        else:
            stats = qr.QRStats()
            qr.run_day(node, day, worker.spool, drain, maker, stats, mods or configured,
                       args.dry_run,
                       args.pause, progress=report,
                       workers=args.workers)
            report(f"{day}: {dict(stats.counts)}; imported {worker.stats}", force=True)
            # a day is finished only after a full run (no modality subset) without errors
            if not args.dry_run and mods is None and not stats.counts.get("study_errors"):
                with maker() as session:
                    qr.mark_day_done(session, day)
        day -= dt.timedelta(days=1)
    if receiver is not None:
        receiver.stop()
    report(f"finished {start}..{end}", force=True)
    return 0


def _listening(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


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

    ex = sub.add_parser("export", help="export studies or events as CSV or XLSX")
    ex.add_argument("-q", "--query", default="", help='query language, e.g. "modality:CT date:30d"')
    ex.add_argument("--level", choices=["study", "event"], default="study")
    ex.add_argument("--format", choices=["csv", "xlsx"], help="default: from the file name")
    ex.add_argument("--pid", action="store_true", help="include patient identifiers")
    ex.add_argument("-o", "--output", required=True)
    ex.add_argument("--url")
    ex.set_defaults(func=_cmd_export)

    dd = sub.add_parser("drl-dataset", help="export the DRL dataset (csv / xlsx / json)")
    dd.add_argument("-q", "--query", default="origin:local")
    dd.add_argument("-o", "--output", required=True, help="the extension sets the format")
    dd.add_argument("--url")
    dd.set_defaults(func=_cmd_drl_dataset)

    sv2 = sub.add_parser("survey-export", help="anonymised paediatric survey export (one month)")
    sv2.add_argument("--month", required=True, help="YYYY-MM")
    sv2.add_argument("-q", "--query", default="origin:local")
    sv2.add_argument("-o", "--output", required=True, help=".csv, .xlsx or .json")
    sv2.add_argument("--url")
    sv2.set_defaults(func=_cmd_survey_export)

    rp = sub.add_parser("report-1255", help="quarterly INSP report, Order 1255/2016 (xlsx)")
    rp.add_argument("--year", type=int, required=True)
    rp.add_argument("--quarter", type=int, required=True, choices=[1, 2, 3, 4])
    rp.add_argument("-q", "--query", default="origin:local")
    rp.add_argument("-o", "--output", required=True)
    rp.add_argument("--url")
    rp.set_defaults(func=_cmd_report_1255)

    gr = sub.add_parser("growth", help="weight-for-age tables: install (setup), or check "
                                       "(estimate missing / implausible weights, then "
                                       "DRL exclusions P5/P95)")
    gr.add_argument("action", choices=["install", "check", "status"])
    gr.add_argument("--url")
    gr.set_defaults(func=_cmd_growth)

    cn = sub.add_parser("cnp", help="national IDs (CNP): fill or correct sex and birth date, "
                                    "recompute exact ages (offline)")
    cn.add_argument("action", choices=["backfill"])
    cn.add_argument("--apply", action="store_true", help="write (default: dry run)")
    cn.add_argument("--url")
    cn.set_defaults(func=_cmd_cnp)

    hs = sub.add_parser("his", help="HIS / RIS: check the configured servers' login")
    hs.add_argument("action", choices=["check"])
    hs.add_argument("--url")
    hs.set_defaults(func=_cmd_his)

    fg = sub.add_parser("refetch", help="forget studies imported with missing doses so that "
                                         "qr retrieves them again (repair)")
    fg.add_argument("--missing", choices=["dap", "dlp"],
                    help="dose reports (proj_rdsr: dap, ct_rdsr: dlp) without that total")
    fg.add_argument("--device", type=int, help="all projection dose reports of this device id "
                                               "(e.g. to gain fields the parser now reads)")
    fg.add_argument("--since", help="only studies from this date (YYYY-MM-DD)")
    fg.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    fg.add_argument("--url")
    fg.set_defaults(func=_cmd_refetch)

    sk = sub.add_parser("skin", help="estimate peak skin doses of fluoroscopy studies (batch)")
    sk.add_argument("--study", type=int, help="print the estimate for one study")
    sk.add_argument("--url")
    sk.set_defaults(func=_cmd_skin)

    al = sub.add_parser("alerts", help="evaluate alert rules, send notifications, list")
    al.add_argument("action", choices=["evaluate", "notify", "list", "test-notify"])
    al.add_argument("--since", help="evaluate studies from this date (YYYY-MM-DD)")
    al.add_argument("--until", help="evaluate studies before this date (YYYY-MM-DD)")
    al.add_argument("--url")
    al.set_defaults(func=_cmd_alerts)

    cl = sub.add_parser("classify", help="assign standard exam codes and DRL codes to studies")
    cl.add_argument("--study", type=int, action="append", help="only this study id (repeatable)")
    cl.add_argument("--url")
    cl.set_defaults(func=_cmd_classify)

    fx = sub.add_parser("openrem-fix", help="correct known OpenREM value errors (idempotent)")
    fx.add_argument("--url")
    fx.add_argument("--dry-run", action="store_true")
    fx.set_defaults(func=_cmd_openrem_fix)

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
    sv.add_argument("--host", help="address to listen on (default from settings: 127.0.0.1)")
    sv.add_argument("--port", type=int, help="port (default from settings: 8104)")
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
    q.add_argument("--node", help="DICOM node name (default: the first query/retrieve node)")
    q.add_argument("--from", dest="date_from", help="first study date YYYY-MM-DD")
    q.add_argument("--to", dest="date_to", help="last study date (default today)")
    q.add_argument("--days", type=int, default=2, help="without --from: last N days")
    q.add_argument("--modalities", help="comma list (default [pacs] modalities: "
                   "CT,DX,CR,RF,XA,MG,SR); queried one at a time")
    q.add_argument("--retrieve", choices=["get", "move"],
                   help="C-GET (default) or C-MOVE to our AE title")
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
