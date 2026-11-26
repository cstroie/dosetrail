# Feature roadmap

Features that commercial dose monitors offer and Dozimetrix does not have yet (surveyed
2026-11-26). The products compared were GE DoseWatch, Bayer Radimetrics, Siemens teamplay,
Philips DoseWise, Qaelum DOSE, Sectra DoseTrack and PACSHealth DoseMonitor.

Already covered, so not listed: DRL comparison and local DRLs, cumulative and per-patient
alerts, skin dose maps, SSDE from RDSR, operator reports, national reports, the survey
aggregator.

Status: proposed. None of these is scheduled yet.

## A. Quick wins: the data is already stored

1. **Paediatric protocol mismatch alert.** Most protocol names carry an age or weight band
   (`… 1-6 ani`, `CT_CAP_N_1_6_ANI`). Alert when the band doesn't match the patient: an adult
   protocol on a child, or a child protocol on an older child. Qaelum and teamplay flag this.
2. **CT Dose Check report (NEMA XR-25).** `ingest/parse.py` already reads the RDSR notification
   and alert values (113900). Report which events went past them, how often per protocol, and
   the reason the operator gave. (DoseWatch, Radimetrics.)
3. **Exposure index / deviation index (IEC 62494).** EI is only a chart metric today. Add a
   target EI per protocol, the DI distribution per room and protocol, and an alert on repeated
   DI > +3 or < −2 (dose creep, detector problems).
4. **Collimation audit for radiography.** Where a room reports both DAP and air kerma, DAP ÷ kerma
   gives the exposed field area. Compare it with the anatomy and the detector size. This
   separates over-collimation from a DAP calibration error, e.g. a room whose DAP is several
   times its peers' at similar kV and mAs.
5. **CT scan length and overranging.** Compare the scanned length with the exam region (a head
   scan running into the neck, an abdomen scan extended into the chest). Show scan length
   outliers per protocol. (DoseWatch.)
6. **Protocol drift detection.** Alert when a protocol's typical kV, reference mAs or CTDIvol
   changes step-wise from one week to the next: usually an edited protocol or a software update.
   Builds on `protocols.py`. (teamplay protocol management.)

## B. Workflow: mostly new tables and pages, little new data

7. **High-dose follow-up for fluoroscopy and interventional work.** After a PSD alert: tell the
   patient, flag the record, check the skin after 2–4 weeks. Typical triggers are the SIR/NCRP
   levels (PSD 3 Gy, reference-point kerma 5 Gy, KAP 500 Gy·cm²). Track it with a status and a
   printable patient letter.
8. **Investigation and incident records.** An alert becomes a case: cause, actions, outcome, and
   whether it is reportable as an unintended or accidental exposure (BSS Art. 63, to the national
   regulator). Gives the medical physicist an audit trail. (DoseWatch, Qaelum, Radimetrics.)
9. **Before/after comparison of an optimisation.** Pick the date of a protocol change and compare
   doses before and after, by band, with a significance test and an image-quality note. Fits the
   annual local DRL review. (teamplay.)
10. **Scheduled report emails.** For example a monthly PDF per department head: top outliers,
    values above DRL, open alerts, device status. Uses the existing SMTP and ntfy notifiers.
11. **QA and calibration register per device.** Record the DAP meter correction factor with its
    date and apply it at read time; reminders when a QA test or calibration is due. (Qaelum QA
    module.) Related to item 4.

## C. Bigger items: new data or physics

12. **CT effective dose and organ dose.** Simple version: DLP × k-factor by region and age
    (ICRP 103, paediatric k-factors). Commercial version: Monte Carlo organ dose per patient size
    (Radimetrics, DoseWatch). k-factors are cheap; Monte Carlo is not.
13. **Conceptus dose estimate.** For CT or fluoroscopy of a pregnant patient. Builds on the
    existing `pregnant` alert kind.
14. **SSDE from images.** Water-equivalent diameter from the localizer or axial images, so SSDE
    covers every scanner, not only RDSR Dw cases (Radimetrics). Needs image retrieval (C-GET of
    the localizer), which is heavy on the PACS.
15. **Patient centering in CT.** Isocentre offset from the localizer; off-centre patients get
    wrong tube current modulation. Needs images too.
16. **Reject and repeat analysis.** True reject data needs DR consoles that export rejects (IHE
    "Rejected for Quality Reasons" KOS) or console logs. A proxy is repeated views of the same
    projection within one study.
17. **MR SAR monitoring.** MR headers carry SAR and dB/dt; relevant for children and implants
    (DoseWatch, Qaelum). New scope: not ionising radiation.
18. **Contrast media tracking.** Injector data (Bayer, Bracco). Probably out of scope.
19. **Lifetime cancer risk estimates (BEIR VII).** Offered by Radimetrics. Recommended to leave
    out: contentious for individual patients and easily misread.

## Suggested order

1–6 first: each is a small slice on stored data, and 4 and 6 help with problems already found.
Then 7 and 8, which turn alerts into a documented process an inspection will ask about.
