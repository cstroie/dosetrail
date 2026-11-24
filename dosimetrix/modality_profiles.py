"""One profile per modality group: what the Studies list, the charts and the DRL page show.

The web pages read these from /api/modalities, so a page never assumes CT.
"""

from __future__ import annotations

# columns: keys of the study row (api/data.py _study_row) shown after the common ones
# metrics: keys of the stats catalogue (stats.py) offered for this group's charts
# drl: reference quantities (drl.py QUANTITY) that belong on this group's DRL table
PROFILES: list[dict] = [
    {"code": "CT", "label": "CT", "modalities": ["CT"],
     "columns": ["events", "ctdivol_max", "dlp_total"], "main": "dlp_total",
     "metrics": ["dlp_total", "ctdivol_max"], "drl": ["dlp", "ctdivol"]},
    {"code": "DX", "label": "DX/CR", "modalities": ["DX", "CR"],
     "columns": ["n_events", "kvp_max", "ei_range", "di_range", "dap_total"], "main": "dap_total",
     "metrics": ["dap", "kvp", "mas", "ei", "di"], "drl": ["dap", "kae"]},
    {"code": "RF", "label": "RF", "modalities": ["RF"],
     "columns": ["n_events", "fluoro_time", "fluoro_dap", "acq_dap", "kar_total", "dap_total"],
     "main": "dap_total", "metrics": ["dap_total", "kar_total", "fluoro_time"],
     "drl": ["dap", "kae"]},
    {"code": "XA", "label": "XA", "modalities": ["XA"],
     "columns": ["n_events", "fluoro_time", "kar_total", "psd", "dap_total"], "main": "dap_total",
     "metrics": ["dap_total", "kar_total", "fluoro_time"], "drl": ["dap", "kae"]},
    {"code": "MG", "label": "MG", "modalities": ["MG"],
     "columns": ["n_events", "agd_left", "agd_right"], "main": "agd_max",
     "metrics": ["agd", "kvp", "mas"], "drl": ["mgd"]},
    {"code": "NM", "label": "NM", "modalities": ["NM"],
     "columns": ["activity", "dlp_total"], "main": "activity",
     "metrics": ["activity"], "drl": ["activity"]},
    {"code": "PT", "label": "PT", "modalities": ["PT"],
     "columns": ["activity", "dlp_total"], "main": "activity",
     "metrics": ["activity"], "drl": ["activity"]},
]

BY_MODALITY = {m: p for p in PROFILES for m in p["modalities"]}
GROUPS = [(p["code"], p["label"], p["modalities"]) for p in PROFILES]


def group_of(modality: str | None) -> str | None:
    p = BY_MODALITY.get((modality or "").upper())
    return p["code"] if p else None
