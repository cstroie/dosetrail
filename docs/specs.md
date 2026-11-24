# Report specs: drop a file, get the export

A **spec** is one TOML file that describes an export: a table an authority asks for, a cohort for an
audit, a form another hospital or country uses. The program finds it, checks it, and offers it on the
command line, in the API, on the Reports page and (if it has a deadline) on the overview. There is no
route, command or page to write for a new one.

Every national report Dozimetrix makes is itself a bundled spec (`dozimetrix/seeds/specs/`), and its
constants (30 patients, 20 patients, February-March, the 20th of the month…) are the spec's `[rules]`,
each with the article it comes from. An amendment of an order is an edit to a number in a file.

## Where files go, and how to see them

```toml
# dozimetrix.toml (the one configuration file)
[specs]
dir = "~/.config/dozimetrix/specs"        # the default: <config dir>/specs
plugins_dir = "~/.config/dozimetrix/plugins"
```

Drop `something.toml` into the specs folder. The folder is re-read when a file changes, so the next
listing shows it, with no restart. A site spec with the same `id` as a bundled one replaces it.

```sh
dozimetrix specs list                 # every spec, bundled and site, and any file that failed
dozimetrix specs fields               # the fields and transforms a spec may name
dozimetrix specs run <id> -p year=2026 -o out.xlsx     # the extension picks the format
dozimetrix specs run <id> -p year=2026                 # no file: JSON on the screen
```

A file with an error is **not fatal**: it is listed as a problem (`specs list` prints it; an
administrator sees it in the API and under a spec's page on Reports) and every other spec keeps working.

## Anatomy

```toml
[spec]
id = "ro-insp-1255-quarterly"      # lower case letters, digits . _ -
kind = "table"                     # records | table | series | outliers | compare
version = "2016-11-07"             # the edition of the form or order; written into every file
title = { en = "…", ro = "…" }
authority = "INSP / LIR-DSP"
country = "RO"
legal = "Ordinul MS nr. 1255/2016, anexa 4"
formats = ["xlsx", "json"]         # xlsx | csv | json | zip
access = ["exporter"]              # roles that may run it (admin always may)

[params]                           # what the person chooses; the Reports page builds the controls
period = { type = "quarter" }      # year | quarter | month | range | none
q = { type = "query", default = "origin:local" }       # the typed query language
min_n = { type = "int", default = 10 }                 # int | float | str | bool | query

[rules]                            # THE constants, each with its source
min_patients = { value = 30, article = "art. 7 alin. 3" }

[deadline]                         # optional, see below
```

* `"${rules.min_patients}"` anywhere in the body is replaced by that rule (a string that is only a
  reference keeps the number's type). A file never hides a number: every workbook's first sheet names
  the spec, its version and each rule with its article.
* The chosen period becomes the window of the selection; `[window] months = [2, 3]` narrows a year to
  some of its months (February and March for the annual DRL return).

## The kinds

| Kind | For | Body (see the bundled spec of the same kind) |
|---|---|---|
| `records` | individual records: a cohort (a row per study), an authority's "N patients per exam" form (a row per examination, one workbook per device), or a row shape only an extractor can make (the national survey) | `[unit] level = study \| exam \| extract`, `[[sheet]]` / `[[family]]`, `[[column]]` |
| `table` | per national code: how many examinations, by age band and sex, and the mean dose by age group (the INSP quarterly form) | `[table]`, `[table.values]`, `[[sheet]]` with `[[sheet.quantity]]` |
| `series` | a reference comparison over the last N periods, with the slope (the DRL trend) | `[series]` |
| `outliers` | who is well above their colleagues for the same exam and age group (operators) | `[outliers]` with `[[outliers.source]]` |
| `compare` | group statistics against a reference: the ICRP 135 / RP 185 grid, the ICRP 135 summary | `[compare] method = group_grid \| drl_summary` |

A **column** is a registered `field` (`study.total_dlp`), optionally with a `transform`
(`round:1`, `scale:1000`, `yesno`, `date`, `datetime`, `map:R=drept:L=stang`, `upper`), or a `value`
a kind computed (a sum over the exam's events, a number an extractor produced), or `seq` (the row
number). Fields are read-only accessors over a study, its patient, device, events and national code.

### Personal data

A field that identifies a person (`patient.name`, `patient.id`, `patient.birth_date`,
`patient.cnp7`, `study.accession`) is marked `identifying`. **A spec that uses one needs the `pid`
role, whatever its `access` says, and the run is audited as such.** A spec cannot lower this.
`pseudo.patient` and `pseudo.device` are keyed codes (HMAC with the site secret) that are safe to
publish; `age.coarse` and `time.month` are coarse enough not to narrow down a birth date.

### Extractors

Some rows cannot be said as a column list (a CT series per row, exposures grouped per projection,
checks between values). An **extractor** is a small registered function that yields them
(`survey_rows`, `drl_dataset_rows`); the spec still owns the columns, units, descriptions, version
and parameters, and ships a data dictionary with the file.

## Deadlines

```toml
[deadline]
id = "insp"                  # prefix of the item ids (insp:2026Q3) and the key
months_after_period = 1      # due in the Nth month after the period ends ...
day = 20                     #   ... on this day
# yearly alternative:  month = 4  day = 15
# lead_days = 21  grace_days = 60        (the defaults)
```

The overview shows the deadline from `lead_days` before until `grace_days` after, with the spec's
title in the user's language and a link to the report. A person marks it **sent**; Dozimetrix sends
nothing.

## Extending beyond the file: plugins

A new field, transform, filter, extractor or kind is a function with a decorator, in a `.py` file in
`[specs] plugins_dir` (see `docs/examples/plugin-site-fields.py`). Plugins are imported once at
start-up and **run with the server's rights**, so that folder is for the administrator only. A plugin
that fails to import is skipped and reported; the file name and SHA-256 of every plugin loaded are
written to the audit log, so a change is visible. A spec itself can never run code.

```python
from dozimetrix.specs.registry import field

@field("site.hospital")
def hospital(row):
    "The name of this hospital."
    return "Spitalul Exemplu"
```

## Try it, then keep it

1. Copy `docs/examples/cohort-ct-paediatric.toml` to the specs folder and `dozimetrix specs list`.
2. `dozimetrix specs run example-ct-under-5 -p from=2026-09-01 -p to=2026-09-30` prints the rows.
3. Open the Reports page: the spec is in the report selector with its parameters and a preview.
4. Change a rule's value, run it again: the file's information sheet shows the new number and its
   article.

`docs/examples/monthly-return.toml` is a `table` with a deadline, as another authority's monthly
form would be. The tests load both, so they cannot drift from the program.

## Bundled specs

| Spec | Kind | What |
|---|---|---|
| `ro-insp-1255-quarterly` | table | Quarterly centralised report, Order 1255/2016 (art. 7(3): 30 patients per device) |
| `ro-insp-1255-art12` | records | Annual DRL return: 20 patients per exam and device, February-March |
| `ro-ms1245-drl-trend` | series | Quarterly means against the DRLs over 6 quarters |
| `dozimetrix-operator-doses` | outliers | Doses per operator against the department |
| `dozimetrix-icrp135-grid` | compare | ICRP 135 / RP 185 groups beside the national grid |
| `dozimetrix-icrp135-summary` | compare | Per room and facility, against a reference set |
| `ro-sdnp-paediatric-survey` | records | National paediatric survey, anonymised |
| `dozimetrix-drl-dataset` | records | The pseudonymised DRL dataset |
| `dozimetrix-cohort` | records | A cohort of studies by typed query |
