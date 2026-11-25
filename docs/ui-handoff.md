# UI hand-over: findings from running the Console UI on real data

The UI now runs against the live API (`dozimetrix serve`, then log in). The pages receive the same shapes as `data/sample/`, plus these fields:
- `id` on studies, lookup results and previous studies;
- `accession_hashed`;
- `patient.age_days`, `patient.age_precision`, `patient.weight_source`;
- `events.total`, `dap_total` and `modality` on list rows;
- `totals.dlp_mgycm`, `totals.ctdivol_max`, `totals.n_events` on detail;
- per-event CT, detector and MG fields.

## Needed for real data

1. **Detail pages for CT, DX/CR and MG.**
   - `study.html` assumes fluoroscopy: it reads `thresholds.dap_gycm2`, and `thresholds` is `null` for other modalities, so a CT study fails with "Cannot read properties of null".
   - CT needs per-series rows: CTDIvol, DLP, phantom (16/32 cm), scan length, pitch, localizer flag.
   - DX needs per-exposure rows: DAP, kVp, mAs, EI/DI.
2. **List columns per modality.** The study list shows CT columns (EV S/A/L, DLP) for every modality. DX/CR rows show "0/0/0" and "—".
   - DX/CR need DAP and the number of exposures.
   - RF/XA need DAP, Ka,r and fluoro time.
   - The page title still says "Studies · CT" whatever the query.
3. **Many devices.** About 170 of the devices are other hospitals' scanners, seen through the PACS. Without `origin:local` the chart legends list dozens of them. Charts need a top-N plus "other" grouping, or `origin:local` as a non-removable default.
4. **No DRL set yet.** `/api/drl` returns an empty set on purpose. "Dots vs DRL" and the "% of local DRL" axis should say that no reference set is selected, instead of drawing a DRL line at 100 %.
5. **Raw protocol names.** Real protocol names are free vendor text, for example "Abd nativ K", "TOR ART" and "16.6.3 Abdomen-Pelvis 18.5 - 22.5 KG". Per-protocol charts become readable once standard names exist (milestone 3). Until then, label the axis "protocol (raw)".
6. **Paediatric grouping.** The main audit view should group by patient band:
   - RP 185 weight bands for body exams;
   - RP 185 age bands for head exams;
   - the national age groups 1–4, 5–9, 10–14 and ≥15.

   The API will expose the group schemes and reference sets. A "group by" selector on chart panels is the UI side.
7. **Hashed accessions.** These come as `#a1b2c3d4e5…` with `accession_hashed: true` (legacy data). Keep the monospace column wide enough, and don't offer "filter by accession" for them.
8. **Stale feeds.** All local feeds are currently stale, because nothing has been received since August. The overview shows this correctly. Consider sorting stale feeds first.
9. **Write actions.**
   - Device edits are persisted in API mode (admin role, audit-logged): display name, modality override (`auto` clears it), origin, merge, split (a "Split" button on alias rows) and dismissing a merge suggestion. The functions are in `core/api.js` (`updateDevice`, `mergeDevices`, `splitDevice`, `dismissMerge`); they clear the read cache after a write.
   - Still missing: alert acknowledgements, and a UI for the patient-identifier reveal (`POST /api/studies/{id}/identify`, pid role, audit-logged). The study page's "Reveal…" button is not wired yet.

## Milestone 3: server statistics, standard exams, reference levels

New server endpoints:
- **Exam classification:** every study now has standard exam codes: the radiography and CT nomenclators, plus national and European DRL codes. Radiography is classified per exposure, so codes carry the projection.
- **`GET /api/stats`:** one metric grouped by dimensions over the studies a query selects, with n, mean, SD, median, P25/P75, min/max and optional histograms. Parameters:
  - `q`;
  - `metric`: `dlp_total ctdivol_max dap_total dap_fluoro dap_acq kar_total fluoro_time exposures dlp ctdivol scan_length dap kar kvp mas ei di sid`;
  - `by`: comma list of `exam device age_band weight_band month quarter year modality sex protocol view`;
  - `system` (exam vocabulary): `ro-nom-ct`, `ro-nom-rx`, `ro-drl-ms838-2025`, `ec-rp185`;
  - `exam`: comma list of codes;
  - `age_scheme`, `weight_scheme`, `bins`, `min_n`.
- **Reference levels:**
  - `GET /api/drl/sets` lists the reference sets;
  - `GET /api/drl/compare?q=&set=&by=facility|device&min_n=` returns each exam × group × quantity with local quartiles, median/DRL and a status: `below`, `above`, `well_above` (more than 1.5×), `insufficient`, `no_data` or `unavailable`.
- **Exam mapping:**
  - `GET /api/classification/summary`, `GET /api/classification/unclassified?family=rx|rf|ct`, `GET /api/classification/mappings` and `GET /api/codes/{system}`;
  - `PUT /api/classification/mappings` (admin role, audit-logged) maps one text to a code and reclassifies the affected studies.
- All of these are wrapped in `core/api.js`: `stats`, `drlSets`, `drlCompare`, `classificationSummary`, `unclassified`, `mappings`, `codes` and `decideMapping`. They are server-only; in sample mode they reject with a message.

Two plain working pages were added in your style, and are yours to restyle or fold into other pages:
- `drl.html`: "Reference levels", a new rail entry. It shows the comparison table with a median/DRL bar and status tags, a facility / per-device toggle, a minimum n, and the shared query bar.
- `review.html`: "Protocol mappings", the admin tab of the same name. It shows coverage per family, the most frequent unclassified texts with a field and code picker, and the recorded decisions with "Remove".
  - **Known limitation:** the page takes about 10 s to load on the full history, because the unclassified radiography list groups 1.2 million exposures.

Requested for the charts workspace:
1. **Charts on server aggregates.** Panels should call `stats()` instead of downloading acquisitions. Server mode returns the percentiles directly, so the panels draw them and stop computing in the browser.
   - **Boxplot:** draw from `p25/median/p75/min/max`.
   - **Histograms:** pass `bins=N` and use the returned `bins` edges with each group's `hist` counts.
   - **Trend:** use `by=month` (or `quarter`/`year`).
2. **Group-by selector** per panel, from the dimensions above. The usual paediatric audit view is `by=exam,age_band` (national groups) or `exam,weight_band` (RP 185).
3. **Exam vocabulary selector:** "local nomenclator" (`ro-nom-ct` / `ro-nom-rx`) or a DRL vocabulary. Use the vocabulary's labels on axes, not raw protocol names.
4. **"Dots vs DRL":** take the reference value from `drlCompare()` rows. Hide the DRL line when the selected exam or group has none.
5. **Modalities beyond CT:**
   - radiography: `metric=dap` per exposure, with exam codes that include the projection;
   - fluoroscopy: `dap_total`, `kar_total` and `fluoro_time` per study.

## Milestone 5: exports, regulatory reports, data quality, alerts

More plain working pages, yours to restyle:
- `reports.html` (new rail entry "Reports"): the quarterly INSP report (OMS 1255/2016) preview with XLSX download, and the monthly anonymised paediatric survey export.
- `quality.html` (admin tab "Data quality"): rule counts, completeness per device, and studies with findings.
- `reports.html` also has the second grid (ICRP 135 / RP 185, extended; `GET /api/reports/grid`): head exams by age, body exams by weight, median vs RP 185 EDRL; the stats dimension `ref_group` gives the same groups in charts.
- `drl-sets.html` (admin tab "DRLs"): the reference sets and their values, read-only (seed-owned); the comparison stays on `drl.html`.
- `settings.html` (admin tab "Settings"): the site name and code, the web address, spool retention (editable); deployment info (read-only).
- `rules.html` (admin tab "Alerts"): the notification channels (e-mail, ntfy: settings, test, status), then enable, threshold, severity and notify per rule, with add and delete for rules an admin created.
- The overview alert queue now uses real alerts (`GET /api/alerts`). "Acknowledge" calls `POST /api/alerts/{id}/ack`.
  - API alerts carry `study_id`; their `id` is the alert's own id.
  - `value` and `limit` are short texts.

Requested:
1. **Study detail:** `GET /api/studies/{id}` now has `quality: [{rule, severity, title, detail}]`. Show it near the readouts. Also show the study's open alerts: `GET /api/alerts?status=all` filtered by `study_id`, or ask for a `study_id` filter.
2. **Alerts page:** the rail's "Alerts" entry still points to the overview. A full page could list open and acknowledged alerts, add a note on acknowledge (`{note}`), and filter by rule.
3. **Export buttons:** the study list uses `exportUrl()`. Other lists could offer `level=event` (one row per series or exposure) and `pid=1` for users with the pid role.

## Done in the backend session (2026-11-22)
- **Charts workspace:** server panels in `pages/charts-server.js`: boxplot, median/mean bars, trend, histogram, and median vs DRL. Each panel has its own modality (CT, DX, CR, RF, XA, MG), metric, grouping (exam in any vocabulary, device, age or weight group, projection, protocol, sex, period), second-dimension series, and minimum n. They use the workspace query. The CT-only browser panels are kept and labelled "CT …". New default workspaces include a vs-DRL panel and a DX boxplot. The chart theme gained `ok`.
- **Dose by size band** (`ssize` panel, `GET /api/charts/size-bands?device=&kind=weight|age`): every radiography protocol of one unit at once. X is the size band the operator picked, written in the protocol name (`5-15Kg`, `1-6 ani`, `>50 Kg`); the patient's own weight and age are not used. Y is DAP per exposure on a log axis: the median with a P25–P75 bar, a line joining a protocol's bands, the national DRL as a small marker. Colour is the anatomy, marker and line style the projection. `dozimetrix/rxprotocols.py` parses the names: the program-set code in front (W035, T035, X035, OT_) is dropped so the sets merge into one anatomy (it stays in the tooltip); a name with no band (Sinusuri) goes in a "no band" slot at the right of both views. Only the 8 busiest anatomies start switched on; the rest are in the legend.
  - Open for you: restyle, and possibly retire the browser-side CT panels once you're happy with the server ones. (Sharing workspaces server-side was done 2026-11-25: `/api/views`, see design.md §10.)
- **Alerts page** (`alerts.html`, the rail's Alerts entry): filters, per-rule counts, paging, and bulk acknowledge with a note.
- **Study detail** for CT, DX and MG (`pages/study-generic.js`). "Reveal…" now calls the audited identify endpoint.
- **Admin:** DICOM nodes, Users and Audit log pages, with one shared tab bar (`components/admin-tabs.js`).

## Protocol comparison and local DRLs (2026-11-24/25)

New API, no page yet (the backend session leaves the page to you):

**`GET /api/protocols/compare`** (`protocols.py`): the protocols of one CT region side by side.
Params: `region` (exam code, default vocabulary the national DRL: `CT-CAP` gathers head, low-dose head and head trauma; `system` selects another, e.g. `ro-nom-ct`), `q`, `start`, `end`, `grid` (`icrp` = head by age, body by weight; `national`), `by` (`protocol` | `family`: merges contrast / phase / band variants such as `CER_NATIV`, `CER K`), `by_device` (true: one row per room), `min_n`, `estimated`.
Each row: `band`, `band_label`, `phantom_cm` (16 / 32: never mixed), `device_name`, `protocol` (normalised) with `names` (raw spellings), `n`, `studies`, `stats[metric]` = `{n, mean, sd, min, p25, median, p75, max}` for `ctdivol`, `dlp`, `kvp`, `ma`, `rotation_s`, `pitch`, `collimation_mm`, `scan_length_mm`, `emas` (mA × rotation / pitch), `hist` = `{edges, counts}` on one scale per group and phantom, `rank` (by median CTDIvol among rows with `enough`), `ratio_to_lowest`, `n_from_name` (band taken from the protocol name, e.g. `CT_CAP_N_1_6_ANI`), `n_name_mismatch`.
Suggested page: region picker, group tabs (the bands), one table per group and phantom with the rank and a ratio-to-lowest bar, a small histogram or box per protocol on the shared scale, columns for kV / mAs / pitch / collimation. Say plainly that dose is the only thing ranked: image quality (noise, slice thickness) is not in the data.

**Local DRLs** (`local-drl.html`, already updated): rule select (pooled P75 from 30 values else median, or ICRP room rule), "Values for P75", exam scope (all / national only), "local only" tag for exams without a national or European DRL, "room outlier" tag. Radiography local-only exams have one row per view (`projection`: `AP/PA` or `LL`); reference values and comparison rows now carry `projection`.
