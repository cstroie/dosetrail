"""Client for HippoBridge, a FHIR-shaped gateway to a hospital information system.

Only this module knows HippoBridge's resource shapes (its extension URLs and note
categories); the rest of Dosimetrix sees `HisPatient` and `HisRequest`.

Calls used (one patient, one study ≈ 3 calls):
  GET /fhir/Patient?q=<national id>          bare Patient (1 hit) | Bundle (several) | 404
  GET /fhir/Patient/<id>                     full record (chart weight / height, undated)
  GET /fhir/ServiceRequest?patient=<id>&type=<radio|ct|rads|…>&dt=<exam time>
                                             the patient's requests of that type and year
  GET /fhir/ServiceRequest/<id>              order form: weight, height, indication, ward…
Never list requests without `type` (the gateway would scrape every department) nor without
`dt` (every year, at most 100 rows: recent requests of a long history can be cut off).
Every call carries `background=1`: the gateway then never holds its users' work for ours.

Authentication is HTTP Basic, passed through to the HIS login. Wrong credentials, like a
broken HIS session, make searches answer 404 "no patients" rather than 401, and
/api/whoami answers 404 now and then even when everything works – so "not found" is
checked against a patient found before (see enrich.py), not against whoami.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class HisError(Exception):
    """kind: 'auth' (credentials rejected: stop), 'retry' (try later), 'bad' (unexpected)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class HisPatient:
    id: str
    national_id: str | None = None
    sex: str | None = None
    birth_date: dt.date | None = None
    weight_kg: float | None = None  # chart values: date of measurement unknown
    height_m: float | None = None


@dataclass
class HisRequestStub:
    id: str
    authored_on: dt.datetime | None
    region: str | None = None
    procedure: str | None = None


@dataclass
class HisRequest(HisRequestStub):
    national_id: str | None = None
    sex: str | None = None
    birth_date: dt.date | None = None
    weight_kg: float | None = None
    height_m: float | None = None
    pregnant: bool | None = None
    ward: str | None = None
    requester: str | None = None
    priority: str | None = None
    indication: str | None = None
    guideline: str | None = None
    prior_imaging: str | None = None
    raw_values: dict = field(default_factory=dict)  # weight/height text as written


# ------------------------------------------------------------------------------ parsing

_VITALS = "http://hl7.org/fhir/us/vitals/StructureDefinition/"


def number(text) -> float | None:
    """'20', '20,5', '20.5 kg' -> float; 0, negatives and text without digits -> None."""
    if text is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(text))
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    return v if v > 0 else None


def height_m(text) -> float | None:
    """Heights are recorded in cm; a value below 3 is taken as metres."""
    v = number(text)
    if v is None:
        return None
    return round(v if v < 3 else v / 100, 3)


def sex(text) -> str | None:
    """'male' / 'female' / 'M' / 'F' / 'Masculin' / 'Feminin' -> M / F."""
    t = (text or "").strip().upper()[:1]
    return t if t in ("M", "F") else None


def date(text) -> dt.date | None:
    t = (text or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def datetime_(text) -> dt.datetime | None:
    try:
        v = dt.datetime.fromisoformat((text or "").strip())
    except ValueError:
        return None
    return v.replace(tzinfo=None)


def yes_no(text) -> bool | None:
    t = (text or "").strip().lower()
    if t in ("da", "yes", "true", "1", "y"):
        return True
    if t in ("nu", "no", "false", "0", "n"):
        return False
    return None


def _ext(resource: dict, name: str) -> str | None:
    """valueString of an extension whose URL is `name` or ends with '/name'."""
    for e in resource.get("extension") or []:
        url = e.get("url") or ""
        if url == name or url.endswith("/" + name):
            return e.get("valueString") or e.get("valueUri") or None
    return None


def _national_id(resource: dict) -> str | None:
    for i in resource.get("identifier") or []:
        if (i.get("system") or "").endswith("/patient-cnp") and i.get("value"):
            return str(i["value"]).strip()
    return None


def parse_patient(r: dict) -> HisPatient:
    return HisPatient(
        id=str(r.get("id") or ""), national_id=_national_id(r), sex=sex(r.get("gender")),
        birth_date=date(r.get("birthDate")),
        weight_kg=number(_ext(r, _VITALS + "body-weight")),
        height_m=height_m(_ext(r, _VITALS + "height")))


def _text(cc) -> str | None:
    if isinstance(cc, list):
        cc = cc[0] if cc else None
    return ((cc or {}).get("text") or "").strip() or None


def parse_request_stub(r: dict) -> HisRequestStub:
    return HisRequestStub(id=str(r.get("id") or ""), authored_on=datetime_(r.get("authoredOn")),
                          region=_text(r.get("bodySite")), procedure=_text(r.get("code")))


def parse_request(r: dict) -> HisRequest:
    notes: dict[str, str] = {}
    for n in r.get("note") or []:
        cat = _text(n.get("category")) or "ward"
        if n.get("text") and cat not in notes:
            notes[cat] = n["text"].strip()
    subject = r.get("subject") or {}
    w, h = _ext(r, "patientWeight"), _ext(r, "patientHeight")
    return HisRequest(
        id=str(r.get("id") or ""), authored_on=datetime_(r.get("authoredOn")),
        region=_text(r.get("bodySite")), procedure=_text(r.get("code")),
        national_id=((subject.get("identifier") or {}).get("value") or None),
        sex=sex(_ext(r, "patientGender")), birth_date=date(_ext(r, "patientBirthDate")),
        weight_kg=number(w), height_m=height_m(h), pregnant=yes_no(_ext(r, "patientPregnant")),
        ward=notes.get("ward"), requester=((r.get("requester") or {}).get("display") or None),
        priority=r.get("priority"), indication=notes.get("clinical-indication"),
        guideline=notes.get("guideline-decision"), prior_imaging=notes.get("prior-imaging"),
        raw_values={k: v for k, v in (("weight", w), ("height", h)) if v})


# ------------------------------------------------------------------------------- client


class Client:
    def __init__(self, base_url: str, username: str | None, password: str | None,
                 timeout: float = 120, opener=None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/fhir+json, application/json"}
        if username:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        self._open = opener or urllib.request.urlopen
        self.calls = 0

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """JSON body; None for 404 (not found / no results)."""
        params = {**(params or {}), "background": "1"}  # never delay the gateway's users
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self.headers)
        self.calls += 1
        try:
            with self._open(req, timeout=self.timeout) as r:  # noqa: S310 – configured URL
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (401, 403):
                raise HisError("auth", f"credentials rejected ({e.code})") from e
            if e.code in (429, 502, 503, 504):
                raise HisError("retry", f"server busy ({e.code})") from e
            raise HisError("bad", f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HisError("retry", f"unreachable: {getattr(e, 'reason', e)}") from e
        except ValueError as e:
            raise HisError("bad", "answer is not JSON") from e

    # -------------------------------------------------------------- resources

    def metadata(self) -> dict:
        return self._get("/fhir/Metadata") or {}

    def find_patient(self, query: str) -> HisPatient | None:
        """The one patient matching `query` (a national ID or patient code), else None
        (none, or several that the ID cannot tell apart)."""
        body = self._get("/fhir/Patient", {"q": query})
        if not body:
            return None
        if body.get("resourceType") == "Patient":
            return parse_patient(body)
        if body.get("resourceType") != "Bundle":
            return None
        hits = [e["resource"] for e in body.get("entry") or []
                if (e.get("resource") or {}).get("resourceType") == "Patient"]
        exact = [h for h in hits if _national_id(h) == query] or hits
        if len(exact) != 1:
            return None
        full = self._get(f"/fhir/Patient/{urllib.parse.quote(str(exact[0].get('id')))}")
        return parse_patient(full or exact[0])

    def list_requests(self, patient_id: str, kind: str,
                      year: int | None = None) -> list[HisRequestStub]:
        """The patient's requests of one type; with `year`, only that year's (lighter)."""
        params = {"patient": patient_id, "type": kind}
        if year:
            params["dt"] = f"{year}-07-01T00:00:00"  # the gateway keeps only the year
        body = self._get("/fhir/ServiceRequest", params)
        return [parse_request_stub(e["resource"]) for e in (body or {}).get("entry") or []
                if (e.get("resource") or {}).get("resourceType") == "ServiceRequest"]

    def get_request(self, request_id: str) -> HisRequest | None:
        body = self._get(f"/fhir/ServiceRequest/{urllib.parse.quote(request_id)}")
        return parse_request(body) if body else None

    def verify_login(self) -> None:
        """Raise HisError('auth') when the HIS rejects the credentials (401/403). Any other
        answer passes: /api/whoami answers 404 now and then even when searches work."""
        self._get("/api/whoami")

    def check(self) -> dict:
        """Reachability and credentials: {ok, ms, error}."""
        started = time.monotonic()
        try:
            meta = self.metadata()
            if meta.get("resourceType") != "CapabilityStatement":
                raise HisError("bad", "not a FHIR server (no CapabilityStatement)")
            self.verify_login()
            return {"ok": True, "ms": int((time.monotonic() - started) * 1000), "error": None}
        except HisError as e:
            return {"ok": False, "ms": int((time.monotonic() - started) * 1000),
                    "error": str(e)[:255]}
