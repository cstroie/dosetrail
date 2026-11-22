# Dosimetrix — frontend design brief

*Paste everything below the line into Claude Design.*

---

## Your task

Design the web user interface for **Dosimetrix**, an open-source patient radiation-dose monitoring application used inside hospitals.

**Propose 3 distinctly different visual and interaction directions.** For each direction, deliver working static mockups of the 5 key screens listed below, using realistic sample data. We will pick one direction, possibly combining ideas from the others, and build it.

## Product context

Dosimetrix collects radiation-dose data from imaging equipment (CT scanners, digital radiography, fluoroscopy and angiography units, mammography). This data comes from DICOM dose reports and image headers. Dosimetrix stores it, and lets staff do four things:

- check individual exams and their irradiation events;
- compute dose statistics per protocol, procedure and device (median, boxplot, histogram, trends over time). These are used to set and audit **diagnostic reference levels (DRLs)** and to optimise protocols;
- spot high or cumulative doses, mainly in fluoroscopy, and receive alerts;
- export data for audits and regulatory surveys.

It replaces an older tool whose interface is a dated Bootstrap 3 layout. That interface has a 25-field filter form in a sidebar, long stacked chart accordions and raw tables. We want something calm, fast, precise and information-dense, without looking cluttered.

### Users
- **Medical physicists and radiation protection experts:** heavy users of charts, statistics and exports. Technically literate.
- **Radiographers and modality leads:** look up a specific exam or check a device or protocol.
- **Radiologists and department heads:** quick overview and high-dose alerts.
- **Administrators:** device names, DICOM nodes, users, mappings.

### Environment
- Hospital intranet. **No internet access at runtime**: no CDNs, no web fonts from Google, no external assets. Use system font stacks, or say which open-licence font files would be self-hosted.
- Desktop first: 1366×768 up to 1920×1080 and ultrawide. Must stay usable on a tablet. A phone only needs the alert and exam-lookup views.
- Some users work in dim reading rooms, so a **dark theme is required**, in addition to light.
- Languages: English and **Romanian**. Allow for longer Romanian labels and diacritics (ă â î ș ț).

## Hard technical constraints (please respect them in your mockups)

- **Only HTML, CSS and vanilla JavaScript (ES modules).** No npm, no build step, no frameworks: no React, Vue, Tailwind build or Sass.
- Native **Web Components** (custom elements) are welcome for reusable parts: data table, filter bar, chart card, stat tile, modal.
- **Charts: Apache ECharts** is the only third-party library. It will be vendored as one local file. In the mockups you may load it from a CDN, but mark that clearly, because we will replace it with a local copy.
- Styling through **CSS custom properties (design tokens)**: colour, spacing, radius, typography, elevation. Both themes must be driven only by tokens.
- No icon font from a CDN. Use inline SVG icons, either a small hand-picked set or an open-licence set you name.
- Accessibility: WCAG 2.1 AA contrast in both themes, keyboard navigation, visible focus, chart colours safe for colour-blind users.
- The detail pages must print cleanly on A4 (dose summary for the patient file).

## Domain vocabulary and units (use them exactly)

| Metric | Meaning | Unit shown |
|---|---|---|
| CTDIvol | CT dose index (volume) | mGy |
| DLP | Dose-length product | mGy·cm |
| DAP / KAP | Dose-area product | Gy·cm² (allow the user to switch to mGy·cm² or µGy·m²) |
| Ka,r / RP dose | Air kerma at the reference point (fluoroscopy) | mGy or Gy |
| Fluoro time | Fluoroscopy time | mm:ss |
| AGD | Average glandular dose (mammography) | mGy |
| EI / DI | Exposure index / deviation index (DX) | unitless; DI target is 0, and ±3 is acceptable |
| kVp, mAs | Exposure factors | kV, mAs |
| SSDE | Size-specific dose estimate (CT) | mGy |

**Modalities:** CT, DX/CR (radiography), RF/XA (fluoroscopy and interventional), MG (mammography), later NM/PET.

**Statistics shown:**
- n (count), median, mean, IQR, P75 (the DRL is usually set at the 75th percentile);
- min and max, and outliers;
- a comparison against a reference value (local DRL or national DRL).

## Screens to mock up (for each of the 3 directions)

### 1. Home / overview dashboard
- Per modality: studies this week/month, and a trend sparkline.
- **Device health:** a list of devices with the date of the last study received. Highlight stale feeds, for example "no data for 3 days", because broken DICOM feeds are a real operational problem.
- Open **alerts**: high-dose fluoroscopy, CT dose-check notifications, studies above the DRL.
- Quick search by accession number or patient ID.
- A local vs external equipment toggle. Studies from outside hospitals also arrive through the PACS.

### 2. Study list (CT example)
- **Filters:**
  - date range;
  - study description, requested procedure, acquisition protocol;
  - device (display name, manufacturer, model, station);
  - patient age range and weight range;
  - accession number, patient ID (only for authorised users);
  - total DLP range;
  - number of events by type (spiral, axial, localiser);
  - "include test/phantom studies";
  - local/external.

  We need a pattern that does not take a permanent 300 px sidebar: for example, a filter bar with chips, an expandable advanced panel, and saved filters. The filter state must also live in the URL, so views can be shared and bookmarked.
- **Columns:**
  - date/time;
  - device;
  - accession;
  - patient (age, sex, weight);
  - study description;
  - number of events;
  - total DLP;
  - a flag when the study is above the DRL or has an alert.
- Server-side paging; 500,000+ rows exist. Columns are sortable and can be shown or hidden, and the table has density modes.
- Actions: export the current filter as CSV or XLSX, and "show charts for this selection".

### 3. Study detail (fluoroscopy / interventional example, the richest one)
- **Header:**
  - patient (ID and name visible only for authorised users; otherwise pseudonymised);
  - age, sex, height, weight;
  - accession, date, device, physician.
- **Dose summary tiles:**
  - total DAP;
  - total Ka,r;
  - fluoro time;
  - number of acquisitions;
  - split fluoro/acquisition and plane A/B for biplane systems;
  - estimated peak skin dose (later).
- **Cumulative context:** the same patient's previous studies in the last 12 weeks, with summed DAP and Ka,r, against the alert thresholds.
- **Event timeline:** each irradiation event placed on the time axis, with bar height = DAP. Clicking an event highlights it in the table.
- **Event table:**
  - time, protocol, type (fluoro/acquisition), plane;
  - kVp, mA, pulse rate;
  - Cu filter;
  - primary and secondary angles;
  - field size, SID;
  - DAP, Ka,r, duration.
- **Placeholder for a skin dose map:** a 2D unfolded body surface heat map, plus a 3D view later.
- A print-friendly version.

### 4. Charts / statistics workspace (CT example)
- Same filter model as the study list, so the user moves from a list to charts with the same selection.
- **Default view:** "CTDIvol per acquisition protocol, per device":
  - a horizontal boxplot or dot-strip per protocol, with one colour per device;
  - a vertical line at the DRL;
  - n shown per category.
- **Chart types to support:**
  - median/mean bar;
  - boxplot;
  - histogram;
  - trend over time (median per month, per device);
  - scatter of DLP against patient weight;
  - workload heat map (weekday × hour).
- **Options:**
  - grouping: by device or by protocol;
  - mean, median or both;
  - time period: day, week, month, quarter, year;
  - sort by name, value or count;
  - case-insensitive category merge;
  - raw names or standardised names.
- Every chart can be downloaded as PNG and as CSV data, and shows a small summary table (n, median, P75, IQR).
- How do users build and save a set of favourite charts, a "my audit dashboard"? Please propose a design.

### 5. Admin: devices
- A table of equipment records with these fields:
  - manufacturer, model, serial, station name, institution;
  - display name (editable inline);
  - modality override;
  - local/external;
  - study count;
  - last study date.
- **Merge workflow:** the same physical device can appear several times, for example with different capitalisation or after a software update. Show suggested merges and let the user select several records, merge them and choose the surviving display name.

Also show, briefly, for each direction:
- the **navigation shell** (top bar or side rail, modality switcher, user menu, language and theme switch);
- **empty, loading and error states**;
- the **colour palette** for the modalities and for an 8-device categorical series, plus the alert/status colours.

## Sample data (synthetic, realistic)
- **CT** (Siemens, GE, Philips scanners):
  - Head: CTDIvol about 45–60 mGy, DLP about 700–1000 mGy·cm.
  - Chest: CTDIvol about 5–12 mGy, DLP about 200–450.
  - Abdomen-pelvis: CTDIvol about 8–15 mGy, DLP about 400–800.
  - Localisers: about 0.1–0.3 mGy.
- **DX:**
  - Chest PA: DAP about 0.05–0.15 Gy·cm², EI about 250–400 (vendor-dependent).
  - Lumbar spine LAT: DAP about 1–3 Gy·cm².
- **Fluoroscopy and interventional:**
  - Barium swallow: DAP about 5–15 Gy·cm², about 2 min fluoro.
  - Coronary angiography: DAP about 20–60 Gy·cm², Ka,r about 0.3–1 Gy.
  - Complex PCI: up to 300 Gy·cm², Ka,r above 3 Gy, which should trigger an alert.
- **Devices:** use generic names such as "CT 1 – Emergency", "DR Room 2", "Mobile DR ICU", "Angio Suite", "Fluoro Room". Do not use real hospital names.
- **Patients:** synthetic IDs and Romanian-style names, such as "POPESCU^ION". Accession numbers such as "A2026112100123".

## Deliverables (please return)

For **each** of the 3 directions (A, B, C):
1. A short **rationale**: who it serves best, its strengths and its trade-offs.
2. A folder `variant-a/` (and so on) containing:
   - `tokens.css`: all design tokens, with light and dark themes;
   - `base.css`;
   - `components/*.js`: web components such as the table, filter bar and chart card;
   - one HTML file per screen (`home.html`, `study-list.html`, `study-detail.html`, `charts.html`, `admin-devices.html`), each runnable by opening it locally;
   - sample data as `data/*.json` or inline modules.
3. A one-page **component inventory**: names, purpose and states.

Keep the code clean and readable. We will build on it, so plain semantic HTML, small ES modules and no inline-style soup. Clarity of numbers and units matters more than decoration: this is a measurement tool.
