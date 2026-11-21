"""Generate the synthetic sample data used by the static UI (dosetrail/web/data/sample/).

Everything here is invented: device names are generic, patients and accession
numbers are random. The output is deterministic (fixed seed), so re-running the
script only changes the files when the script changes.

    python tools/make_ui_sample.py
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "dosetrail" / "web" / "data" / "sample"
NOW = datetime(2026, 11, 21, 10, 42)
rng = random.Random(20261121)

DEVICES = [
    # id, display, manufacturer, model, serial, station, modality, origin, institution
    (1, "CT 1 – Emergency", "Siemens", "SOMATOM go.Top", "108542", "CT1_EMERG", "CT", "local"),
    (2, "CT 2 – Radiology", "GE Healthcare", "Revolution EVO", "EVO2291", "CTRAD2", "CT", "local"),
    (3, "CT 3 – Oncology", "Philips", "Incisive CT", "INC0457", "CT3ONCO", "CT", "local"),
    (4, "Angio Suite", "Philips", "Azurion 7 B20/15", "AZ7-1180", "ANGIO1", "XA", "local"),
    (5, "Fluoro Room", "Siemens", "Luminos dRF Max", "LDR3312", "FLUORO", "RF", "local"),
    (6, "DR Room 1", "Philips", "DigitalDiagnost C90", "4410-22", "DR_ROOM_1", "DX", "local"),
    (7, "DR Room 2", "Siemens", "Ysio X.pree", "YX-9020", "DRROOM2", "DX", "local"),
    (8, "Mobile DR ICU", "GE Healthcare", "AMX Navigate", "MX7731", "AMX-ICU", "DX", "local"),
    (9, "Mammo 1", "Hologic", "Selenia Dimensions", "SD-55120", "MAMMO1", "MG", "local"),
    (
        10,
        "Ext. CT – Regional Clinic",
        "Canon",
        "Aquilion Prime SP",
        "",
        "AQPRIME",
        "CT",
        "external",
    ),
]
# extra equipment records that are really the same physical device (merge suggestions)
ALIASES = [
    (11, 1, "ct1_emerg", "SIEMENS", "VA30", 2118, "2025-03-14T09:12"),
    (12, 1, "CT1-EMERG", "Siemens", "VA40", 7244, "2025-06-02T17:40"),
    (13, 6, "DR1", "Philips", "4.3", 18877, "2026-01-11T12:05"),
    (14, 8, "amx-icu", "GE Healthcare", "", 402, "2025-08-20T06:31"),
]
LAST_STUDY = {
    1: NOW - timedelta(minutes=11),
    2: NOW - timedelta(hours=2),
    3: NOW - timedelta(hours=5),
    4: NOW - timedelta(minutes=40),
    5: NOW - timedelta(hours=19, minutes=40),
    6: NOW - timedelta(minutes=8),
    7: datetime(2026, 11, 18, 16, 40),
    8: datetime(2026, 11, 15, 7, 12),
    9: NOW - timedelta(hours=18),
    10: NOW - timedelta(days=3),
}
STUDY_COUNTS = {
    1: 31842,
    2: 28411,
    3: 9306,
    4: 3920,
    5: 5188,
    6: 61204,
    7: 44310,
    8: 12090,
    9: 22650,
    10: 1312,
}

# standard protocol name -> (description, {device id: CTDIvol q1, median, q3 in mGy},
#   scan length cm, local DRL CTDIvol mGy, local DRL DLP mGy·cm, relative frequency)
CT_PROTOCOLS = {
    "Head routine": (
        "CT head without contrast",
        {1: (46, 51, 56), 2: (48, 53, 58), 3: (41, 45, 49)},
        16.5,
        60,
        1000,
        0.30,
    ),
    "Chest routine": (
        "CT chest",
        {1: (6.8, 8.1, 9.6), 2: (5.9, 7.2, 8.4), 3: (4.8, 5.6, 6.7)},
        36,
        10,
        400,
        0.25,
    ),
    "Abdomen-pelvis": (
        "CT abdomen-pelvis with contrast",
        {1: (10.2, 11.8, 13.6), 2: (9.1, 10.4, 12.0), 3: (8.0, 9.2, 10.5)},
        48,
        13,
        700,
        0.22,
    ),
    "CT pulmonary angiography": (
        "CT pulmonary angiography",
        {1: (5.6, 6.9, 8.3), 2: (5.1, 6.2, 7.4)},
        34,
        10,
        350,
        0.08,
    ),
    "Cervical spine": ("CT cervical spine", {1: (16, 19, 22), 2: (17, 20, 23)}, 18, 22, 500, 0.08),
    "Chest-abdomen-pelvis": (
        "CT chest-abdomen-pelvis with contrast",
        {1: (8.8, 10.1, 11.6), 2: (8.0, 9.3, 10.6), 3: (7.0, 8.1, 9.3)},
        70,
        12,
        900,
        0.07,
    ),
}
FIRST = [
    "ION",
    "MARIA",
    "ELENA",
    "ANDREI",
    "GHEORGHE",
    "IOANA",
    "MIHAI",
    "ANA",
    "VASILE",
    "CRISTINA",
    "DAN",
    "ADRIANA",
]
LAST = [
    "POPESCU",
    "IONESCU",
    "POPA",
    "STAN",
    "DUMITRU",
    "STOICA",
    "GHEORGHE",
    "MATEI",
    "CONSTANTIN",
    "MARIN",
    "TUDOR",
    "DOBRE",
]


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def lognorm_from_quartiles(q1: float, med: float, q3: float) -> float:
    sigma = (math.log(q3) - math.log(q1)) / 1.349
    return math.exp(rng.gauss(math.log(med), sigma))


def hour_weight(h: int, weekday: int) -> float:
    day = 1.0 if weekday < 5 else 0.35
    return 0.12 + day * math.exp(-(((h - 11) / 3.6) ** 2))


def random_time(day: datetime, emergency: bool) -> datetime:
    weights = [hour_weight(h, day.weekday()) + (0.2 if emergency else 0) for h in range(24)]
    h = rng.choices(range(24), weights)[0]
    return day.replace(hour=h, minute=rng.randrange(60))


def make_ct():
    studies, acqs = [], []
    seq = {}
    start = NOW - timedelta(days=150)
    day = start
    while day.date() <= NOW.date():
        for dev in (1, 2, 3):
            base = {1: 9, 2: 7, 3: 3}[dev] * (
                1 if day.weekday() < 5 else (0.7 if dev == 1 else 0.1)
            )
            for _ in range(max(0, int(rng.gauss(base, 1.5)))):
                t = random_time(day, dev == 1)
                if t > NOW:
                    continue
                protos = [p for p, v in CT_PROTOCOLS.items() if dev in v[1]]
                weights = [CT_PROTOCOLS[p][5] for p in protos]
                proto = rng.choices(protos, weights)[0]
                desc, per_dev, length, _drl, _drl_dlp, _w = CT_PROTOCOLS[proto]
                sex = rng.choice("MF")
                age = max(18, min(95, int(rng.gauss(58, 17))))
                weight = (
                    None
                    if rng.random() < 0.35
                    else round(max(42, rng.gauss(78 if sex == "M" else 67, 14)))
                )
                wf = 1 + ((weight or 75) - 75) / 75 * 0.6
                ctdi = round(lognorm_from_quartiles(*per_dev[dev]) * wf, 2)
                n_spiral = 2 if proto in ("Chest-abdomen-pelvis",) and rng.random() < 0.3 else 1
                if proto == "Abdomen-pelvis" and rng.random() < 0.15:
                    n_spiral = 3
                acq_dlp = []
                for _k in range(n_spiral):
                    dlp = round(ctdi * length * rng.uniform(0.9, 1.15), 1)
                    acq_dlp.append(dlp)
                    acqs.append(
                        {"p": proto, "d": dev, "c": ctdi, "l": dlp, "w": weight, "t": iso(t)}
                    )
                loc = rng.choice([1, 1, 2])
                total = round(sum(acq_dlp) + loc * rng.uniform(2, 8), 1)
                key = t.strftime("%Y%m%d")
                seq[key] = seq.get(key, 100) + rng.randint(1, 9)
                acc = f"A{key}{seq[key]:05d}"
                flags = []
                drl_dlp = CT_PROTOCOLS[proto][4]
                if total > drl_dlp:
                    flags.append(
                        {
                            "kind": "drl",
                            "level": "warn",
                            "text": f"Above local DRL {drl_dlp} mGy·cm",
                        }
                    )
                if ctdi > 80:
                    flags.append(
                        {
                            "kind": "dose_check",
                            "level": "alert",
                            "text": f"CT dose check: CTDIvol {ctdi} mGy",
                        }
                    )
                studies.append(
                    {
                        "accession": acc,
                        "date": iso(t),
                        "device_id": dev,
                        "description": desc,
                        "protocol": proto,
                        "patient": {
                            "age": age,
                            "sex": sex,
                            "weight": weight,
                            "pseudo_id": "P-"
                            + "".join(
                                rng.choice("23456789ABCDEFGHJKMNPQRSTUVWXYZ") for _ in range(4)
                            ),
                        },
                        "events": {"spiral": n_spiral, "axial": 0, "localizer": loc},
                        "ctdivol_max": ctdi,
                        "dlp_total": total,
                        "flags": flags,
                        "test": False,
                    }
                )
        day += timedelta(days=1)
    # a few phantom / QA studies, excluded by default
    for k in range(6):
        t = NOW - timedelta(days=7 * k + 2, hours=3)
        studies.append(
            {
                "accession": f"QA{t:%Y%m%d}01",
                "date": iso(t),
                "device_id": 1 + k % 3,
                "description": "QA phantom daily",
                "protocol": "QA",
                "patient": {"age": None, "sex": "O", "weight": None, "pseudo_id": "QA"},
                "events": {"spiral": 1, "axial": 1, "localizer": 1},
                "ctdivol_max": 20.1,
                "dlp_total": 402.0,
                "flags": [],
                "test": True,
            }
        )
    studies.sort(key=lambda s: s["date"], reverse=True)
    return studies, acqs


def make_xa_study():
    """The complex PCI used by the study-detail screen (A2026112100123)."""
    r = random.Random(11)
    acq_set = {2, 4, 6, 9, 11, 13, 15, 17, 19}
    events, t = [], 0.0
    for i in range(40):
        acq = (i % 20) in acq_set
        dur = 3 + r.random() * 5 if acq else 20 + r.random() * 70
        dap = 4 + r.random() * 8 if acq else dur * 0.06 * (0.6 + r.random())
        plane = "B" if r.random() < 0.34 else "A"
        p, s = round(-40 + r.random() * 80), round(-30 + r.random() * 60)
        events.append(
            {
                "t": t,
                "acq": acq,
                "dur": dur,
                "dap": dap,
                "kar": dap * (9 + r.random() * 6),
                "plane": plane,
                "kvp": round((78 + r.random() * 14) if acq else (70 + r.random() * 12)),
                "ma": round((600 + r.random() * 300) if acq else (40 + r.random() * 50)),
                "pulse": 15 if acq else (7.5 if r.random() < 0.6 else 15),
                "cu": 0.1 if acq else (0.4 if r.random() < 0.5 else 0.9),
                "primary": p,
                "secondary": s,
                "field": 20 if acq else (20 if r.random() < 0.5 else 25),
                "sid": round(98 + r.random() * 22),
            }
        )
        t += dur + 25 + r.random() * 80
    kd = 284.6 / sum(e["dap"] for e in events)
    kk = 3420 / sum(e["kar"] for e in events)
    kf = 1938 / sum(e["dur"] for e in events if not e["acq"])
    kt = 4000 / t
    start = datetime(2026, 11, 21, 9, 14)
    out = []
    for n, e in enumerate(events, 1):
        dur = e["dur"] * (1 if e["acq"] else kf)
        out.append(
            {
                "no": n,
                "time": (start + timedelta(seconds=e["t"] * kt)).strftime("%Y-%m-%dT%H:%M:%S"),
                "type": "acquisition" if e["acq"] else "fluoro",
                "protocol": "Cardiac 15 fps"
                if e["acq"]
                else ("Fluoro Low 7.5 p/s" if e["pulse"] == 7.5 else "Fluoro Normal 15 p/s"),
                "plane": e["plane"],
                "kvp": e["kvp"],
                "ma": e["ma"],
                "pulse_rate": e["pulse"],
                "cu_mm": e["cu"],
                "primary_angle": e["primary"],
                "secondary_angle": e["secondary"],
                "field_cm": e["field"],
                "sid_cm": e["sid"],
                "dap_gycm2": round(e["dap"] * kd, 4),
                "kar_mgy": round(e["kar"] * kk, 2),
                "duration_s": round(dur, 1),
            }
        )
    return {
        "accession": "A2026112100123",
        "modality": "XA",
        "description": "Coronary angiography + PCI",
        "start": "2026-11-21T09:14",
        "end": "2026-11-21T10:22",
        "device": {"id": 4, "name": "Angio Suite", "biplane": True},
        "physician": "Dr. A. Ionescu",
        "patient": {
            "pseudo_id": "P-7Q2K",
            "age": 67,
            "sex": "M",
            "height_cm": 176,
            "weight_kg": 88,
            "identified": {"name": "POPESCU^ION", "id": "2041187"},
        },
        "totals": {
            "dap_gycm2": 284.6,
            "kar_gy": 3.42,
            "fluoro_s": 1938,
            "acquisitions": 18,
            "frames": 1412,
            "peak_skin_mgy": None,
        },
        "thresholds": {
            "dap_gycm2": 250,
            "kar_gy": 3.0,
            "kar_notify_gy": 1.0,
            "cum_kar_gy": 5.0,
            "cum_dap_gycm2": 500,
            "cum_weeks": 12,
        },
        "alerts": [
            {"kind": "kar", "level": "alert", "text": "Ka,r 3.42 Gy ≥ 3.00 Gy"},
            {
                "kind": "cumulative",
                "level": "alert",
                "text": "Σ Ka,r 5.42 Gy in 12 weeks ≥ 5.00 Gy",
            },
        ],
        "previous": [
            {
                "accession": "A2026091400311",
                "date": "2026-09-14",
                "description": "Coronary angiography",
                "dap_gycm2": 38.2,
                "kar_gy": 0.52,
                "fluoro_s": 400,
            },
            {
                "accession": "A2026103000207",
                "date": "2026-10-30",
                "description": "PCI, one vessel",
                "dap_gycm2": 121.5,
                "kar_gy": 1.48,
                "fluoro_s": 845,
            },
        ],
        "events": out,
    }


def make_overview(ct_studies):
    week_start = NOW - timedelta(days=NOW.weekday())
    week_start = week_start.replace(hour=0, minute=0)
    ct_week = sum(1 for s in ct_studies if not s["test"] and s["date"] >= iso(week_start))
    ct_month = sum(1 for s in ct_studies if not s["test"] and s["date"] >= "2026-11-01")

    def weekly(last, spread):
        return [max(1, round(last * (1 + rng.uniform(-spread, spread)))) for _ in range(11)] + [
            last
        ]

    alerts = [
        {
            "id": 1,
            "level": "alert",
            "kind": "KA,R",
            "title": "Complex PCI · Angio Suite",
            "accession": "A2026112100123",
            "time": "2026-11-21T09:14",
            "value": "3.42 Gy",
            "limit": "≥ 3.00 Gy",
        },
        {
            "id": 2,
            "level": "alert",
            "kind": "CUMUL.",
            "title": "P-7Q2K · 3 studies / 12 wk",
            "accession": "A2026112100123",
            "time": "2026-11-21T10:22",
            "value": "Σ 5.42 Gy",
            "limit": "≥ 5.00 Gy",
        },
    ]
    for s in ct_studies[:400]:
        for f in s["flags"]:
            if len(alerts) >= 5:
                break
            dev = next(d[1] for d in DEVICES if d[0] == s["device_id"])
            alerts.append(
                {
                    "id": len(alerts) + 1,
                    "level": f["level"],
                    "kind": "DOSE CHECK" if f["kind"] == "dose_check" else "> DRL",
                    "title": f"{s['protocol']} · {dev}",
                    "accession": s["accession"],
                    "time": s["date"],
                    "value": f"{s['ctdivol_max']} mGy"
                    if f["kind"] == "dose_check"
                    else f"{s['dlp_total']:.0f} mGy·cm",
                    "limit": f["text"].split(": ")[-1].replace("Above ", ""),
                }
            )
    feeds = []
    for d in DEVICES:
        if d[7] != "local":
            continue
        days = []
        for k in range(6, -1, -1):
            day = (NOW - timedelta(days=k)).date()
            last = LAST_STUDY[d[0]].date()
            weekend = day.weekday() >= 5
            if day > last:
                n = 0
            else:
                base = {"CT": 40, "XA": 10, "RF": 8, "DX": 230, "MG": 25}[d[6]]
                n = (
                    0
                    if (weekend and d[6] in ("RF", "MG"))
                    else max(1, round(base * (0.4 if weekend else 1) * rng.uniform(0.8, 1.2)))
                )
            days.append({"date": day.isoformat(), "n": n})
        age_days = (NOW - LAST_STUDY[d[0]]).total_seconds() / 86400
        status = "alert" if age_days >= 5 else ("warn" if age_days >= 2 else "ok")
        feeds.append(
            {
                "device_id": d[0],
                "name": d[1],
                "last_study": iso(LAST_STUDY[d[0]]),
                "status": status,
                "days": days,
            }
        )
    return {
        "generated": iso(NOW),
        "modalities": [
            {
                "code": "CT",
                "label": "CT",
                "week": ct_week,
                "month": ct_month,
                "delta_pct": 6,
                "weekly": weekly(ct_week, 0.08),
            },
            {
                "code": "DX",
                "label": "DX/CR",
                "week": 1906,
                "month": 8214,
                "delta_pct": -3,
                "weekly": weekly(1906, 0.05),
            },
            {
                "code": "XA",
                "label": "RF/XA",
                "week": 58,
                "month": 241,
                "delta_pct": 12,
                "weekly": weekly(58, 0.12),
            },
            {
                "code": "MG",
                "label": "MG",
                "week": 133,
                "month": 602,
                "delta_pct": 0,
                "weekly": weekly(133, 0.06),
            },
        ],
        "alerts": alerts,
        "feeds": feeds,
    }


def make_devices():
    recs = []
    for d in DEVICES:
        recs.append(
            {
                "id": d[0],
                "physical_id": d[0],
                "display_name": d[1],
                "manufacturer": d[2],
                "model": d[3],
                "serial": d[4],
                "station": d[5],
                "software": "",
                "institution": "Regional Clinic" if d[7] == "external" else "County Hospital",
                "modality": d[6],
                "modality_override": None,
                "origin": d[7],
                "studies": STUDY_COUNTS[d[0]],
                "last_study": iso(LAST_STUDY[d[0]]),
            }
        )
    for rid, pid, station, man, sw, n, last in ALIASES:
        p = next(x for x in recs if x["id"] == pid)
        recs.append(
            {
                **p,
                "id": rid,
                "display_name": station,
                "manufacturer": man,
                "station": station,
                "software": sw,
                "studies": n,
                "last_study": last,
            }
        )
    suggestions = []
    for pid in sorted({a[1] for a in ALIASES}):
        ids = [pid] + [a[0] for a in ALIASES if a[1] == pid]
        suggestions.append(
            {"record_ids": ids, "rule": "same manufacturer (case-insensitive), model and serial"}
        )
    return {"records": recs, "merge_suggestions": suggestions}


def make_drl():
    return {
        "set": "Local DRL 2025",
        "ct_ctdivol_mgy": {k: v[3] for k, v in CT_PROTOCOLS.items()},
        "ct_dlp_mgycm": {k: v[4] for k, v in CT_PROTOCOLS.items()},
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ct, acqs = make_ct()
    for dev in (1, 2, 3):  # keep the feed times consistent with the generated CT studies
        LAST_STUDY[dev] = datetime.fromisoformat(
            next(x["date"] for x in ct if x["device_id"] == dev)
        )
    files = {
        "devices.json": make_devices(),
        "ct-studies.json": ct,
        "ct-acquisitions.json": acqs,
        "study-A2026112100123.json": make_xa_study(),
        "overview.json": make_overview(ct),
        "drl.json": make_drl(),
    }
    for name, data in files.items():
        (OUT / name).write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        print(f"{name}: {(OUT / name).stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
