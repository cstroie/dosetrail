# Dozimetrix — design draft

Status: **DRAFT for review** · 2026-11-21

Dozimetrix is an open-source (MIT) patient radiation exposure monitor for CT, radiography (DX/CR), fluoroscopy (RF/XA), mammography (MG) and later NM/PET. It aims to reach the same goals and reports as OpenREM (see `openrem-analysis.md`) with a fast, modern, maintainable code base. No OpenREM code is reused.

---

## 1. Principles

1. **Few dependencies.** Write our own code where it is reasonable. Every third-party package must justify itself (§3).
2. **Flat, analytics-first data model.** One wide row per irradiation event, with typed and indexed columns for every metric we chart or filter on. The full source document is kept as JSON for re-processing. No 1:1 mirror of DICOM templates.
3. **Idempotent ingestion.** Re-sending, re-querying or re-importing anything is always safe. The keys are SOP Instance UID and Irradiation Event UID.
4. **Vendor quirks are data, not scattered `if`s.** A registry of adapters, each with fixture-based tests.
5. **Keep the raw evidence.** Received DICOM objects are spooled and optionally archived, so a parser fix can be re-applied to history.
6. **Statistics belong in the database** where possible. The web layer only formats.
7. **One process to run.** The web app, DICOM listener and background worker live in one service with no broker and no external queue. They can be split later.

---

## 2. Platform

| Item | Choice |
|---|---|
| Language | Python **3.12** (floor). It gives `tomllib`, modern typing and performance. To be confirmed for the production host. |
| Production DB | **MariaDB ≥ 10.6** (10.11 / 11.4 LTS recommended) |
| Development / test DB | SQLite (no server needed). CI also runs the test suite against MariaDB. |
| PostgreSQL | **Not supported** as a Dozimetrix backend. It is only *read* by the OpenREM importer (§8). |
| Deployment | Plain install first: virtualenv, systemd unit, reverse proxy optional. Docker later. |
| Licence | MIT |

### MariaDB specifics
- Charset `utf8mb4`. Descriptions and protocol names use `utf8mb4_unicode_ci`, which gives case-insensitive grouping for free.
- DICOM UIDs, serial numbers and hashes use `VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin`: exact matching and compact indexes.
- DDL is not transactional, so migrations stay small and single-purpose, and the upgrade procedure includes a backup step.
- Percentiles: `MEDIAN()` / `PERCENTILE_CONT() OVER (PARTITION BY …)` on MariaDB. Pure Python (`statistics.quantiles`) on SQLite. Both implementations sit behind one `stats` interface and are tested for identical results.

---

## 3. Dependency budget

| Keep (justified) | Write our own | Avoid |
|---|---|---|
| **pydicom**: DICOM parsing is huge, standard-critical, and pure Python | **Task queue**: DB-backed job table plus worker thread(s) | Celery, RabbitMQ, redis, huey |
| **pynetdicom**: DICOM upper-layer networking (association negotiation, C-STORE/C-FIND/C-MOVE). It is the single largest dependency. Writing our own is possible but costly; see the open question in §12. | **XLSX writer**: an xlsx file is a zip of XML (stdlib `zipfile`) | pandas, numpy, openpyxl, xlsxwriter |
| **SQLAlchemy 2 + Alembic**: portable SQL plus migrations | **Statistics**: medians, quantiles, histograms, boxplot data | matplotlib, plotly (server-side charts) |
| **FastAPI + uvicorn** (pulls in Starlette and pydantic) | **Auth**: sessions, `hashlib.scrypt` passwords, roles | Django and its ecosystem |
| **PyMySQL**: pure-Python MariaDB driver | **Config**: TOML via stdlib `tomllib` | Java, dcmtk and other external binaries at runtime |
| *(frontend)* **ECharts**: one vendored, pinned JS file | **SR tree walker and RDSR mapping** | |
| | **Skin dose calculation** (later) | |

**Frontend (decided):** **no npm and no build step.** The frontend is plain HTML, CSS and vanilla JavaScript ES modules, with native web components, served statically by FastAPI. **ECharts** is the only vendored library, kept as one local file. The visual direction is **variant B, "Console"** (chosen 2026-11-21, provisional). It is dark-first, with light as the second theme. Navigation is an icon rail plus a context bar. A command palette (Ctrl K) handles search and commands, and filters are typed `key:value` tokens with autocomplete that are stored in the URL. The study list sits beside a preview pane. Numbers use a monospace font. Charts live on a workspace of drag-and-resize panels that share one query. The mockups are in the design canvas and exported to git-ignored `design-inbox/`; the brief is `frontend-brief.md`. The implementation lives in `dosimetrix/web/` (see its README); until the API exists it runs on synthetic sample data.

---

## 4. Architecture

```
            ┌─────────────── dosimetrix (one service) ────────────────┐
 Modality ─►│ DICOM SCP (C-STORE)  ─► spool/ ─► Ingest worker         │
 PACS  ◄───►│ QR client (C-FIND/MOVE, scheduled) ─┘     │             │
 Files  ───►│ CLI import (files / folders / zip)  ─────┘             │
 OpenREM ──►│ Legacy importer (CSV or direct DB)  ─────────────┐      │
            │                                   ▼              ▼      │
            │                       Adapters ─► Normalised model ─► DB│
            │                                                    │    │
 Browser ◄──│ REST API (JSON) + static UI (ECharts) ◄─ Stats layer ◄┘    │
            │ Jobs: exports, alerts, recompute, retention             │
            └────────────────────────────────────────────────────────┘
```

### Package layout (proposed)
```
dosimetrix/
  config.py          settings (TOML + env)
  db/                SQLAlchemy models, session, migrations (alembic/)
  dicom/
    scp.py           store SCP → spool
    qr.py            query/retrieve client and per-modality series selection
    sr.py            generic SR content-tree walker → dict
    codes.py         the DICOM code constants we need (CIDs used by the dose templates)
  ingest/
    pipeline.py      spool → classify → adapter → upsert
    adapters/        rdsr_ct.py, rdsr_projection.py, dx_header.py, mg_header.py,
                     ct_philips_doseinfo.py, rrdsr_nm.py, vendors/*.py (quirk rules)
    upsert.py        idempotent merge (cumulative / continued RDSR logic)
  legacy/openrem/    OpenREM importer (0.10 and 1.0 schemas)
  stats/             quantiles, histograms, time series (SQL + Python fallback)
  exports/           csv, xlsx (own writer), survey formats (pluggable)
  alerts/            high-dose rules, cumulative windows, notifications
  auth/              users, roles, sessions, audit log
  api/               FastAPI routers
  web/               static UI (HTML, JS modules, vendored echarts)
  jobs.py            DB-backed job queue and worker
  cli.py             `dosimetrix` command (serve, import, qr, migrate, user …)
tests/
  fixtures/          anonymised DICOM samples per vendor and modality
```

---

## 5. Data model (core)

Names are indicative. Every table has an integer `id` primary key, plus `created_at` and `updated_at`.

**patient**
- `id`
- `patient_id` (clear)
- `issuer`
- `name`
- `birth_date`
- `sex`
- `legacy_sha256`: OpenREM-compatible hash used for linking historical data (§8.3)
- `is_test`: set by the not-patient rules

**device**: the physical equipment, with a stable identity
- `id`
- `manufacturer`
- `model`
- `serial_number`
- `device_observer_uid`
- `station_name`
- `institution`
- `department`
- `software_versions`
- `display_name`
- `modality_override`
- `is_local`: local or external equipment
- `merged_into_id`

Identity key, in order of preference:
1. device observer UID;
2. manufacturer + serial number;
3. a normalised (case-insensitive) tuple.

The station name alone is never the identity, because names such as "ct99" are shared across sites.

**study**
- `id`
- `study_uid` (ascii_bin, indexed)
- `accession`
- `accession_is_hashed`
- `patient_id` (FK)
- `device_id` (FK)
- `modality` (CT/DX/CR/RF/XA/MG/NM)
- `study_datetime`
- `study_description`, `requested_procedure`, `procedure`
- `standard_name_id`
- Patient values at the time of the study: `age_years`, `weight_kg`, `height_m`
- `physician`, `operator`, `referring`
- **Totals:** `n_events`, `total_dlp`, `total_ctdivol_sum`, `total_dap`, `total_rp_dose`, `total_fluoro_time`, `total_fluoro_dap`, `total_acq_dap`, `agd_left`, `agd_right`, `admin_activity_mbq`, …
- `source_kind` (rdsr / header / philips_sc / openrem_import …)
- `source_json` (optional, compressed)

**event**: one wide row per irradiation event (all modalities)
- `id`
- `study_id`
- `event_uid` (ascii_bin, unique per study)
- `datetime`
- `event_type` (spiral / axial / stationary / localizer / fluoro / acquisition / …)
- `protocol`, `standard_protocol_id`
- `plane` (A/B)
- `anatomy`, `laterality`, `view`
- **CT:** `ctdivol`, `dlp`, `phantom` (16/32), `scan_length`, `pitch`, `collimation`, `rotation_time`, `kvp`, `ma`, `ma_max`, `exposure_time`, `ssde`, `wed`, `dose_check_alert`
- **Projection:** `dap`, `rp_dose`, `kvp`, `mas`, `ma`, `exposure_time`, `pulse_rate`, `pulse_width`, `n_pulses`, `fluoro_time`, `filter_cu_mm`, `filter_al_mm`, `grid`, `sid`, `ssd`, `field_w`, `field_h`, `primary_angle`, `secondary_angle`, `table_pos_x`, `table_pos_y`, `table_pos_z`
- **Detector:** `exposure_index`, `target_ei`, `deviation_index`, `ei_vendor_value`, `ei_vendor_unit`
- **MG:** `agd`, `esd`, `compression_thickness`, `compression_force`, `target`, `filter`, `exposure_control_mode`
- `extra_json`: vendor or rare fields

**nm_administration** (later)
- `study_id`
- `radiopharmaceutical`, `radionuclide`
- `activity_mbq`
- `datetime`
- `route`
- `organ_doses_json`

### 5.1 Paediatric-first patient data
Paediatric dosimetry is a primary use case. Doses are compared per **age group** for head exams and per **weight group** for body exams (ICRP 135, RP 185), so the patient fields need to be richer than in OpenREM:
- **Exact age**, computed from the birth date and the study date. It is shown in days below 1 year, in months between 1 and 24 months, and in years after that.
- **Weight and height, with provenance:** `weight_source` takes one of these values:
  - `measured`;
  - `chart_same_day`;
  - `chart_72h`;
  - `estimated_reference` (from a growth-reference table);
  - `estimated_visual`;
  - `dicom_unknown`.

  Weight is often missing from DICOM, so it can be entered manually in the UI, imported from CSV, and later come from RIS/HIS.
- **Pregnancy flag and gestational weeks**, when they are recorded.
- **Pluggable national-ID parsers** derive birth date and sex when DICOM lacks them. Example: the Romanian CNP encodes both.
- **Flags for exclusion from DRL statistics**, with a reason:
  - weight below P5 or above P95 for age, against a growth-reference table;
  - premature newborn;
  - incubator;
  - explicitly modified protocol.

  Excluded studies are still kept and monitored.

### 5.2 Classification as data
- **`code_system`** (name, version, source, licence) and **`code`** (system, code, meaning, parent, attributes) hold any number of exam and indication vocabularies. Examples:
  - national procedure codes;
  - a national investigation nomenclature;
  - local or proposed nomenclators for CT, radiography and fluoroscopy;
  - an indications nomenclator;
  - RadLex/LOINC;
  - ICD-like disease groups.
- **`code_mapping`** links local protocol names and study descriptions to exam codes, and exam codes to codes in other systems. Mappings can be suggested automatically, but a person must confirm them.
- **Study-level classification:**
  - `exam_code`;
  - `indication_code`;
  - `contrast_mode` (native / contrast / native+contrast / multiphase);
  - `projection` codes for radiography;
  - a bilateral flag.
- **Patient-group schemes** (`group_scheme`, `group_band`): half-open bands `[lower, upper)` on age or weight, so a patient exactly at a limit falls into the upper group. Seeded with:
  - the ICRP 135 / RP 185 bands;
  - an extended scheme with extra bands for >18 years and for 80–110 kg and >110 kg;
  - national schemes as they are published.

  Exams declare which parameter (age or weight) and which scheme apply.

### 5.3 CT series semantics
DRLs for CT refer to **one scan series**, not the whole study (RP 185, and national rules). Each CT event therefore stores:
- `is_localizer`;
- `is_principal`: the main diagnostic series, picked by a rule and overridable;
- the CTDIvol **phantom (16 or 32 cm)**.

Statistics can use the principal series, the per-series values or the study total.

### 5.4 Institutions
`institution` is a first-class dimension from the start: every device belongs to an institution. A single site uses one institution. An aggregating instance (§9.4) holds many.

### 5.5 Units
Units are configurable per device and source. A conversion table is editable by the admin (for example cGy·cm², dGy·cm², mGy·cm², µGy·m² → Gy·cm²), because devices report DAP in many units.

**Supporting tables**
- **Mapping:** `standard_name`, `not_patient_rule`
- **Ingest bookkeeping:** `ingested_object` (sop_uid, study_id, source, status, error)
- **Access:** `user`, `role`, `audit_log`
- **Background work:** `job`, `export`
- **Alerts:** `alert_rule`, `alert_event`
- **DICOM networking:** `dicom_node`, `qr_schedule`

Units are fixed and documented: DLP in mGy·cm, CTDIvol in mGy, DAP in **Gy·cm²**, RP dose in Gy, AGD in mGy. The UI converts to display units only at render time.

---

## 6. Ingestion

1. **Receive:** the C-STORE SCP writes each object to `spool/incoming/` and acknowledges immediately. Nothing else happens in the network thread.
2. **Classify:** by SOP class, modality and series description. The possible classes are: RDSR, Enhanced SR, RRDSR, DX/CR image, MG image, Philips Dose Info secondary capture, other. Objects of no interest are dropped early, and only their header is read (pixel data is deferred).
3. **Adapt:** pick the adapter by (SOP class, manufacturer, model, software version), then apply the vendor quirk rules. The output is a normalised `StudyRecord` with a list of `EventRecord`s.
4. **Upsert:** decide by study UID and event UIDs:
   - the SOP Instance UID was already seen: skip;
   - the new events are a subset of the stored ones: skip;
   - the new events are a superset (a cumulative RDSR): replace the study's events;
   - the events are disjoint (a continued study): add them;
   - a header-only study is later followed by an RDSR: the RDSR wins.
5. **Post-process:** device identity, standard names, not-patient rules, study totals, alert evaluation, and (later) skin dose.
6. **Archive or delete** the raw object according to policy.

### DX/CR priority
About 90 % of the local volume is DX/CR from image headers. Throughput therefore matters: batched inserts and a single transaction per study. The target is more than 50 studies/s on one core.

### Vendor adapters
Each quirk becomes a rule module with a test fixture. Examples:
- the exposure-index unit per manufacturer (S, EI, lgM, REX, EXI);
- SID reported in decimetres;
- protocol-name fallback chains;
- illegal DAP units (dGy·cm²);
- missing template identifiers;
- C-arm RDSRs from one vendor flagged as the wrong device type;
- DBT laterality derived from the protocol name.

The catalogue from the OpenREM analysis (§5 there) is the starting backlog.

### Query/retrieve
A scheduled C-FIND per modality and date window. Series are selected per modality: RDSR first, then Enhanced SR, then dose screenshots, then images (DX/MG). Existing studies are skipped, and there are include/exclude filters on station and description. Per-node options cover PACS quirks, for example ModalitiesInStudy at study level. This replaces ad-hoc pull scripts and provides back-fill for any date range.

---

## 7. Web application

- **Home:** studies per modality, and per-device totals and last-study date. Stale devices are highlighted, which catches broken feeds.
- **Study lists per modality:** filters as in OpenREM (§3.2 of the analysis) plus a local/external filter. Server-side paging and URL-encoded filters, so a filtered view can be bookmarked or shared.
- **Study detail:** a summary, a per-event table, a cumulative-dose context for the same patient, and alerts.
- **Charts:** every chart in the OpenREM inventory (§3.4), driven by the same filter set. The statistics come from the API as JSON and are rendered with ECharts: median, mean, boxplot, histogram, over time, scatter against mass or thickness, and workload heatmaps.
- **Exports:** CSV and XLSX per modality, as asynchronous jobs with a download list. Survey formats are pluggable.
- **Admin:**
  - devices: display names, merge, local/external, modality override;
  - standard names;
  - not-patient rules;
  - DICOM nodes and schedules;
  - alert rules;
  - users and roles;
  - jobs;
  - failed-ingest review.
- **i18n:** English and Romanian, as JSON message catalogues.

---

## 8. OpenREM legacy import

### 8.1 Access
OpenREM uses PostgreSQL. Two modes are planned:
- **CSV mode (default, no extra dependency):** a script runs `psql \copy (SELECT …) TO STDOUT CSV HEADER` for the needed `remapp_*` tables into a dump folder. Dozimetrix then reads that folder with the stdlib `csv` module. The dump can be produced on the OpenREM server itself.
- **Direct mode (optional extra):** read-only connection with `psycopg`.

### 8.2 Mapping
- **Version detection:** from `django_migrations` (0.10 versus 1.0 schema).
- **Studies:** `generalstudymoduleattr` maps to `study`.
- **Patients:** `patientmoduleattr` and `patientstudymoduleattr` map to `patient` and the patient fields on `study`.
- **Devices:** `generalequipmentmoduleattr` and `uniqueequipmentnames` map to `device`. The OpenREM display names are carried over, together with case-insensitive merge suggestions.
- **CT events:** CT events plus their source parameters and scanning length are flattened into `event`.
- **Projection events:** projection events plus their source, detector and mechanical data are flattened into `event`. The child tables for kVp, mA, exposure and pulse width are aggregated: mean, or the sum for exposure.
- **Coded values:** `contextid` values are resolved to text.
- **Settings and mappings:** not-patient indicators, standard names and alert thresholds are imported.
- **Batching and resuming:** the import runs in batches of about 10k studies and can be resumed. It is idempotent on study UID and event UID, so the same import can be repeated safely.

### 8.3 Patient identity in legacy data
OpenREM may have stored patient ID and name in clear, as unsalted SHA-256, or not at all, and this can vary over time.
- Hashed values are stored in `patient.legacy_sha256`, using OpenREM's exact recipe: `sha256(utf8(value))` with multi-value fields joined.
- For every clear ID, the same hash is computed so that hashed history links to known patients.
- Hashed accession numbers are imported as-is and marked as hashed; they cannot be recovered.

---

## 9. Exports, DRL surveys and reference levels

Dozimetrix does not adopt any single national template. It stores one **standards-based core dataset**, and every survey format is a thin converter on top of it.

### 9.1 Standards
| Layer | Standard |
|---|---|
| DRL methodology | **ICRP Publication 135**: a per room or facility median per exam; a minimum sample of about 20 patients (more for MG); a standard-size adult window of about 70 kg; the national DRL is the P75 of the facility medians. This is the basis of Euratom 2013/59 Art. 56. |
| Exam taxonomies | EC **RP 180** (European DRLs), **RP 185** (paediatric DRLs: weight bands for body exams, age bands for head), **RP 195 / EUCLID** (clinical-indication DRLs for CT) |
| Procedure codes | **RadLex Playbook / LOINC radiology** codes attached to standard names. SNOMED CT codes for clinical indications are optional. |
| Quantities and units | DICOM dose definitions (CTDIvol, DLP, DAP, Ka,r, AGD) with **UCUM** unit codes (`mGy`, `mGy.cm`, `Gy.m2`) |
| Registry exchange | **IHE REM**: de-identified RDSR export for dose registries |

### 9.2 Export products
1. **Dozimetrix DRL Dataset (versioned)**
   - One row per exam, as CSV, XLSX (own writer) or JSON, with a published data dictionary.
   - Each row holds:
     - pseudonymised facility and device codes, modality, manufacturer and model;
     - local exam name, standard code and indication;
     - age, sex, weight, height and the derived adult or paediatric band;
     - dose metrics with UCUM units, event counts and key technique factors.
   - A metadata header records the period, filters, software version and method.
2. **ICRP 135 summary**
   - Per room or facility × exam: n, median, mean, P25, P75, min and max.
   - Applies minimum-n and weight-window rules, and flags groups that are not compliant.
3. **De-identified RDSR export** (IHE REM), for future national or EU registries.
4. **Plain CSV and XLSX list exports** per modality, matching OpenREM's.
5. **Converters (later):** future Romanian template, UK UKHSA formats, IAEA forms. Each reshapes product 1 or 2.

### 9.3 Reference levels as data
DRL tables are stored as **versioned reference sets** (local, national, European RP 180/185/195, others). They are seeded with the **RP 185 EDRLs**, which use paediatric weight and age groups, and with the **current Romanian national DRLs**, which use age groups 1–4, 5–9, 10–14 and ≥15 years for radiography, CT, mammography, interventional and NM. Each entry holds an exam code, metric, value, unit, patient group, source and validity date. Charts, study flags and alerts compare against a selected set. A new national table is a data import, not a code change.

---

### 9.4 Regulatory reporting, national surveys and data quality
- **National reporting (Romania, first converter set):**
  - **Quarterly centralised report:** per national procedure code, the exam counts by age band and sex, and the mean dose (DAP, ESAK/MGD, DLP, RP dose or activity) by age group. Dozimetrix fills the official workbook layout directly.
  - **Per-exam DRL survey forms (art. 12):** one row per exam per exam-type sheet, with a partial national ID (sex and birth-date digits only), age, sex, height, weight, pregnancy, indication and dose.
  - **Single-sheet per-exam register:** a variant with pick-lists for exam code, indication, contrast or projection, and dose.
  - **Semestrial trend analysis:** quarterly means compared with the DRL over the last 6 quarters.
  - All of these are generated from the core dataset. The layouts are versioned converters, so a form revision is a new converter version.
- **Survey (anonymised) export:**
  - name, national ID and exact birth date are removed;
  - age is kept in days, months or years;
  - the exam date is reduced to month and year;
  - a **keyed pseudonymous patient code** (HMAC with a site secret) allows repeat examinations to be counted without re-identification;
  - formats: versioned CSV/JSON with a data dictionary;
  - a monthly scheduled export is possible.
- **Survey statistics (ICRP 135 / RP 185):**
  - the median per institution, procedure and group;
  - **the P75 of institutional medians** (the national DRL);
  - the national median;
  - the interquartile factor Q3/Q1;
  - checks of minimum sample size per group;
  - a comparison with EDRLs and national DRLs, with threshold bands (below the EDRL, from the EDRL to 1.5×, above 1.5×);
  - DRLs per clinical indication.
- **Data-quality rules engine,** whose results are shown per study and per device:
  - CTDIvol × scan length ≈ DLP (±10 %);
  - DLP, CTDIvol and DAP plausible for the exam and age group;
  - age and weight within physiological growth-reference ranges;
  - phantom consistent with the exam (a 32 cm phantom on a paediatric head scan is flagged);
  - duplicates;
  - **completeness KPIs**, for example the share of studies with a measured weight.
- **Aggregator mode (later):** one Dozimetrix instance can receive anonymised survey exports from many institutions, by upload or API, validate them and compute national statistics. The same code serves local sites and the central collection.

## 10. Security and privacy

- Patient identifiers are stored in clear and visible only to roles that have `view_pid`. Pseudonymised display and exports are available per role or setting.
- **Roles:** `viewer`, `exporter`, `pid` (view patient identifiers), `importer`, `admin`. They can be combined.
- **Local accounts:** passwords hashed with scrypt. LDAP/AD is optional later and would be written in-house or kept as a small, optional extra.
- **Audit log:** study views with identifiers, exports, admin changes and logins.
- **Retention policy:** for raw DICOM spool and archive, and optionally for identifiers after N years.

---

## 11. Testing

- Unit tests per adapter, using anonymised fixture files per vendor and modality.
- Upsert scenarios: cumulative, continued and duplicate RDSRs, and header followed by RDSR.
- Stats parity: the SQLite (Python) and MariaDB (SQL) implementations must give identical results.
- A legacy import test on a synthetic OpenREM-shaped CSV dump.
- Integration: SCP and QR against a local test SCP, then against the real PACS (read-only queries).
- Tooling: pytest and ruff (development dependencies only).

---

## 12. Roadmap

| Milestone | Content |
|---|---|
| **M1 — Core data** | Project skeleton, config, schema and migrations (SQLite and MariaDB) including institutions, code systems, group schemes, weight provenance and series semantics, SR walker, adapters for CT RDSR, projection RDSR and DX/CR headers, upsert, CLI file import, OpenREM legacy import (CSV mode) |
| **M2 — Browse** | Auth and roles, API, study lists and detail pages for CT/DX/RF/MG, device admin (display names, merge, local/external) |
| **M3 — Charts** | Stats layer, the full chart inventory, standard names |
| **M4 — DICOM networking** | Store SCP, scheduled QR with series selection, back-fill, ingest monitoring |
| **M5 — Exports and alerts** | CSV and own-XLSX exports, DRL dataset and ICRP 135 summary, periodic regulatory reports, anonymised survey export, data-quality rules, reference sets, RDSR export, fluoroscopy high-dose and cumulative alerts, notifications |
| **M6 — Advanced** | Aggregator mode for multi-institution surveys, MG header adapter polish, Philips Dose Info, NM/RRDSR, SSDE, skin dose maps, Docker |

---

## 13. Decisions and open questions

**Decided (2026-11-21):**
- **Frontend:** HTML, CSS and vanilla JavaScript only; no npm and no build step. Visual direction: **variant B "Console"**, provisional (§3).
- **Python:** 3.12 is the minimum, and the production host will provide it.
- **pynetdicom** is kept.
- **Legacy import:** CSV dumps are made on the OpenREM server and copied to the Dozimetrix host.
- **Notifications:** pluggable channels: e-mail over SMTP (stdlib `smtplib`) and **ntfy** (plain HTTP POST with stdlib `urllib`), with more to come.

- **Survey and DRL exports:** a standards-based core dataset (ICRP 135 method, RP 180/185/195 taxonomies, RadLex/LOINC codes, UCUM units, IHE REM RDSR export). National formats will be converters (§9).

- **Deployment scope:** a local collector first. Aggregator mode comes later (§9.4).
- **Seed vocabularies:** the proposed Romanian nomenclators for CT, conventional radiology and fluoroscopy, and clinical indications, plus the extended weight and age group scheme, ship as versioned seed data. They are credited to their author.
- **Growth-reference tables** (WHO weight-for-age, for the P5/P95 exclusion) are **downloaded at setup** and not bundled, because of their licence.

**Open:** none at the moment.
