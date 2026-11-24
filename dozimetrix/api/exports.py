"""The one pair of routes every spec is served through: list them, run one.

``GET /api/exports`` lists the specs this user may run (and, for an administrator, the files that
failed to load and the plugins). ``GET /api/exports/{id}?format=…&<params>`` runs one. Roles, the
``pid`` requirement of identifying fields and the audit row are enforced here, for every spec.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..specs import loader, registry, run, writers
from ..specs.loader import SpecError


def register(app, *, db, current_user, clock, file_response, audit, settings, secret) -> None:
    @app.get("/api/exports")
    def list_exports(user=Depends(current_user)):
        cat = loader.discover(settings.specs_dir)
        roles = user.role_set()
        out = []
        for s in cat.specs.values():
            try:
                run.check_roles(s, roles)
                allowed = True
            except run.Forbidden:
                allowed = False
            out.append({**s.public(), "allowed": allowed, "needs_pid": run.needs_pid(s)})
        admin = "admin" in roles
        return {"specs": out, "problems": cat.problems if admin else [],
                "plugins": getattr(app.state, "plugins", []) if admin else []}

    @app.get("/api/exports/{spec_id}")
    def run_export(spec_id: str, request: Request, format: str = "json",
                   user=Depends(current_user), session: Session = Depends(db)):
        cat = loader.discover(settings.specs_dir)
        spec = cat.specs.get(spec_id)
        if spec is None:
            raise HTTPException(404, "no such export")
        try:
            run.check_roles(spec, user.role_set())
        except run.Forbidden as e:
            raise HTTPException(403, str(e)) from e
        if format not in spec.formats:
            raise HTTPException(400, f"format is one of {', '.join(spec.formats)}")
        raw = {k: v for k, v in request.query_params.items() if k != "format"}
        now = clock()
        try:
            ctx = run.build(session, spec, raw, secret=secret, now=now,
                            username=user.display_name or user.username)
            result = run.run(ctx)
        except SpecError as e:
            raise HTTPException(400, str(e)) from e
        audit(session, request, user, "report", f"spec:{spec.id}@{spec.version}:{format}",
              {"params": raw, "pid": run.needs_pid(spec)})
        session.commit()
        if format == "json":
            return writers.as_json(result, ctx)
        return file_response(lambda fh: registry.WRITERS[format](result, fh, ctx), format,
                             spec.id, now)
