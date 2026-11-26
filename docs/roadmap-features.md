# Feature roadmap

Features that commercial dose monitors offer and Dozimetrix does not have yet (surveyed
2026-11-26). The products compared were GE DoseWatch, Bayer Radimetrics, Siemens teamplay,
Philips DoseWise, Qaelum DOSE, Sectra DoseTrack and PACSHealth DoseMonitor.

Already covered, so not listed: DRL comparison and local DRLs, cumulative and per-patient
alerts, skin dose maps, SSDE from RDSR, operator reports, national reports, the survey
aggregator.

Status (2026-11-26): items 1–11 (A and B) and 12, 13, 15, 16 (from C) are built, one commit each; 14 and 17–19 are not. What
exists, per item, is in the "Built" notes below. Web pages: Analysis (`analysis.html`: the eight reports, with CSV), and in Admin: Follow-ups,
Incidents, QA register, Report digests, CT coefficients; the study page shows the effective dose,
the conceptus screening, the corrected DAP and the centering; the alerts page has an Investigate
button that opens an incident. The QA correction factor is applied to statistics, DRL comparisons, exports, DAP alerts and
follow-ups (see `qa.py`). Not built: a PDF digest, organ dose. Unchecked: no CT coefficients are loaded, centering stays off.

## A. Quick wins: the data is already stored

1. **Paediatric protocol mismatch alert.** Most protocol names carry an age or weight band
   (`… 1-6 ani`, `CT_CAP_N_1_6_ANI`). Alert when the band doesn't match the patient: an adult
   protocol on a child, or a child protocol on an older child. Qaelum and teamplay flag this.
    *Built: alert kind `protocol_band` (rule `protocol-band-mismatch`, CT).*
2. **CT Dose Check report (NEMA XR-25).** `ingest/parse.py` already reads the RDSR notification
   and alert values (113900). Report which events went past them, how often per protocol, and
   the reason the operator gave. (DoseWatch, Radimetrics.)
    *Built: `reports/dosecheck.py`, `/api/reports/dose-check`; new `event.dose_check_notification`; the details are in `extra_json["dose_check"]`. Existing events need an RDSR refetch to fill them.*
3. **Exposure index / deviation index (IEC 62494).** EI is only a chart metric today. Add a
   target EI per protocol, the DI distribution per room and protocol, and an alert on repeated
   DI > +3 or < −2 (dose creep, detector problems).
    *Built: `reports/exposure.py`, `/api/reports/exposure-index`, device rule `device_di`. The target EI is the one the detector reports per exposure (shown per protocol, flagged when it varies); a site-set target table was not built.*
4. **Collimation audit for radiography.** Where a room reports both DAP and air kerma, DAP ÷ kerma
   gives the exposed field area. Compare it with the anatomy and the detector size. This
   separates over-collimation from a DAP calibration error, e.g. a room whose DAP is several
   times its peers' at similar kV and mAs.
    *Built: `reports/collimation.py`, `/api/reports/collimation`. The detector size is not stored, so the field area is compared with the department's median for the same exam and age group, not with the detector.*
5. **CT scan length and overranging.** Compare the scanned length with the exam region (a head
   scan running into the neck, an abdomen scan extended into the chest). Show scan length
   outliers per protocol. (DoseWatch.)
    *Built: `reports/scanlength.py`, `/api/reports/scan-length`. Length outliers only: real overranging (extra rotations) is not in the dose report.*
6. **Protocol drift detection.** Alert when a protocol's typical kV, reference mAs or CTDIvol
   changes step-wise from one week to the next: usually an edited protocol or a software update.
   Builds on `protocols.py`. (teamplay protocol management.)
    *Built: device rule `protocol_drift` (rule `protocol-drift-25pct`).*

## B. Workflow: mostly new tables and pages, little new data

7. **High-dose follow-up for fluoroscopy and interventional work.** After a PSD alert: tell the
   patient, flag the record, check the skin after 2–4 weeks. Typical triggers are the SIR/NCRP
   levels (PSD 3 Gy, reference-point kerma 5 Gy, KAP 500 Gy·cm²). Track it with a status and a
   printable patient letter.
    *Built: table `followup`, `followup.py`, `/api/followups` (+ letter, Romanian or English).*
8. **Investigation and incident records.** An alert becomes a case: cause, actions, outcome, and
   whether it is reportable as an unintended or accidental exposure (BSS Art. 63, to the national
   regulator). Gives the medical physicist an audit trail. (DoseWatch, Qaelum, Radimetrics.)
    *Built: tables `incident`, `incident_log`, `incidents.py`, `/api/incidents`.*
9. **Before/after comparison of an optimisation.** Pick the date of a protocol change and compare
   doses before and after, by band, with a significance test and an image-quality note. Fits the
   annual local DRL review. (teamplay.)
    *Built: `reports/optimisation.py`, `/api/reports/optimisation` (Mann-Whitney U; the image-quality note is free text carried in the report).*
10. **Scheduled report emails.** For example a monthly PDF per department head: top outliers,
    values above DRL, open alerts, device status. Uses the existing SMTP and ntfy notifiers.
    *Built: `digests.py`, `/api/digests`, CLI `digests`. HTML + text mail, no PDF (no dependency).*
11. **QA and calibration register per device.** Record the DAP meter correction factor with its
    date and apply it at read time; reminders when a QA test or calibration is due. (Qaelum QA
    module.) Related to item 4.
    *Built: table `device_qa`, `qa.py`, `/api/devices/{id}/qa`, alert `qa_due`. The DAP correction factor is applied on the study page, the dose statement, the collimation audit, statistics, DRL comparisons, exports, DAP alerts and follow-ups.*

## C. Bigger items: new data or physics

12. **CT effective dose and organ dose.** Simple version: DLP × k-factor by region and age
    (ICRP 103, paediatric k-factors). Commercial version: Monte Carlo organ dose per patient size
    (Radimetrics, DoseWatch). k-factors are cheap; Monte Carlo is not.
    *Built: `ctdose.py`, `ct_kfactor`, CLI `ct-kfactor`, study page, `/api/reports/effective-dose`. No coefficients ship: load a published set (`docs/examples/ct-kfactors-brady2012.csv` is a candidate to verify). A series whose phantom has no coefficient gets no estimate. Organ dose (Monte Carlo) not built.*
13. **Conceptus dose estimate.** For CT or fluoroscopy of a pregnant patient. Builds on the
    existing `pregnant` alert kind.
    *Built: `conceptus.py`; the `pregnant` alert and the study page carry a low/central/high screening range for CT with the uterus in the beam (normalised fetal dose x series CTDIvol); fluoroscopy gets no number (the physicist records an estimate in an incident). Factors from a search summary: verify. Synthetic tests only.*
14. **SSDE from images.** Water-equivalent diameter from the localizer or axial images, so SSDE
    covers every scanner, not only RDSR Dw cases (Radimetrics). Needs image retrieval (C-GET of
    the localizer), which is heavy on the PACS.
15. **Patient centering in CT.** Isocentre offset from the localizer; off-centre patients get
    wrong tube current modulation. Needs images too.
    *Built, **unverified**: `centering.py`, `ct_centering`, `/api/reports/centering`, CLI `centering FILE`. Off by default (`[ingest] measure_centering`); tested only on a synthetic phantom; no PACS retrieval added. Check it on a phantom placed off the isocentre before turning it on.*
16. **Reject and repeat analysis.** True reject data needs DR consoles that export rejects (IHE
    "Rejected for Quality Reasons" KOS) or console logs. A proxy is repeated views of the same
    projection within one study.
    *Built as a proxy: `reports/repeats.py`, `/api/reports/repeats`. Possible repeats (same exam, projection, laterality, protocol within 10 minutes), compared between rooms; no rejected-image notes are received, so true rejects are not available.*
17. **MR SAR monitoring.** MR headers carry SAR and dB/dt; relevant for children and implants
    (DoseWatch, Qaelum). New scope: not ionising radiation.
18. **Contrast media tracking.** Injector data (Bayer, Bracco). Probably out of scope.
19. **Lifetime cancer risk estimates (BEIR VII).** Offered by Radimetrics. Recommended to leave
    out: contentious for individual patients and easily misread.

## Suggested order

1–6 first: each is a small slice on stored data, and 4 and 6 help with problems already found.
Then 7 and 8, which turn alerts into a documented process an inspection will ask about.
