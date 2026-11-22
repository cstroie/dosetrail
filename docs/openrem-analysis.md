# OpenREM analysis — goals for a modern replacement

Date: 2026-11-21

**Sources** (OpenREM is GPLv3. This document describes its behaviour only. No OpenREM code is reused in Dosimetrix.)
- Source code: `gitlab.com/openrem/OpenREM`, tag `0.10.0` (Nov 2019, Python 2.7/3, Django 2.2, Celery) and branch `develop` = `1.0.0b3` (Python 3.10+, Django 3.2, huey/redis).
- Docs: 0.10.0.
- Live demo: `demo.openrem.org`, logged in as `demo/demo`. It runs 1.0.0b3 with 2000 CT, 1599 DX, 5 RF, 3 MG and some NM studies.

---

## 1. What OpenREM is for

OpenREM collects radiation-dose data from imaging equipment, normalises it into one database, and lets medical physicists and radiation-protection staff do the following:

1. **Browse** studies per modality with rich filtering, and drill down to per-irradiation-event detail.
2. **Audit**: build median/mean/boxplot/histogram charts per protocol, study description and requested procedure, per system and over time. This is the input for local **DRLs** (diagnostic reference levels) and optimisation.
3. **Export** to CSV/XLSX and to national survey formats (UK PHE 2019, NHSBSP).
4. **Alert** on high-dose fluoroscopy: total DAP, total reference-point dose, and doses accumulated over N weeks for the same patient. Alerts go by email.
5. **Estimate peak skin dose** for interventional fluoroscopy (openSkin), shown as a 2D and 3D map.
6. **Ingest automatically** from modalities and PACS (C-STORE SCP and C-FIND/C-MOVE query-retrieve), with vendor-specific workarounds.

---

## 2. Inputs (ingestion)

| Input | Modality | Notes |
|---|---|---|
| **RDSR** (X-Ray Radiation Dose SR, TID 10001/10011) | CT, RF/XA, DX, MG | Primary, preferred source. A single parser walks the SR content tree. |
| **Enhanced SR** (GE) | CT | Accepted only when it is confirmed to be a GE dose report. |
| **RRDSR** (Radiopharmaceutical Radiation Dose SR, TID 10021) | NM/PET | 1.0 only. 0.10 has no NM. |
| **NM/PET image headers** | NM/PET | 1.0 only (`nm_image.py`), used to complement the RRDSR. |
| **DX/CR image headers** (CR and DX IODs) | DX/CR | One "event" per image. The exposure-index unit is inferred from the manufacturer. |
| **MG image headers** | MG | GE Senographe, Hologic Selenia (including DBT proprietary objects), Siemens. Also captures rejected images. |
| **Philips CT "Dose Info"** secondary capture | CT | Older Philips scanners: dose data in private/SC tags. |
| **Toshiba/Canon CT dose screenshot** + image headers | CT | Builds an RDSR by shelling out to **Java pixelmed.jar + dcmtk** (`dcmconv`, `dcmmkdir`). Fragile and heavy. |
| **CSV patient height/weight** | all | Matched by accession number or Study UID. Can overwrite existing values. |

### Duplicate and cumulative RDSR handling (`extractors/rdsr.py`)
Matching runs first on Study Instance UID, then SOP Instance UID, then the set of **Irradiation Event UIDs**:
- SOP already processed: skip.
- New events are a subset of existing events, or the same events: skip.
- New events are a superset (a *cumulative* RDSR, e.g. Siemens writes one after every exposure): delete the old study and import the new one.
- Events are disjoint (a *continued* study): store a second study entry.
- RRDSR arrives after the NM images were imported: append.

**Lesson:** event-UID-based idempotent upsert is the right idea. It just needs to be the core of the design, not a patch added after the fact.

### DICOM networking
- **Store SCP:** the native pynetdicom store was "not stable in the long term". The official recommendation was **Orthanc** as the receiver, with a Lua script that hands files to OpenREM.
- **Query/Retrieve SCU** (`netdicom/qrscu.py`, 2.6k lines):
  - Query by modality and date (or time window).
  - Include/exclude lists on study description and station name.
  - Option: "Ignore studies already in the database".
  - Option: "Include SR-only studies".
  - Option: "Get SR series that return nothing at image level".
  - Option: "Attempt to get Toshiba dose images".
- **Series pruning logic per modality:**
  - CT: RDSR, then ESR, then Philips Dose Info, then (optional) Toshiba SC plus one image per series. Series with more than 5 images are dropped.
  - MG and DX: RDSR is preferred over images.
  - RF/XA: SR only.
  - Duplicate check by Study UID, then SOP UID.
- **PACS quirks:** some PACS (Impax 6.6) need ModalitiesInStudy at study level, and others return nothing if it is sent. This is a per-node flag.

---

## 3. Reports and screens to match

### 3.1 Home
- Study count per modality.
- Per-modality table of **system display name, total studies, latest study date**. The last two are configurable: "days since", "studies in last N days".

### 3.2 Study list per modality — filters (from the live demo)

Common filters:
- Date from/until.
- Study description, Procedure, Requested procedure, Acquisition protocol.
- Hospital, Make, Model, Station name, Display name.
- Min/max age, min/max weight.
- Accession number.
- "Include possible test data".
- Ordering, items per page.
- Patient name/ID, only for users with PID rights.
- Standard name, when standard names are enabled (1.0).

Modality-specific filters:

| Modality | Extra filters | List columns |
|---|---|---|
| **CT** | Min/max study DLP; number of events total / spiral / axial / localisers / stationary | Institution · Make/Model/Display name · Date · Study description/Procedure/Requested procedure/Accession · No. events · Total DLP · … |
| **DX** | Min/max **study** DAP; min/max **acquisition** DAP; number of events | … · No. events · Total DAP (cGy·cm²) |
| **RF** | Physician; min/max study DAP | … · No. events · Total DAP · **Total dose at RP (Gy)** · **DAP and RP dose summed over 12 weeks before the study** · Physician |
| **MG** | View code; breast-thickness range; exposure control mode | … · No. events · Accumulated AGD (mGy) |
| **NM** | Min/max administered activity (MBq) | … · Radiopharmaceutical · Administered dose (MBq) |

### 3.3 Study detail pages

**CT**
- Header: totals (events, DLP).
- Per event:
  - Acquisition protocol, standard acquisition name, type (spiral/axial/…).
  - CTDIvol, DLP, scanning length.
  - kVp, mA, max mA, rotation time, pitch, exposure time.
  - Slice thickness, collimation, modulation type.
  - Phantom (16/32 cm).
  - Dose check (alert/notification) details, SSDE/WED (1.0).

**DX**
- Totals (events, DAP).
- Per event:
  - Time, protocol, standard protocol.
  - kVp, mAs, mA, exposure time.
  - Filters, SID, grid, focal distance.
  - **Exposure index / Deviation index** with a vendor-specific unit (S, EI, lgM, REX, EXI…).
  - DAP, exposure control mode, comment.

**MG**
- Accumulated AGD left/right/both.
- Per event:
  - Time, protocol, laterality, view code + modifier.
  - Compression thickness, target/filter.
  - kVp, mA, ms, mAs.
  - **ESD**, **AGD**.
  - Exposure control mode + description.

**RF** (see screenshot `rf_detail.png`)
- Study summary list:
  - Accession, date/time, description, procedure, age, height/weight.
  - Total DAP and dose at reference point per plane.
  - Hospital, equipment, display name, Study UID, physicians, operators.
  - The "test patient indicator" that matched.
- Summary table: per irradiation type (Fluoroscopy / Acquisition / Total), total DAP, total RP dose and total duration.
- Same-patient studies over the previous N weeks (cumulative-dose alert context).
- **Skin dose map**, 2D unfolded and 3D WebGL, with peak skin dose.
- Event table:
  - Time, protocol, event type, pulse rate, plane.
  - DAP, RP dose, duration/exposure time.
  - kVp, mA, filters (Cu/Al mm).
  - Primary/secondary angle, detector size, SID, orientation.
- Vendor private comment dumped raw. Siemens XML is shown as-is, which is ugly.

**NM**
- Radiopharmaceutical, radionuclide, administered activity, pre/post activity and times, route, organ doses, PET series details, patient state, GFR.

### 3.4 Charts (complete inventory from the 1.0 form)

**Global options**
- Average: mean / median / **boxplot** (the demo uses median plus boxplot plus histogram).
- Grouping: by system or by protocol.
- "Plot a series per system".
- Histograms and bin count.
- Time period: day / week / month / quarter / year.
- Sort by name / value / frequency, with direction.
- Case-insensitive categories.
- Colour map and theme.
- Facet wrap.

**Download:** PNG, or data as CSV.

**CT** (each comes in a raw-name and a **standard-name** variant):
- Acquisition-level:
  - Frequency.
  - Mean/median CTDIvol and DLP.
  - CTDIvol and DLP over time.
  - CTDIvol and DLP vs patient mass.
  - Acquisition types to include: sequenced, spiral, constant angle, stationary, free, cone beam.
- Study-level:
  - Frequency.
  - DLP, CTDIvol, number of events.
  - DLP over time.
  - Workload by weekday and hour.
- Requested procedure: frequency, DLP, events, DLP over time.

**DX**
- Acquisition: frequency, DAP, mAs, kVp, each over time; DAP vs mass.
- Study: frequency, DAP, DAP vs mass, workload.
- Requested procedure: frequency, DAP, DAP vs mass.

**RF**
- Study and requested procedure: frequency, DAP, DAP over time.
- Workload.
- Optional "split plots by physician".

**MG**
- Acquisition: frequency, average AGD, AGD over time.
- **AGD vs compressed thickness** (scatter and binned average).
- **kVp vs thickness**, **mAs vs thickness**.
- Workload.

**NM**
- Study frequency, workload.
- Injected activity per study, over time, and vs mass.

**Demo account menus** (login `demo`/`demo`, which is a view-only user):
- Config menu: Chart options, Switch charts off, View display names & modality, View not-patient indicators, Standard name mapping.
- User menu: Change password, Logout.
- The demo account has no export permission ("No export permissions").
- The chart-options page sets, per user:
  - General options: average type, histogram bins, fixed bins across subplots, label wrap length, grouping (system or protocol), theme, sub-charts per row, and about 17 colour maps.
  - For each modality: a checkbox per chart, a time period, and a default sort.
- Screenshots were taken for internal reference and are not published.

### 3.5 Exports (asynchronous, with progress, listed and downloadable)
- **CSV** (one row per study, events flattened into columns) and **XLSX** (a summary sheet, an all-data sheet, and **one sheet per protocol**), for each modality.
- RF XLSX groups similar exposures (plane, protocol, field size, pulse rate, filter, angle within 5°).
- UK-specific formats:
  - **PHE 2019** CT (adult and paediatric), DX (single projection or multi-projection study), RF.
  - **NHSBSP** mammography dose audit.
- **openSkin CSV** for the standalone skin-dose tool.
- Patient name/ID columns only when the user has PID and export rights.

### 3.6 Fluoroscopy high-dose alerts
- Thresholds: total DAP, total RP dose, and peak skin dose.
- Accumulation window of N weeks (default 12) per patient ID.
- Computed on import.
- Email recipients per user.
- Requires the patient ID or its hash to be stored.

### 3.7 Skin dose (openSkin)
- Geometric phantom: a cuboid plus two half-cylinders, scaled from height and mass (defaults 1.786 m / 73.2 kg).
- kVp-dependent backscatter and Cu-filter correction.
- Uses table and beam geometry from the RDSR.
- Results are cached as pickles.
- Documented as working reliably only for **Siemens Artis Zee**. 1.0 adds a per-model "safe list".

---

## 4. Administration features

- **Display names**: one row per unique equipment tuple (manufacturer, institution, station, department, model, serial, software version, gantry ID, device observer UID).
  - Admins assign a friendly name and can *merge* several tuples into one display name.
  - Each tuple has a user-defined modality override ("Dual" DX/RF systems).
  - Option: "merge on Device Observer UID".
- **Standard names** (1.0): map a free-text study description, requested procedure, procedure or acquisition protocol to a harmonised name per modality. Charts can use either the raw or the standard name.
- **Not-patient indicators**: wildcard patterns on patient name or ID flag QA/test/phantom studies. These are excluded unless "include possible test data" is set.
- **Patient ID policy**:
  - Store name, ID and DOB, or not.
  - Store as SHA-256 hash (unsalted, case-sensitive).
  - Hash the accession number, optionally.
- **DICOM delete policy**: delete received objects after import, per type.
- **Roles**: `viewgroup`, `exportgroup`, `pidgroup`, `importsizegroup`, `importqrgroup`, `admingroup`.
- **Task admin**: running/queued/failed background tasks.
- **Failed-import review**: studies with missing equipment or partial data can be reviewed and deleted.
- **Summary-field population**, run as an upgrade task.

---

## 5. Vendor quirks catalogue

These become test fixtures and *vendor adapter* rules in the new design.

| Vendor / model | Quirk | OpenREM handling |
|---|---|---|
| Fuji | DX exposure index is `S` | unit `S ()` |
| Carestream / Kodak | EI in mbels | `EI (Mbels)` |
| Agfa | lgM in bels | `lgM (Bels)` |
| Konica | `S` | |
| Canon (DX) | `REX` | |
| Swissray | `DI` | |
| Philips (DX) | `EI` | |
| Siemens (DX) | `EXI` in µGy | |
| Kodak DR 7500 | SID in **dm** instead of mm | ×100 |
| Toshiba Radrex (`TOSHIBA_MEC`, `TM_TFD_1.0`) | Protocol name in ImageComments | fallback chain ProtocolName, then ImageComments, then SeriesDescription, then PerformedProtocolCodeSequence |
| Toshiba CT | illegal multi-valued vHP in RDSR; PixelMed-built RDSR | `_fix_toshiba_vhp` |
| Toshiba CX/CXL (old) | no RDSR, only a dose screenshot | Java + pixelmed rebuild |
| Siemens Arcadis | private `99SMS_RADSUM` template | special-cased |
| Siemens CT | **cumulative** RDSR after each exposure | event-UID superset logic |
| Siemens Artis | private XML in study comment | displayed raw |
| Philips Allura Xper | SID reported only when it changes; private data | carry the value forward |
| Philips Azurion | wrong AcquisitionDeviceType | override |
| Philips BigBore 4DCT, Opera Swing, Varian, GE Elite/OEC C-arms | malformed or missing template declaration | specific fixes |
| Various | DAP in illegal unit `dGy.cm2` | unit conversion |
| Hologic DBT | laterality missing on projections | derive it from the protocol name (L/R prefix) |
| Carestream | multiple filter thicknesses | multi-value |
| Agfa/Impax 6.6 PACS | ModalitiesInStudy behaviour | per-node flag |

---

## 6. Design mistakes to avoid

1. **ORM that mirrors the DICOM template tree 1:1.** There are about 100 tables. For example kVp, mA and exposure are *child tables of* `IrradEventXRaySourceData`, which is a child of `IrradEventXRayData`, which is a child of `ProjectionXRayRadiationDose`, which is a child of the study. Every chart is a 5–6 level join.
   - The fix they bolted on was denormalised `total_dlp`, `total_dap_a`, `number_of_events…` on the study, back-filled by a long migration task that blocked QR.
2. **Coded values in one generic `ContextID` table** (code_value, meaning, cid). It is populated lazily and needs a join for every coded attribute.
3. **Hard-coded vendor logic** scattered through extractors (`if "fuji" in manufacturer`), with no registry and no tests per quirk.
4. **Chart statistics computed in Python views** through pandas over ORM querysets. Medians only worked on PostgreSQL. Slow on large datasets.
5. **Task infrastructure churn:** Celery plus RabbitMQ plus Flower, then huey plus redis. Windows pinned to Celery 3. The native DICOM store was unstable, so it was replaced by Orthanc plus Lua.
6. **External binaries** (Java pixelmed, dcmtk) needed for one vendor.
7. **Patient identity:**
   - Unsalted SHA-256 can be dictionary-attacked and is case- and whitespace-sensitive.
   - Hashing makes cumulative-dose linkage brittle.
   - Settings changes are not retroactive.
8. **Units:** DAP is stored in Gy·m² and displayed in cGy·cm². Conversions are scattered through templates.
9. **UI:** server-rendered Bootstrap 3 pages, a 1000-line filter form in a sidebar, and vendor XML dumped raw. There is no API.
10. **Licence:** GPLv3. **We will not copy code.** We use only the published DICOM standards and observed behaviour, so the MIT licence stays clean.

---

## 7. OpenREM → new DB importer: source-schema notes

- Tables are Django tables named `remapp_<modelname-lowercase>`. Read them with **plain SQL or SQLAlchemy reflection**, never by importing OpenREM code.
- The core dose schema is the same in 0.10 and 1.0. 1.0 adds:
  - NM: `RadiopharmaceuticalRadiationDose`, `RadiopharmaceuticalAdministrationEventData`, `OrganDose`, `PETSeries…`.
  - `StandardNames`, `SkinDoseMapResults`, `OpenSkinSafeList`, `BackgroundTask`.
  - `GeneralStudyModuleAttr.standard_names` (M2M).
  - 0.10 instead has the `Median` SQL aggregate helpers and Celery-based tasks.
- Detect the version via the `django_migrations` table: the last `remapp` migration name.
- Key joins (study root `remapp_generalstudymoduleattr`):
  - `patientmoduleattr`, `patientstudymoduleattr`, `generalequipmentmoduleattr` → `uniqueequipmentnames` (display names).
  - **CT:** `ctradiationdose` → `ctaccumulateddosedata`, and `ctirradiationeventdata` → `ctxraysourceparameters`, `scanninglength`, `ctdosecheckdetails`.
  - **Projection:** `projectionxrayradiationdose` → `accumxraydose` → `accumprojxraydose`, `accummammographyxraydose`, `accumintegratedprojradiogdose`.
  - **Projection events:** `projectionxrayradiationdose` → `irradeventxraydata`, which links to `irradeventxraysourcedata` (`kvp`, `xraytubecurrent`, `exposure`, `pulsewidth`, `xrayfilters`, `xraygrid`), `irradeventxraydetectordata` and `irradeventxraymechanicaldata` → `doserelateddistancemeasurements`.
  - Coded values: `contextid`.
- Configuration to carry over:
  - Display names and their merges.
  - Standard names.
  - Not-patient indicators.
  - Alert thresholds.
  - DICOM nodes.
- Patient identity depends on the site's PatientIDSettings. Names and IDs may be absent, in clear, or SHA-256 hashed. This decides whether cumulative per-patient dose can be rebuilt for historical data.

---

## 8. Proposed goals for the new system (draft)

- **Parity:** every input, list, detail page, chart and export in §2–§3, plus admin features in §4.
- **Fast:**
  - A **flat, analytics-friendly schema**: study, then irradiation event (one wide row per event with typed columns for the common metrics), then an optional JSONB/JSON blob for the full SR tree and vendor extras.
  - Precomputed per-study totals.
  - Statistics in SQL on MariaDB (`MEDIAN`/`PERCENTILE_CONT`), with a pure-Python fallback for SQLite.
- **Pluggable vendor adapters:** a registry keyed on (manufacturer, model, software version, SOP class), each with fixture-based tests.
- **Robust ingestion:** a native async C-STORE SCP and QR (pynetdicom), with an idempotent event-UID upsert. Raw objects are optionally archived for re-processing when parsers improve.
- **Modern UI and REST API**, i18n-ready (at least English and Romanian).
- **Privacy:** patient identifiers are stored in clear and shown only to authenticated users with the right role. Pseudonymisation is optional. Retention is configurable, and access is audit-logged.
- **Regulatory reports:** a pluggable "survey export" framework covering EU/national DRL formats, not only the UK PHE and NHSBSP formats.
