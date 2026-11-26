"""Patient centering in CT: how far the patient lay from the gantry isocentre, from an axial image.

A patient off the isocentre gets a wrong tube-current modulation and a wrong bowtie-filter
dose: a patient 2-3 cm too low (the usual error) is over-estimated by the scanner's localizer
and over-dosed at the surface. Only an image shows it: the dose report does not. So this reads
**one axial image per study**, which the CT staff fetch already retrieves (the first image of the
smallest series) when it is an axial one.

Method (``measure``): the body is the largest connected region above -300 HU (air and most of the
carbon-fibre table are below it); its centroid, in patient coordinates, minus the isocentre.
Lateral offset: + toward the patient's left; vertical: + posterior, i.e. a positive value is a
patient lying **lower** than the isocentre (supine). Only supine head-first / feet-first images
are measured.

**Not verified on real data, and what it rests on.** No CT image was available to check it:
``isocentre`` is the one function that carries the assumption. It uses the image's Data Collection
Center (0018,9313), which says where the isocentre is in patient coordinates, when the scanner
writes it (``method = data_collection_center``). Where it does not, the image centre is taken as
the isocentre only if the reconstruction is centred on it, which cannot be told from the header:
those are stored as ``method = image_center`` and the report counts them apart; do not act on
them before a phantom or a known case has confirmed them. A residual table or mattress above
-300 HU shifts the centroid downward by a few millimetres.

Off by default: ``[ingest] measure_centering = true`` in the TOML turns it on, and it then reads the
pixels of those images (the other CT images are read header-only, as always). Without numpy, or
for a compressed image that cannot be decoded, the reason is stored and the image is not measured.
"""

from __future__ import annotations

from collections import deque
from functools import lru_cache

LIMIT_MM = (10.0, 20.0)  # a vertical offset beyond these is flagged: noticeable, large
HU_BODY = -300.0
GRID = 128  # the mask is reduced to about this size for the labelling


@lru_cache(maxsize=1)
def enabled() -> bool:
    from .config import load_settings

    return bool(load_settings().measure_centering)


def wants_pixels(ds) -> bool:
    """A CT axial image to measure: the CT image SOP classes, not a localizer."""
    from .ingest.parse import CT_IMAGE_SOP

    if str(ds.get("SOPClassUID", "")) not in CT_IMAGE_SOP or not enabled():
        return False
    itype = [str(x).upper() for x in (ds.get("ImageType") or [])]
    return "LOCALIZER" not in itype and "DERIVED" not in itype


def isocentre(ds) -> tuple[tuple[float, float], str] | None:
    """The isocentre (x, y) in patient coordinates, mm, and how it was got. THE assumption:
    see the module docstring."""
    dcc = ds.get("DataCollectionCenterPatient")
    if dcc is not None and len(dcc) >= 2:
        return (float(dcc[0]), float(dcc[1])), "data_collection_center"
    return None  # the image centre is not assumed here; see centre_of_image


def _largest_component(mask):
    """Boolean mask of the largest 4-connected region (pure python on a reduced grid)."""
    import numpy as np

    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    for y0 in range(h):
        for x0 in range(w):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            comp, queue = [], deque([(y0, x0)])
            seen[y0, x0] = True
            while queue:
                y, x = queue.popleft()
                comp.append((y, x))
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
            if len(comp) > len(best):
                best = comp
    out = np.zeros_like(mask, dtype=bool)
    for y, x in best:
        out[y, x] = True
    return out


def measure(ds, allow_image_center: bool = True) -> dict:
    """``{"measured": bool, "reason": ..., offset_x_mm, offset_y_mm, method, body_w_mm,
    body_h_mm}`` of one axial image dataset (with pixel data)."""
    fail = {"measured": False, "offset_x_mm": None, "offset_y_mm": None, "method": None}
    try:
        import numpy as np
    except ImportError:
        return {**fail, "reason": "numpy is not installed"}
    if str(ds.get("PatientPosition", "")).upper() not in ("HFS", "FFS"):
        return {**fail, "reason": f"patient position {ds.get('PatientPosition') or '?'}: only "
                                  "supine head-first / feet-first is measured"}
    ps, ipp, iop = ds.get("PixelSpacing"), ds.get("ImagePositionPatient"), \
        ds.get("ImageOrientationPatient")
    if not (ps and ipp and iop):
        return {**fail, "reason": "no image geometry"}
    iop = [float(v) for v in iop]
    if abs(iop[0] - 1) > 0.05 or abs(iop[4] - 1) > 0.05:  # an axial slice: rows along x, y down
        return {**fail, "reason": "not an axial slice"}
    try:
        arr = ds.pixel_array.astype(float)
    except Exception as e:  # noqa: BLE001 – compressed without a decoder, or no pixel data
        return {**fail, "reason": f"pixels not readable: {type(e).__name__}"}
    arr = arr * float(ds.get("RescaleSlope", 1) or 1) + float(ds.get("RescaleIntercept", 0) or 0)
    rows, cols = arr.shape
    step = max(1, max(rows, cols) // GRID)
    small = arr[:rows // step * step, :cols // step * step]
    small = small.reshape(rows // step, step, cols // step, step).mean(axis=(1, 3))
    body = _largest_component(small > HU_BODY)
    ys, xs = np.nonzero(body)
    if len(xs) < 50:
        return {**fail, "reason": "no body found in the image"}
    dy, dx = float(ps[0]), float(ps[1])  # row spacing (along y), column spacing (along x)
    cx = (xs.mean() + 0.5) * step * dx + float(ipp[0])  # patient x of the centroid
    cy = (ys.mean() + 0.5) * step * dy + float(ipp[1])
    width = (xs.max() - xs.min() + 1) * step * dx
    height = (ys.max() - ys.min() + 1) * step * dy
    iso = isocentre(ds)
    if iso is None:
        if not allow_image_center:
            return {**fail, "reason": "the image does not say where the isocentre is"}
        iso = ((float(ipp[0]) + cols * dx / 2, float(ipp[1]) + rows * dy / 2), "image_center")
    (ix, iy), method = iso
    return {"measured": True, "reason": None, "offset_x_mm": cx - ix, "offset_y_mm": cy - iy,
            "method": method, "body_w_mm": width, "body_h_mm": height}
