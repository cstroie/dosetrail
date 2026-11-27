# Dozimetrix Hardware Requirements

This document specifies the hardware and infrastructure requirements for running Dozimetrix in production, based on real-world data volumes, traffic patterns, and performance characteristics observed at ~600,000 studies.

---

## 1. Executive Summary

| Deployment Size | Studies | Devices | Daily Ingest | Recommended Spec |
|-----------------|---------|---------|--------------|------------------|
| **Small** (single dept) | < 50,000 | 2–10 | 50–200 | 4 vCPU, 16 GB RAM, 200 GB SSD |
| **Medium** (hospital) | 50,000–300,000 | 10–50 | 200–1,000 | 8 vCPU, 32 GB RAM, 500 GB SSD |
| **Large** (multi-site / region) | 300,000–1,000,000+ | 50–200 | 1,000–5,000 | 16 vCPU, 64 GB RAM, 1 TB NVMe |
| **Reference (current)** | **~600,000** | ~30 | ~500 | 8 vCPU, 32 GB RAM, 500 GB SSD |

> **Note:** The current development instance runs at ~600k studies with 30+ devices, ~500 studies/day ingest. The specs below are based on this real workload.

---

## 2. Database Server (MariaDB)

### 2.1 Minimum Requirements

| Component | Small | Medium | Large |
|-----------|-------|--------|-------|
| **CPU** | 4 vCPU | 8 vCPU | 16 vCPU |
| **RAM** | 16 GB | 32 GB | 64 GB |
| **Storage** | 200 GB SSD | 500 GB NVMe | 1 TB NVMe |
| **innodb_buffer_pool_size** | 4 GB | 16 GB | 48 GB |

### 2.2 Storage Sizing

| Data Type | Size per 100k Studies | 600k Studies (est.) |
|-----------|----------------------|---------------------|
| Study table (core) | ~150 MB | ~900 MB |
| Event table (all modalities) | ~2–4 GB | ~12–24 GB |
| Classification / codes / bands | ~100 MB | ~600 MB |
| Indexes (incl. full-text) | ~1–2 GB | ~6–12 GB |
| **Total database** | **3.5–6.5 GB** | **20–40 GB** |
| **With 3× headroom** | **12–20 GB** | **60–120 GB** |
| **Backup (compressed)** | **1–2 GB** | **6–12 GB** |

> **Real data point:** The OpenREM import of ~600k studies produced a ~25 GB MariaDB data directory. With Dozimetrix's leaner schema and no pixel data, expect **20–40 GB** for the live database.

### 2.3 MariaDB Configuration (Critical)

```ini
# /etc/mysql/mariadb.conf.d/99-dozimetrix.cnf
[mysqld]
# Buffer pool: 70–80% of RAM for dedicated DB server
innodb_buffer_pool_size = 16G        # Medium: 16 GB of 32 GB RAM
innodb_buffer_pool_instances = 16

# Log files: large enough for write-heavy ingest
innodb_log_file_size = 2G
innodb_log_files_in_group = 4
innodb_log_buffer_size = 64M

# Flushing: avoid stalls during bulk ingest
innodb_io_capacity = 2000
innodb_io_capacity_max = 4000
innodb_flush_method = O_DIRECT
innodb_flush_neighbors = 0

# Concurrency
innodb_thread_concurrency = 0        # 0 = auto
innodb_read_io_threads = 8
innodb_write_io_threads = 8

# Timeouts (long queries for stats/DRL)
innodb_lock_wait_timeout = 120
lock_wait_timeout = 120

# Character set
character_set_server = utf8mb4
collation_server = utf8mb4_unicode_ci

# Binary logging (for replication / point-in-time recovery)
log_bin = mariadb-bin
binlog_format = ROW
expire_logs_days = 7
max_binlog_size = 500M

# Slow query log (essential for stats optimization)
slow_query_log = 1
slow_query_log_file = /var/log/mysql/slow.log
long_query_time = 2
log_queries_not_using_indexes = 0
```

### 2.4 Why These Settings Matter

- **Buffer pool**: At 600k studies, the working set (active indexes + recent studies) is ~10–15 GB. A 16 GB pool keeps hot data in memory; on spinning disks, a too-small pool degrades chart queries from **25 ms → 10 s**.
- **Log file size**: Ingest bursts (QR backfill, OpenREM import) write millions of rows. Large logs reduce checkpoint stalls.
- **IO capacity**: NVMe can sustain 100k+ IOPS; tune to your storage class.

---

## 3. Application Server (Web + Receive + QR + HIS)

All four roles run from the same Python process (single binary, different entrypoints). They share the same code, config, and database connection pool.

### 3.1 Minimum Requirements

| Component | Small | Medium | Large |
|-----------|-------|--------|-------|
| **CPU** | 4 vCPU | 8 vCPU | 16 vCPU |
| **RAM** | 8 GB | 16 GB | 32 GB |
| **Storage** | 50 GB SSD | 100 GB SSD | 200 GB NVMe |

### 3.2 Runtime Profile

| Process | Threads | Memory (typical) | Notes |
|---------|---------|------------------|-------|
| **Web (uvicorn workers)** | 4–8 workers × 1 thread | 200–400 MB each | CPU-bound on stats; scale workers to vCPU count |
| **Receive (DICOM SCP)** | 1 main + worker pool | 150–300 MB | I/O-bound; handles C-STORE, spools to disk |
| **QR (query/retrieve)** | 1 (scheduled hourly) | 200–500 MB | Bursty; pulls from PACS, spikes CPU on classification |
| **HIS worker** | 1 (daemon) | 100–200 MB | Low CPU; rate-limited by HIS API |
| **Maintenance (nightly)** | 1 (cron) | 500 MB – 2 GB | Reclassify at 600k studies takes ~10 s; memory peaks at band lookup |

### 3.3 Web Worker Sizing

```bash
# In systemd unit or docker-compose:
# workers = (2 × vCPU) + 1  for CPU-bound stats endpoints
# But: result_cache_s=120 caches heavy queries, so 4–8 workers suffice
WEB_WORKERS=6
```

**Memory per worker**: ~250 MB baseline + query result sets. With 6 workers: **~1.5 GB**.

### 3.4 Result Cache Memory

The in-memory result cache (default 120 s TTL) holds:
- Up to **48 cached answers**
- Each answer ≤ **400,000 rows** (aggregated)
- Typical memory: **200–800 MB** at 600k studies

Disable with `result_cache_s = 0` if memory is tight.

---

## 4. DICOM Spool Storage

The spool holds raw DICOM objects before/after ingest.

### 4.1 Sizing

| Object Type | Typical Size | Daily Volume (500 studies) | Retention (default) |
|-------------|--------------|----------------------------|---------------------|
| RDSR (CT) | 100–500 KB | 50–100 MB | 0 days (deleted after ingest) |
| RDSR (RF/XA) | 200 KB – 2 MB | 100–500 MB | 0 days |
| DX/CR Header | 50–200 KB | 10–50 MB | 0 days |
| MG Header | 100–300 KB | 5–20 MB | 0 days |
| **Failed/Archive** | — | — | **configurable** (default 0 = keep) |

**With `keep_received = true` and 30-day retention**: **5–15 GB/month**.

### 4.2 Recommended Spool Layout

```
/var/lib/dozimetrix/spool/
├── incoming/      # Active receive (fast SSD)
├── archive/       # Successful ingest (can be slower storage)
└── failed/        # Failed parse (investigate, then retry/delete)
```

- Mount `incoming` on **NVMe/SSD** (low latency for C-STORE ACK)
- `archive` and `failed` can be on **HDD/bulk storage** if retention > 0

---

## 5. Growth Tables (WHO / CDC)

Downloaded at setup (`dozimetrix growth install`):

| Table | Size |
|-------|------|
| WHO 0–5 years | ~2 MB |
| WHO 5–10 years | ~1 MB |
| CDC 2–20 years | ~3 MB |
| **Total** | **~6 MB** |

Stored in `<data_dir>/growth/`. Negligible for sizing.

---

## 6. Backups

### 6.1 Backup Size (Compressed)

| Data | 600k Studies |
|------|--------------|
| MariaDB dump (--single-transaction, gzipped) | 6–12 GB |
| Site secret + config + spool/failed + spool/archive | < 1 GB |
| **Total per backup** | **7–13 GB** |

### 6.2 Retention Policy (Default)

| Tier | Count | Example |
|------|-------|---------|
| Daily | 14 | Last 2 weeks |
| Weekly | 8 | Last 8 weeks (Mondays) |
| Monthly | 6 | Last 6 months (1st of month) |
| **Total stored** | **28 archives** | **~200–350 GB** |

### 6.3 Backup Destination

- **Local**: Separate disk/volume (survives primary disk failure)
- **Remote**: rsync over SSH to another host (`DEST` in backup.conf)
- **Encryption**: `ENCRYPT=age` with `AGE_RECIPIENT` (recommended for PHI)

**Network**: 1 Gbps LAN → 10 GB backup transfers in ~2 minutes.

---

## 7. Network Requirements

### 7.1 Inbound

| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 443 | HTTPS | Users / Reverse Proxy | Web UI, API |
| 11112 | DICOM (TCP) | Modalities, PACS | C-STORE (dose objects) |
| 104 | DICOM (TCP) | **PACS → Dozimetrix** | C-MOVE destination (if `retrieve = move`) |

### 7.2 Outbound

| Port | Protocol | Destination | Purpose |
|------|----------|-------------|---------|
| 3306 | MySQL | Database server | DB connection |
| 104 | DICOM | PACS | C-FIND / C-GET (QR) |
| 443 | HTTPS | HIS / FHIR server | Patient / request lookup |
| 25/587 | SMTP | Mail server | Alert notifications |
| 443 | HTTPS | ntfy.sh / self-hosted | Push notifications |
| 22 | SSH | Backup destination | rsync mirror |

### 7.3 Reverse Proxy (Required for Production)

- **nginx** or **Apache** terminating TLS
- Proxy to `127.0.0.1:8104` (web) and pass `X-Forwarded-For`, `X-Forwarded-Proto`
- Configure `secure_cookies = true` and `web_trusted_proxies` in `dozimetrix.toml`
- **WebSocket**: Not used (no live updates; polling only)

---

## 8. CPU & Memory: Detailed Breakdown by Workload

### 8.1 Ingest (Receive → Classify → Upsert)

| Step | CPU | Memory | I/O | Parallelism |
|------|-----|--------|-----|-------------|
| C-STORE receive | Low | 50 MB | Write spool (sequential) | 10–50 concurrent associations |
| Classify (SOP class) | Negligible | — | Read header only | — |
| Adapter (parse RDSR) | Medium | 100–200 MB | — | 1 per study (batched) |
| Upsert (DB) | Medium | — | **Write-heavy** (indexes) | Batched per study (1 tx) |

**Throughput target**: **> 50 studies/s on 1 core** (DX/CR headers).
At 500 studies/day = 6 studies/min = trivial load.

**Burst (QR backfill)**: 1,000–5,000 studies/hour → needs 2–4 cores for classification + DB writes.

### 8.2 Query/Retrieve (Hourly)

- C-FIND per modality × date window
- C-GET/C-MOVE for matched series
- Classification + upsert for each new study
- **Peak**: 1–2 cores for 10–30 min/hour

### 8.3 Web API (User-Facing)

| Endpoint | Typical Latency (600k) | Cacheable? |
|----------|------------------------|------------|
| Study list (paged) | 50–150 ms | No (user-specific filters) |
| Study detail | 30–80 ms | No |
| Chart data (`/api/stats/...`) | 400–3,500 ms | **Yes (120 s default)** |
| DRL comparison | 500–2,000 ms | **Yes** |
| Local DRL propose | 1–5 s | **Yes** |
| Exports (CSV/XLSX) | 2–30 s | Async job |

**Concurrent users**: 5–20 typical. With result cache, 6 workers handle 20+ users.

### 8.4 Nightly Maintenance (`dozimetrix maintain`)

| Task | Duration (600k) | Memory |
|------|-----------------|--------|
| Reclassify (90 days) | 2–10 s | 500 MB |
| Reclassify (full, rules changed) | 30–120 s | 1–2 GB |
| Bands refill | 5–15 s | 300 MB |
| Indications refresh | 2–5 s | 200 MB |
| Device counters | 1–3 s | 100 MB |
| Review counts | 10 s | 200 MB |
| **Total** | **20–160 s** | **Peak ~2 GB** |

Runs at 03:30 via systemd timer. Single-threaded.

---

## 9. Recommended Production Topology

### 9.1 Single-Server (Small–Medium)

```
┌─────────────────────────────────────┐
│         Application Server          │
│  ┌───────────────────────────────┐  │
│  │  Web (6 workers)              │  │
│  │  Receive (DICOM SCP)          │  │
│  │  QR (hourly cron)             │  │
│  │  HIS (daemon)                 │  │
│  │  Maintain (nightly cron)      │  │
│  └───────────────────────────────┘  │
│  RAM: 32 GB  |  CPU: 8 vCPU         │
│  SSD: 500 GB (OS + app + spool)     │
└─────────────────────────────────────┘
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
    ┌───────────┐       ┌───────────┐
    │  MariaDB  │       │  Backup   │
    │  (local)  │       │  (remote) │
    │  32 GB    │       │  rsync/SSH│
    │  500 GB   │       │           │
    └───────────┘       └───────────┘
```

**Total**: 1 app server + 1 DB server (can be same box for <100k studies).

### 9.2 Split Architecture (Large / HA)

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Load       │────▶│  App Server │────▶│  MariaDB    │
│  Balancer   │     │  (×2–3)     │     │  Primary    │
│  (nginx)    │     │  Web+Recv   │     │  (32–64 GB) │
└─────────────┘     └─────────────┘     └─────────────┘
                           │                    │
                    ┌──────┴──────┐       ┌─────┴─────┐
                    │  QR/HIS/    │       │  Replica  │
                    │  Maintain   │       │  (read)   │
                    │  (×1 each)  │       └───────────┘
                    └─────────────┘
```

- **Web/Receive**: Stateless, scale horizontally behind LB
- **QR/HIS/Maintain**: Singleton (leader election or dedicated node)
- **DB**: Primary + read replica for chart queries (optional; result cache reduces need)

---

## 10. Cloud / VM Sizing Reference

### 10.1 AWS (Graviton3 / Intel)

| Tier | Instance | vCPU | RAM | Storage | Est. Monthly (us-east-1, Linux) |
|------|----------|------|-----|---------|----------------------------------|
| Small | t4g.medium | 2 | 4 GB | gp3 200 GB | ~$60 |
| Medium | t4g.xlarge | 4 | 16 GB | gp3 500 GB | ~$150 |
| **Recommended** | **r6g.2xlarge** | **8** | **64 GB** | **gp3 500 GB** | **~$350** |
| Large | r6g.4xlarge | 16 | 128 GB | gp3 1 TB | ~$700 |

> Use **R6g (memory-optimized)** for MariaDB; **T4g (burstable)** works for app server at small scale.

### 10.2 Azure / GCP Equivalents

| Cloud | Medium DB | Medium App |
|-------|-----------|------------|
| Azure | D8s v5 (8 vCPU, 32 GB) | D4s v5 (4 vCPU, 16 GB) |
| GCP | n2-standard-8 (8 vCPU, 32 GB) | n2-standard-4 (4 vCPU, 16 GB) |

---

## 11. Monitoring & Alerting Thresholds

| Metric | Warning | Critical | Action |
|--------|---------|----------|--------|
| **DB: innodb_buffer_pool_pages_free** | < 10% | < 5% | Increase buffer pool or add RAM |
| **DB: slow queries (>2 s)** | > 10/min | > 50/min | Check missing indexes; analyze stats queries |
| **App: web worker CPU** | > 70% sustained | > 90% | Add workers or scale out |
| **App: receive queue depth** | > 100 | > 500 | Backlog; check DB write latency |
| **Disk: spool free space** | < 10 GB | < 2 GB | Purge archive/failed; increase retention |
| **Disk: DB free space** | < 20% | < 10% | Archive old studies; expand volume |
| **Backup: last success** | > 26 h | > 48 h | Check backup job logs |
| **QR: last successful sync** | > 3 h | > 6 h | Check PACS connectivity |
| **HIS: queue depth** | > 50 | > 200 | Check HIS API latency / credentials |

---

## 12. Capacity Planning Formulas

### 12.1 Database Growth

```
Study row:           ~2 KB (core) + 1 KB indexes = 3 KB
Event row (avg):     ~1.5 KB + 0.5 KB indexes = 2 KB
Events per study:    CT=15, DX=2, RF=30, MG=4 → weighted avg ~8
Daily studies:       N
Daily growth:        N × (3 KB + 8 × 2 KB) = N × 19 KB
Annual growth:       N × 19 KB × 365 = N × 6.9 MB
```

**Example**: 500 studies/day → **3.5 GB/year** raw data. With indexes, binlog, overhead: **~10 GB/year**.

### 12.2 RAM for Buffer Pool

```
Working set ≈ (Active studies last 90 days) × 3 KB
           + (Active indexes: study_uid, device+date, modality+date, patient)
           ≈ 0.15 × Total studies × 3 KB + 2–4 GB indexes
           
At 600k studies:  ~2.7 GB + 3 GB = ~6 GB working set
→ Buffer pool 16 GB gives 2.5× headroom
```

### 12.3 Spool Storage

```
Daily ingest:  N studies × (0.2 MB avg RDSR + 0.05 MB headers)
With 30-day retention (keep_received=true):  N × 0.25 MB × 30 = N × 7.5 MB/day
At 500/day:  ~3.75 GB/month
```

---

## 13. Security Hardening (Infrastructure)

| Layer | Requirement |
|-------|-------------|
| **OS** | Minimal install (Ubuntu 24.04 LTS / Debian 12 / RHEL 9); auto security updates |
| **Firewall** | Default deny; allow only: 22 (mgmt), 443 (HTTPS), 11112 (DICOM from known AEs), 3306 (app→DB only) |
| **TLS** | TLS 1.2+ only; cert from Let's Encrypt or internal CA; HSTS; `secure_cookies = true` |
| **Database** | Dedicated `dozimetrix` user; no remote root; `require_secure_transport = ON` |
| **Secrets** | Site secret (`/var/lib/dozimetrix/secret`) mode 0600, owned by `dozimetrix` user; back up with DB |
| **Backups** | Encrypted at rest (`ENCRYPT=age`); private key off-site |
| **Audit** | `audit_log` table captures all identifier access, exports, admin changes |

---

## 14. Checklist for New Deployment

- [ ] MariaDB: `innodb_buffer_pool_size` ≥ 70% RAM, log files ≥ 2 GB
- [ ] App server: 8 vCPU / 32 GB RAM / 500 GB NVMe (medium)
- [ ] Spool: NVMe for `incoming/`; HDD OK for `archive/` if retention > 0
- [ ] Reverse proxy: nginx + TLS, `proxy_read_timeout 60s` (for slow exports)
- [ ] Backup: daily + weekly + monthly; remote mirror; encryption enabled
- [ ] Monitoring: Prometheus node_exporter + mysqld_exporter + custom `/health` endpoint
- [ ] Log aggregation: journald → Loki / Elastic / syslog (rotate daily, keep 30 days)
- [ ] DICOM: Register AE `DOZIMETRIX` on all modalities + PACS; test C-ECHO
- [ ] QR: Configure PACS node; test `dozimetrix qr --days 1 --dry-run`
- [ ] HIS: Add server in Admin → Integration; test with `dozimetrix his check`
- [ ] Growth tables: `dozimetrix growth install` (downloads WHO/CDC)
- [ ] Seed data: `dozimetrix seed` (DRLs, group schemes, alert rules, code systems)
- [ ] First admin: `dozimetrix user set admin --roles viewer,exporter,pid,importer,admin`

---

## 15. Scaling Triggers

| Trigger | Action |
|---------|--------|
| Studies > 1M | Split DB read replica for stats; consider partitioning `study` by year |
| Web latency (p95) > 500 ms | Add app server behind LB; increase workers |
| QR backfill > 2 h | Parallelize per-modality; increase `pacs_max_results` windows |
| Ingest queue > 1,000 | Add receive worker (currently single-threaded per association) |
| Backup > 30 min | Parallel `mysqldump` (per-table); exclude spool from daily |
| Alert evaluation > 60 s | Batch evaluate; add index on `(modality, study_datetime)` |

---

## 16. Appendix: Current Development Instance Specs

| Component | Spec |
|-----------|------|
| **OS** | Ubuntu 24.04 (kernel 5.15) |
| **CPU** | 12 vCPU (AMD EPYC) |
| **RAM** | 64 GB |
| **DB Disk** | 500 GB NVMe (MariaDB 11.4) |
| **App Disk** | 500 GB NVMe (spool + app) |
| **Studies** | ~600,000 |
| **Devices** | 32 (18 local) |
| **Daily ingest** | ~500 (QR + push) |
| **Modalities** | CT, DX, CR, RF, XA, MG |
| **MariaDB buffer pool** | 16 GB |
| **Web workers** | 6 |
| **Result cache** | 120 s (48 entries max) |
| **Chart query p95** | 1.2 s (cached: 5 ms) |
| **Nightly maintain** | 45 s (full reclassify: 90 s) |

---

*Document version: 2026-11-27 — based on Dozimetrix codebase at commit `nemo` branch.*