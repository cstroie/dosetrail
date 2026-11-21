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
- `dosetrail-web` listens on 127.0.0.1:8000; publish it through an HTTPS reverse proxy
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
