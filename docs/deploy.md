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

## WHO growth tables (setup step)
- Run `dosetrail growth install` once; it downloads the WHO weight-for-age tables into `<data_dir>/growth`:
  - the Child Growth Standards for 0–5 years;
  - the 2007 Growth Reference for 5–10 years.
- They are WHO data under WHO's terms (see `SOURCE.txt` there), so they are not shipped with DoseTrail. `dosetrail growth status` shows whether they are installed.
- With the tables, data quality flags weights outside P1–P99 for age and sex. Studies below P5 or above P95 are left out of DRL statistics (`study.drl_exclusion`), as the national paediatric survey plan asks. Run `dosetrail growth check` after importing weights, e.g. from the RIS; new studies are checked as they arrive.

## DICOM nodes
- Remote AEs are managed in Admin → DICOM nodes:
  - **query/retrieve** nodes are the PACS that `dosetrail qr` pulls from;
  - **sends** nodes are modalities or PACS allowed to send to the receiver.
  The receiver re-reads the allowed list every minute. Changes are audit-logged, and each node has a C-ECHO test.
- `[pacs]` in `dosetrail.toml` is imported once as a node named "PACS". After that, the database is used. `dosetrail qr --node NAME` picks a node. `--host/--aec` still overrides for one run.
- The receiver and `qr` write a heartbeat that the page shows (running/stopped, objects per sending AE, sync progress). It also shows a 120-day coverage strip of fully retrieved days.
- Stop the receiver with SIGTERM (systemd does), not Ctrl-C.
