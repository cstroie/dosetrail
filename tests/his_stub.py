"""A tiny HippoBridge look-alike for tests: synthetic FHIR answers over real HTTP.

`Stub.patients` maps a national ID to a Patient resource; `Stub.requests` maps a patient id to
a list of ServiceRequest resources (full forms, also served by id). Wrong credentials get 401
on /api/whoami and – like the real gateway – an empty 404 on searches.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "dosetrail", "s3cret"
VITALS = "http://hl7.org/fhir/us/vitals/StructureDefinition/"
NS = "http://stub/fhir/NamingSystem/"


def patient(pid: str, national_id: str, gender="female", birth="2018-01-01", weight=None,
            height=None) -> dict:
    ext = [{"url": "hipocrateUrl", "valueUri": "http://stub/p"}]
    if weight is not None:
        ext.append({"url": VITALS + "body-weight", "valueString": weight})
    if height is not None:
        ext.append({"url": VITALS + "height", "valueString": height})
    return {"resourceType": "Patient", "id": pid, "gender": gender, "birthDate": birth,
            "identifier": [{"use": "official", "system": NS + "patient-cnp",
                            "value": national_id}], "extension": ext}


def request(rid: str, authored: str, *, national_id="", weight=None, height=None,
            region="Chest", ward="PEDIATRICS 1", indication="cough, fever",
            guideline="Chest / child / indicated", gender="F", birth="01.01.2018") -> dict:
    ext = [{"url": "patientGender", "valueString": gender},
           {"url": "patientBirthDate", "valueString": birth},
           {"url": "patientPregnant", "valueString": "Nu"}]
    if weight is not None:
        ext.append({"url": "patientWeight", "valueString": weight})
    if height is not None:
        ext.append({"url": "patientHeight", "valueString": height})
    notes = [{"text": ward}]
    if indication:
        notes.append({"text": indication, "category": [{"text": "clinical-indication"}]})
    if guideline:
        notes.append({"text": guideline, "category": [{"text": "guideline-decision"}]})
    return {"resourceType": "ServiceRequest", "id": rid, "status": "active", "intent": "order",
            "priority": "routine", "authoredOn": authored, "code": {"text": "RADIOGRAPHY"},
            "bodySite": [{"text": region}], "requester": {"display": "Dr Test"},
            "subject": {"display": "7y", "identifier": {"system": NS + "patient-cnp",
                                                         "value": national_id}},
            "identifier": [{"value": rid}], "note": notes, "extension": ext}


class Stub:
    def __init__(self):
        self.patients: dict[str, dict] = {}
        self.requests: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.fail_with: int | None = None
        self.broken_session = False  # login accepted, but whoami 404 and searches empty
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                stub.calls.append(u.path)
                if stub.fail_with:
                    return self._send(stub.fail_with, {"error": "busy"})
                good = self.headers.get("Authorization") == "Basic " + base64.b64encode(
                    f"{USER}:{PASSWORD}".encode()).decode()
                nf = {"resourceType": "OperationOutcome", "issue": [{"code": "not-found"}]}
                if u.path == "/fhir/Metadata":
                    return self._send(200, {"resourceType": "CapabilityStatement"})
                if u.path == "/api/whoami":
                    if good and stub.broken_session:
                        return self._send(404, {"message": "User identity not found"})
                    return self._send(200 if good else 401, {"user": USER} if good else {})
                if not good or stub.broken_session:
                    return self._send(404, nf)
                if u.path == "/fhir/Patient":
                    p = stub.patients.get(q.get("q", ""))
                    return self._send(200, p) if p else self._send(404, nf)
                if u.path.startswith("/fhir/Patient/"):
                    pid = u.path.rsplit("/", 1)[1]
                    p = next((p for p in stub.patients.values() if p["id"] == pid), None)
                    return self._send(200, p) if p else self._send(404, nf)
                if u.path == "/fhir/ServiceRequest":
                    if "type" not in q:
                        return self._send(400, {"error": "type required in tests"})
                    rs = stub.requests.get(q.get("patient", ""), [])
                    return self._send(200, {"resourceType": "Bundle", "total": len(rs),
                                            "entry": [{"resource": r} for r in rs]})
                if u.path.startswith("/fhir/ServiceRequest/"):
                    rid = u.path.rsplit("/", 1)[1]
                    r = next((r for rs in stub.requests.values() for r in rs if r["id"] == rid),
                             None)
                    return self._send(200, r) if r else self._send(404, nf)
                return self._send(404, nf)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
