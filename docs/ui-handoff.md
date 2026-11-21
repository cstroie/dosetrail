# UI hand-over: findings from running the Console UI on real data

The UI now runs against the live API (`dosetrail serve`, then log in). The pages receive the same shapes as `data/sample/`, plus these fields:
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
9. **Write actions.** Display-name edits, merges, alert acknowledgements and the patient-identifier reveal need endpoints. The API has `POST /api/studies/{id}/identify` (pid role, audit-logged). The device write endpoints come next on the API side.
