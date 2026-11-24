"""EXAMPLE plugin: put it in ``[specs] plugins_dir`` (default <config dir>/plugins/).

It runs once at start-up, with the server's rights: keep that folder for the administrator. It
registers a field and a transform that a spec can then name, exactly like the built-in ones
(``dozimetrix specs fields`` lists them).
"""

from dozimetrix.specs.registry import field, transform


@field("site.hospital")
def hospital(row):
    "The name of this hospital, for the header of a return."
    return "Spitalul Exemplu"


@field("site.dlp_per_kg", unit="mGy.cm/kg")
def dlp_per_kg(row):
    "DLP of the study per kilogram of body weight (empty without a weight)."
    s = row.s
    return s.total_dlp / s.weight_kg if s.total_dlp and s.weight_kg else None


@transform("percent")
def percent(value):
    "A fraction as a percentage."
    return None if value is None else value * 100
