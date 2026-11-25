# Deploying Dozimetrix (plain install)

A single machine runs everything: MariaDB, the web UI/API, the DICOM receiver and an hourly
query/retrieve job. Docker images will follow; this guide uses systemd only.

## 1. System
- Python ≥ 3.12 with the `sqlite3` module (only needed for tests), MariaDB ≥ 10.6.
- A service account: `useradd --system --home /var/lib/dozimetrix --shell /usr/sbin/nologin dozimetrix`
- Directories: `/opt/dozimetrix` (code + virtualenv), `/etc/dozimetrix` (config, 0750
  root:dozimetrix), `/var/lib/dozimetrix` (secret, spool; 0700 dozimetrix).

## 2. Database
```sql
CREATE DATABASE dozimetrix CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'dozimetrix'@'localhost' IDENTIFIED BY '…';
GRANT ALL ON dozimetrix.* TO 'dozimetrix'@'localhost';
```
Give InnoDB enough memory to hold the working set — at least the size of the data
(`innodb_buffer_pool_size`, e.g. 2G for about 600 000 studies). On spinning disks this
is the difference between 25 ms and 10 s per page of results.

## 3. Install

**First-run wizard.** On an empty database every page leads to `/setup`. The web service prints a one-time setup token to its log and writes it to `<data_dir>/setup-token` (0600); entering it proves access to the server. The wizard checks the installation, can create the schema and load the seed data, creates the first administrator (all roles, signed in at once), and then asks for the site name, a PACS node (with C-ECHO) and the growth tables. An admin can open `/setup` again later. The manual steps below do the same from the command line.

```sh
python3.12 -m venv /opt/dozimetrix/.venv
/opt/dozimetrix/.venv/bin/pip install dozimetrix        # or: pip install -e <checkout>
cp deploy/dozimetrix.toml.example /etc/dozimetrix/dozimetrix.toml   # edit it
sudo -u dozimetrix dozimetrix --config /etc/dozimetrix/dozimetrix.toml migrate
sudo -u dozimetrix dozimetrix --config /etc/dozimetrix/dozimetrix.toml seed
sudo -u dozimetrix dozimetrix --config /etc/dozimetrix/dozimetrix.toml user set admin --roles viewer,exporter,pid,importer,admin
```
The site secret (`secret_file`) is created on first use. Back it up: pseudonyms and
sessions depend on it.

## 4. Services
```sh
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dozimetrix-web dozimetrix-receive dozimetrix-qr.timer
systemctl enable --now dozimetrix-his   # only with a HIS server (see below)
```
- `dozimetrix-web` listens on `[web] host:port` (default 127.0.0.1:8104); publish it through an HTTPS reverse proxy
  (nginx, Apache) and set `secure_cookies = true`.
- `dozimetrix-receive` listens for DICOM on `[dicom] port` (11112). Register
  `AE DOZIMETRIX / host / 11112` on the modalities and the PACS that should send dose
  objects; restrict callers with `allowed_aets`.
- `dozimetrix-qr.timer` queries the PACS every hour for the last two days, one day and one
  modality at a time (many PACS cap the number of studies a query returns; set
  `max_results` and capped days are split into time windows).
- Three ways to get data in:
  - **C-GET** (`retrieve = "get"`, default) – nothing to configure on the PACS;
  - **C-MOVE** (`retrieve = "move"`) – register `DOZIMETRIX` (host, port 11112) on the PACS;
    objects arrive at the receiver; `dozimetrix qr` starts a temporary receiver if the
    service is not running;
  - **push** – configure modalities or the PACS to send dose objects to the receiver.

## 5. Importing history
- OpenREM: `dozimetrix openrem-dump --ssh root@openrem-host` then `dozimetrix openrem-import`
  (see the reconciliation report it prints).
- Back-fill from the PACS: `dozimetrix qr --from 2026-08-01 --to 2026-11-21` (resumable;
  finished days are remembered).
- New devices start as *external*; mark your own rooms local on Admin → Devices.

## Settings (Admin → Settings)
- Editable, kept in the database and applied without a restart (audit-logged as `settings.update`): the site name and code (the first-run wizard's), the **web address** people open Dozimetrix at (notification links use it; it overrides `[notify] base_url`), and how many days the DICOM spool keeps failed and archived objects (overrides `[dicom] retention_days`; 0 keeps everything; the receiver applies it on its hourly prune). An empty value goes back to the toml.
- Shown read-only, set in `dozimetrix.toml` and needing a restart: version, config file, database, DICOM AE title, ports and folders, web listen address, trusted proxies, allowed hosts, session length, upload limit.

## Alerts and notifications
- Default alert rules are seeded by `dozimetrix seed`; edit them in the UI (Admin → Alerts) or through `PATCH /api/alert-rules/{id}`. A seed run never overwrites an edited rule.
- Admins add rules in the same page ("Add rule": threshold, cumulative, count, × national DRL, pregnant patient) and delete the ones they added; a default rule can only be switched off. Both are audit-logged (`alert_rule.create`, `alert_rule.delete`; deleting removes the rule's alerts). Device rules (silent, drift, sustained above DRL) come only from `seeds/alert_rules.toml`. A new rule applies to studies evaluated from now on; for earlier ones run `dozimetrix alerts evaluate --since`.
- New studies are evaluated as they are imported. To evaluate history, run `dozimetrix alerts evaluate --since 2026-01-01`.
- Notifications are sent by the receiver service when it is idle, at most every 30 s. They can also be sent with `dozimetrix alerts notify`. Only alerts on studies from the last 48 h are sent.
- Notification channels (e-mail, ntfy) are set in Admin → Alerts → Notifications: settings, a "Send test" button, and what was sent or failed. They are kept in the database, the SMTP password and the ntfy token sealed with the site secret (write-only in the UI; changing the secret file means typing them again), and they override the `[notify]` table of the toml field by field (see `dozimetrix.toml.example`). The receiver reads them again on every run: no restart. `dozimetrix alerts test-notify` tests the same settings from the shell.
- With no channel configured, alerts on rules with Notify on stay pending for 48 h, so a channel set up later still sends them; older ones are marked "not sent (historical)".

## Growth tables (setup step)
- Run `dozimetrix growth install` once; it downloads the weight-for-age tables into `<data_dir>/growth`:
  - the WHO Child Growth Standards for 0–5 years;
  - the WHO 2007 Growth Reference for 5–10 years;
  - the CDC 2000 growth charts (2–20 years), used only beyond 10 years, where WHO has no weight-for-age reference. Rerun `install` on older setups to add it.
- They are WHO and CDC data under their publishers' terms (see `SOURCE.txt` there), so they are not shipped with Dozimetrix. `dozimetrix growth status` shows whether they are installed.
- With the tables, data quality flags weights outside P1–P99 for age and sex. Studies of children (< 18 y) below P5 or above P95 are left out of DRL statistics (`study.drl_exclusion`), as the national paediatric survey plan asks. Missing or implausible weights (outside P1–P99) are replaced by the median weight for age and sex (`weight_source = estimated_reference`; an implausible reported value is kept in `weight_reported_kg` and still reported by data quality). Estimates are marked on the study page, in exports and in the second-grid report; patients over 20 years, or without age or sex, keep no weight. Run `dozimetrix growth check` after importing weights, e.g. from the RIS (a reported weight replaces an estimate); new studies are estimated and checked as they arrive.

## Weight / height from a CSV file

`dozimetrix sizes FILE.csv` reads weights and heights from any export (RIS, ward records, a spreadsheet) and prints what it would change; `--apply` writes them (audited), then recomputes estimates, DRL exclusions and alerts for the changed studies. Admins can do the same on Admin → Data quality ("Import weight / height").

- Key per row: `study_uid`, or `accession` (also matches the hashed accessions of legacy imports), or `patient_id` + `date` (every study of the patient that day; add `modality` to narrow it).
- Values: `weight_kg`, `height_m` or `height_cm`. `,` or `;` separated; decimal commas are accepted.
- Provenance: `source` (`measured`, `chart_same_day`, `chart_72h`, `imported`) or `measured_at` (a date: same day as the exam → chart_same_day, within 72 h → chart_72h). Neither → `imported`.
- A value replaces the stored one only when its source ranks higher (measured > same day > 72 h > imported/DICOM > HIS chart > estimate), and only when plausible for age and sex.
- A child's weight without a source (`imported`) outside P1–P99 for age and sex is counted as written in the dry run, but after `--apply` the growth check keeps it only as `weight_reported_kg` and uses the median estimate, as for a DICOM weight.

## Dose SR export (IHE REM)

`dozimetrix rdsr --study ID --out DIR` (or `--query 'modality:CT date:30d'`) writes one X-Ray Radiation Dose SR per study: TID 10011 for CT, TID 10001 for radiography, fluoroscopy and mammography, built from the stored values. Files are pseudonymised unless `--identified`: pseudonymous patient ID; no name, birth date, accession or physicians; derived study, series, SOP and event UIDs. They still carry the exact study date and time, age, sex, the study description and the free-text protocol names and comments, so treat them as personal data (the survey export, which cuts dates to the month, is the anonymised product). Estimated weights are not written. Users with the exporter role can download a study's file from the study page ("Dose SR"; `GET /api/studies/{id}/rdsr`). Dozimetrix only writes files: it does not send dose SRs to a PACS or registry.

## DICOM nodes
- Remote AEs are managed in Admin → Integration:
  - **query/retrieve** nodes are the PACS that `dozimetrix qr` pulls from;
  - **sends** nodes are modalities or PACS allowed to send to the receiver.
  The receiver re-reads the allowed list every minute. Changes are audit-logged, and each node has a C-ECHO test.
- `[pacs]` in `dozimetrix.toml` is imported once as a node named "PACS". After that, the database is used. `dozimetrix qr --node NAME` picks a node. `--host/--aec` still overrides for one run.
- The receiver and `qr` write a heartbeat that the page shows (running/stopped, objects per sending AE, sync progress). It also shows a 120-day coverage strip of fully retrieved days.
- Stop the receiver with SIGTERM (systemd does), not Ctrl-C.

## HIS / RIS (patient, weight, height, request, indication)
- Add the server in Admin → Integration → HIS / RIS servers, then press **Test** (or run
  `dozimetrix his check`). Supported: a HippoBridge FHIR gateway. Use a HIS account with read
  access. The password is stored encrypted with a key derived from the site secret, and is
  never shown again; if the site secret changes, enter the password again.
- With an enabled server, every new study whose exam is at most 7 days old is queued.
  Older exams arriving now (a PACS or legacy backfill) are history: they get the CNP
  (sex, birth date, exact age) and growth-chart weight estimates, not HIS lookups. Use
  `dozimetrix his backfill` for them, if ever. The `dozimetrix-his` service
  (`dozimetrix his run`) works through the queue one study at a time, pausing between calls
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
- Try it without writing: `dozimetrix his run --dry-run --limit 20`.
- History: `dozimetrix his backfill --since 2026-01-01 [--until …] [--device ID]` shows how
  many studies would be queued and roughly how long it takes; `--apply` queues them. The
  worker runs history only within the server's backfill hours (e.g. 22-06), children first,
  after the day's new studies.
- The study page shows the matched request (ward, requester, indication, guideline
  decision) and where the weight came from; admins can queue a study for another lookup.
  Data quality flags HIS lookups that found no patient (`his_unmatched`) and disagreements
  between CNP, HIS and DICOM (`demographic_conflict`). The survey export carries the
  request's indication as free text (`indication_text`): review it before sending.
- Use a HIS account of its own for Dozimetrix, not one another service already uses (for
  example the modality worklist's): the HIS may keep a single session per account.
- National IDs alone (offline, no HIS): `dozimetrix cnp backfill` shows what would change;
  add `--apply` to write it.

## Docker

An alternative to the plain install: one image for every role, plus MariaDB.

```sh
cp deploy/docker/dozimetrix.toml deploy/docker/dozimetrix.local.toml   # edit: password, [pacs]
printf 'MARIADB_PASSWORD=...\nMARIADB_ROOT_PASSWORD=...\n' > .env         # same password as the toml
docker compose up -d db web receive
docker compose logs web | grep 'setup token'   # then open /setup (first-run wizard)
docker compose --profile qr up -d qr     # query/retrieve every hour (needs [pacs])
docker compose --profile his up -d his   # HIS worker (needs a HIS server)
```
- `web` migrates and seeds on start, then serves on 127.0.0.1:8104 (put an HTTPS reverse proxy in front). `receive` listens on 11112.
- The volume `dozimetrix-data` holds the site secret, the spool and the growth tables (`docker compose run --rm web growth install`). Back it up together with `dozimetrix-db`.
- The image build has not been tested here (no Docker on the development host); the same install steps were checked in a clean virtual environment.

## Backup and restore

`deploy/backup/` holds one script for both installs (set `MODE=systemd` or `MODE=docker` in `backup.conf`).
**The archives contain patient identifiers in clear: treat them as PHI** (root-only, mode 0600).

- `dozimetrix-backup` dumps MariaDB (`--single-transaction`) and archives the site secret, the configuration
  (`/etc/dozimetrix`, or `.env` and the local toml for Docker) and the spool's `failed/` and `archive/`.
  It keeps the newest `KEEP_DAILY` days plus one per week and per month, and mirrors the folder to
  `DEST` (another LAN host) with `rsync` over SSH. A failure exits non-zero and, if `NTFY_URL` is set, notifies.
- Install (systemd): `cp deploy/backup/backup.conf.example /etc/dozimetrix/backup.conf` (mode 0600), edit it,
  copy `deploy/systemd/dozimetrix-backup*` to `/etc/systemd/system/`, then
  `systemctl enable --now dozimetrix-backup.timer dozimetrix-backup-check.timer`.
  Docker: call the script from the host's cron or a timer the same way.
- The destination: a user on the other host that owns the folder, with a key used for nothing else
  (`restrict,command="rrsync /srv/backups/dozimetrix"` in its `authorized_keys`). Add the host key to
  `known_hosts` first; the script checks it. The mirror deletes what retention removes, so also snapshot that folder if
  you need protection from a compromised production host.
- Encryption is off by default. `ENCRYPT=age` with `AGE_RECIPIENT` encrypts both archives (`.age`); keep the private key
  off the production host, or the backups cannot be restored. The site secret is inside the archive: without encryption it
  is as exposed as the database.
- `dozimetrix-restore --check` verifies the newest backup (checksums, integrity, the study table present), and runs
  weekly from `dozimetrix-backup-check.timer`. It does not load the dump into a database: do a full restore on a spare
  machine at least once a year.
- `dozimetrix-restore 20261125-0230` replaces the database and the site files (it asks you to retype the stamp).
- `dozimetrix migrate` warns when a schema change is pending and the last backup (`<data_dir>/backup-last-ok`) is older than
  26 h; `--require-backup` turns it into a refusal, `--no-backup-check` silences it. Upgrade by hand with
  `dozimetrix-backup && dozimetrix migrate --require-backup`. The Docker entrypoint only warns, so a container never
  stops starting because of it.

## Local DRLs

Admin → Local DRLs proposes local diagnostic reference levels from the hospital's own data (ICRP 135). For each national DRL examination and patient group, the median of each room (device) with at least *n* values is computed; the local DRL is the P75 of those room medians, or the median when only one room qualifies. CT is per series; radiography, fluoroscopy and mammography per study; DRL exclusions are left out. Groups are the national age groups or the ICRP 135 / RP 185 groups (head by age, body by weight). Head CT series reported on the 32 cm phantom are left out (about half the 16 cm value; each row says how many). Weights estimated from the growth charts do not place a patient in a weight group unless asked for; the same default applies to the DRL comparison, the second-grid report and the central statistics. Each value is shown against the national DRL or the RP 185 EDRL.

Chosen values (editable) are adopted as a reference set `local-…`, which can be chosen like any other set on the comparison page, in the DRL summary export and in the semestrial trend (alert rules keep their own thresholds). The yearly review compares an adopted set with a new period: the new value, the change, and the rooms whose median is above the local DRL (XLSX for the file). Adopting and deleting are audited; the bundled sets cannot be deleted.

## Clinical indications

Each study gets a clinical indication code (`ro-nom-indications`) when its request text is known: from the HIS request (the referral-guideline item, else the free text), the DICOM "reason for the requested procedure" (also Reason for Study / Admitting Diagnoses), or a CSV import (`indication`, `indication_code` columns of `dozimetrix sizes`). Clear matches are coded automatically, a child gets the paediatric variant of an item, and weak matches are only suggested. Admin → Indications lists the texts still to code, most frequent first; one decision codes every study with that text, now and later. Single studies can be changed on the study page.

Use the code anywhere a query is accepted: `ind:PED-TCE,TRM-TCE`, or `ind:TRM-*` for a chapter. For example, local DRLs for head trauma CT: Admin → Local DRLs with the selection `origin:local ind:PED-TCE,NEU-TCE,TRM-TCE`. Charts can group by "Clinical indication", and the survey export fills its `indication` column. Only automatic matches, mapped texts and codes set by hand count; unconfirmed suggestions do not.

## Central collection (national survey)

The same installation can act as the survey's central platform. Each site exports one month from Reports → "Studiul național" (anonymised: pseudonymous institution, device and patient codes; month only) and sends the file. At the centre, Reports → "Central collection" (or `dozimetrix survey-import FILE...`) loads the files (CSV or JSON). Each file is checked: one institution and one month, the survey columns, numbers and plausible ranges. Rows that fail are listed and left out. A new file for the same institution and month replaces the older one. The central team can name the institution codes; the names stay at the centre.

National statistics, per exam × group × quantity (RP 185 groups: head by age, body by weight; or the national age groups):
- each institution's median, counted with at least 10 values (fluoroscopy 20);
- the proposed national DRL (the P75 of those medians, at least 2 institutions);
- the national median and the Q3/Q1 spread (flags at 1.5, 2.5, 3);
- the comparison with the RP 185 EDRL and the OMS 838/2025 DRL.

DRL exclusions are left out, and estimated weights too unless asked for. Download as XLSX. Uploading needs the importer or admin role.

## Data only the medical physicist can supply

The nightly checks and the reports work without these, but three Order 1255/2016 items stay empty
until they are entered:

- **Tube output curve (ESAK, art. 5 and annex 2).** For a room without a DAP meter the ESAK is
  calculated from the tube's output: measure the air kerma per mAs at 50 cm for 40 to 140 kVp,
  then `dozimetrix esak-curve set --device ID --file curve.csv` (two columns: `kvp,mgy_per_mas`),
  `esak-curve show` to check, and `dozimetrix recompute` to calculate. The device id is the
  physical device's (not an alias). The result is `event.esak_calc`; a device's own entrance dose
  is never replaced.
- **Effective dose coefficients (nuclear medicine, art. 3).** See the dose statement and
  Art. 12 workbooks: the `Doza efectiva estimata` column is filled only for radiopharmaceuticals
  with a coefficient entered (mSv/MBq); none ship with the program.
- **Breast glandularity (mammography, art. 3).** Not carried by DICOM; entered per study.

## Report specs and plugins

Every report and export is a spec file (`docs/specs.md`). The ones that ship are in
`dozimetrix/seeds/specs/`; a site adds its own by dropping a `.toml` file in the specs folder, with no
restart:

```toml
[specs]
dir = "/etc/dozimetrix/specs"            # default: <config dir>/specs
plugins_dir = "/etc/dozimetrix/plugins"  # default: <config dir>/plugins
```

`dozimetrix specs list` shows what is registered and any file that failed to load. **Plugins** (`.py`
files that register fields, transforms or kinds) run with the server's rights: make `plugins_dir`
writable by the administrator only. The name and SHA-256 of every plugin loaded are in the audit log.
The reporting deadlines on the overview come from the specs' `[deadline]` blocks.
