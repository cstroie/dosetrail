# Deploying DoseTrail (plain install)

A single machine runs everything: MariaDB, the web UI/API, the DICOM receiver and an hourly
query/retrieve job. Docker images will follow; this guide uses systemd only.

## 1. System
- Python ≥ 3.12 with the `sqlite3` module (only needed for tests), MariaDB ≥ 10.6.
- A service account: `useradd --system --home /var/lib/dosetrail --shell /usr/sbin/nologin dosetrail`
- Directories: `/opt/dosetrail` (code + virtualenv), `/etc/dosetrail` (config, 0750
  root:dosetrail), `/var/lib/dosetrail` (secret, spool; 0700 dosetrail).

## 2. Database
```sql
CREATE DATABASE dosetrail CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'dosetrail'@'localhost' IDENTIFIED BY '…';
GRANT ALL ON dosetrail.* TO 'dosetrail'@'localhost';
```
Give InnoDB enough memory to hold the working set — at least the size of the data
(`innodb_buffer_pool_size`, e.g. 2G for about 600 000 studies). On spinning disks this
is the difference between 25 ms and 10 s per page of results.

## 3. Install
```sh
python3.12 -m venv /opt/dosetrail/.venv
/opt/dosetrail/.venv/bin/pip install dosetrail        # or: pip install -e <checkout>
cp deploy/dosetrail.toml.example /etc/dosetrail/dosetrail.toml   # edit it
sudo -u dosetrail DOSETRAIL_CONFIG=/etc/dosetrail/dosetrail.toml dosetrail migrate
sudo -u dosetrail DOSETRAIL_CONFIG=/etc/dosetrail/dosetrail.toml dosetrail seed
sudo -u dosetrail DOSETRAIL_CONFIG=/etc/dosetrail/dosetrail.toml dosetrail user set admin --roles viewer,exporter,pid,importer,admin
```
The site secret (`secret_file`) is created on first use. Back it up: pseudonyms and
sessions depend on it.

## 4. Services
```sh
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dosetrail-web dosetrail-receive dosetrail-qr.timer
systemctl enable --now dosetrail-his   # only with a HIS server (see below)
```
- `dosetrail-web` listens on `[web] host:port` (default 127.0.0.1:8104); publish it through an HTTPS reverse proxy
  (nginx, Apache) and set `secure_cookies = true`.
- `dosetrail-receive` listens for DICOM on `[dicom] port` (11112). Register
  `AE DOSETRAIL / host / 11112` on the modalities and the PACS that should send dose
  objects; restrict callers with `allowed_aets`.
- `dosetrail-qr.timer` queries the PACS every hour for the last two days, one day and one
  modality at a time (many PACS cap the number of studies a query returns; set
  `max_results` and capped days are split into time windows).
- Three ways to get data in:
  - **C-GET** (`retrieve = "get"`, default) – nothing to configure on the PACS;
  - **C-MOVE** (`retrieve = "move"`) – register `DOSETRAIL` (host, port 11112) on the PACS;
    objects arrive at the receiver; `dosetrail qr` starts a temporary receiver if the
    service is not running;
  - **push** – configure modalities or the PACS to send dose objects to the receiver.

## 5. Importing history
- OpenREM: `dosetrail openrem-dump --ssh root@openrem-host` then `dosetrail openrem-import`
  (see the reconciliation report it prints).
- Back-fill from the PACS: `dosetrail qr --from 2026-08-01 --to 2026-11-21` (resumable;
  finished days are remembered).
- New devices start as *external*; mark your own rooms local on Admin → Devices.

## Alerts and notifications
- Default alert rules are seeded by `dosetrail seed`; edit them in the UI (Admin → Alert rules) or through `PATCH /api/alert-rules/{id}`. A seed run never overwrites an edited rule.
- New studies are evaluated as they are imported. To evaluate history, run `dosetrail alerts evaluate --since 2026-01-01`.
- Notifications are sent by the receiver service when it is idle, at most every 30 s. They can also be sent with `dosetrail alerts notify`. Only alerts on studies from the last 48 h are sent.
- Configure `[notify]` (see `dosetrail.toml.example`), then check it with `dosetrail alerts test-notify`.

## Growth tables (setup step)
- Run `dosetrail growth install` once; it downloads the weight-for-age tables into `<data_dir>/growth`:
  - the WHO Child Growth Standards for 0–5 years;
  - the WHO 2007 Growth Reference for 5–10 years;
  - the CDC 2000 growth charts (2–20 years), used only beyond 10 years, where WHO has no weight-for-age reference. Rerun `install` on older setups to add it.
- They are WHO and CDC data under their publishers' terms (see `SOURCE.txt` there), so they are not shipped with DoseTrail. `dosetrail growth status` shows whether they are installed.
- With the tables, data quality flags weights outside P1–P99 for age and sex. Studies of children (< 18 y) below P5 or above P95 are left out of DRL statistics (`study.drl_exclusion`), as the national paediatric survey plan asks. Missing or implausible weights (outside P1–P99) are replaced by the median weight for age and sex (`weight_source = estimated_reference`; an implausible reported value is kept in `weight_reported_kg` and still reported by data quality). Estimates are marked on the study page, in exports and in the second-grid report; patients over 20 years, or without age or sex, keep no weight. Run `dosetrail growth check` after importing weights, e.g. from the RIS (a reported weight replaces an estimate); new studies are estimated and checked as they arrive.

## Weight / height from a CSV file

`dosetrail sizes FILE.csv` reads weights and heights from any export (RIS, ward records, a spreadsheet) and prints what it would change; `--apply` writes them (audited), then recomputes estimates, DRL exclusions and alerts for the changed studies. Admins can do the same on Admin → Data quality ("Import weight / height").

- Key per row: `study_uid`, or `accession` (also matches the hashed accessions of legacy imports), or `patient_id` + `date` (every study of the patient that day; add `modality` to narrow it).
- Values: `weight_kg`, `height_m` or `height_cm`. `,` or `;` separated; decimal commas are accepted.
- Provenance: `source` (`measured`, `chart_same_day`, `chart_72h`, `imported`) or `measured_at` (a date: same day as the exam → chart_same_day, within 72 h → chart_72h). Neither → `imported`.
- A value replaces the stored one only when its source ranks higher (measured > same day > 72 h > imported/DICOM > HIS chart > estimate), and only when plausible for age and sex.

## Dose SR export (IHE REM)

`dosetrail rdsr --study ID --out DIR` (or `--query 'modality:CT date:30d'`) writes one X-Ray Radiation Dose SR per study: TID 10011 for CT, TID 10001 for radiography, fluoroscopy and mammography, built from the stored values. Files are anonymised (pseudonymous patient ID; no name, birth date, accession or physicians; stable derived UIDs) unless `--identified`. Estimated weights are not written. Users with the exporter role can download a study's file from the study page ("Dose SR"; `GET /api/studies/{id}/rdsr`). DoseTrail only writes files: it does not send dose SRs to a PACS or registry.

## DICOM nodes
- Remote AEs are managed in Admin → Integration:
  - **query/retrieve** nodes are the PACS that `dosetrail qr` pulls from;
  - **sends** nodes are modalities or PACS allowed to send to the receiver.
  The receiver re-reads the allowed list every minute. Changes are audit-logged, and each node has a C-ECHO test.
- `[pacs]` in `dosetrail.toml` is imported once as a node named "PACS". After that, the database is used. `dosetrail qr --node NAME` picks a node. `--host/--aec` still overrides for one run.
- The receiver and `qr` write a heartbeat that the page shows (running/stopped, objects per sending AE, sync progress). It also shows a 120-day coverage strip of fully retrieved days.
- Stop the receiver with SIGTERM (systemd does), not Ctrl-C.

## HIS / RIS (patient, weight, height, request, indication)
- Add the server in Admin → Integration → HIS / RIS servers, then press **Test** (or run
  `dosetrail his check`). Supported: a HippoBridge FHIR gateway. Use a HIS account with read
  access. The password is stored encrypted with a key derived from the site secret, and is
  never shown again; if the site secret changes, enter the password again.
- With an enabled server, every new study whose exam is at most 7 days old is queued.
  Older exams arriving now (a PACS or legacy backfill) are history: they get the CNP
  (sex, birth date, exact age) and growth-chart weight estimates, not HIS lookups. Use
  `dosetrail his backfill` for them, if ever. The `dosetrail-his` service
  (`dosetrail his run`) works through the queue one study at a time, pausing between calls
  (`min_interval_s`):
  1. it finds the patient by the patient ID (a national ID such as the CNP, or a HIS code);
  2. sex and birth date follow CNP > HIS > DICOM, and the exact age is recomputed;
  3. it takes the imaging request of the same type ordered closest before the exam
     (window: `window_before_h` / `window_after_h`), which supplies weight, height,
     pregnancy, ward, requester, indication and guideline decision;
  4. weight and height are written when plausible for the age and sex, by rank: measured >
     request on the exam day (`chart_same_day`) > request ≤ 72 h (`chart_72h`) > DICOM
     header > HIS patient record, which is undated (`his_chart_undated`) > estimate.
- New studies whose request isn't found are tried again after 15 min, 2 h and 24 h.
- Per device, Admin → Devices → HIS limits lookups to the patient (e.g. a room ordered
  through another HIS) or turns them off.
- A HIS record or national ID whose birth date is 12 or more years from a real header birth
  date is treated as another person's (for example, a baby registered under a parent's ID)
  and not used.
- Try it without writing: `dosetrail his run --dry-run --limit 20`.
- History: `dosetrail his backfill --since 2026-01-01 [--until …] [--device ID]` shows how
  many studies would be queued and roughly how long it takes; `--apply` queues them. The
  worker runs history only within the server's backfill hours (e.g. 22-06), children first,
  after the day's new studies.
- The study page shows the matched request (ward, requester, indication, guideline
  decision) and where the weight came from; admins can queue a study for another lookup.
  Data quality flags HIS lookups that found no patient (`his_unmatched`) and disagreements
  between CNP, HIS and DICOM (`demographic_conflict`). The survey export carries the
  request's indication as free text (`indication_text`): review it before sending.
- Use a HIS account of its own for DoseTrail, not one another service already uses (for
  example the modality worklist's): the HIS may keep a single session per account.
- National IDs alone (offline, no HIS): `dosetrail cnp backfill` shows what would change;
  add `--apply` to write it.
