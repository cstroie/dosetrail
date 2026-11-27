// Analysis: the quality and optimisation reports built on the stored doses and the scanner's own
// settings: CT Dose Check, exposure / deviation index, collimation, CT scan length, possible
// repeats, CT effective dose, patient centering, and the before/after of a protocol change. Each is
// a server report (dozimetrix/reports/…); the page picks one, a period and a study query, and
// shows its tables with a CSV download. Read-only; the explanations are the reports' own.

import '../components/dx-shell.js';
import { h, clear, section, pageHead } from '../core/dom.js';
import { num } from '../core/format.js';
import { analysisReport, analysisUrl } from '../core/api.js';
import { dataTable, kpiRow, tag, field } from '../core/tables.js';
import { analysisTabs, doseTabs } from '../components/admin-tabs.js';

const page = document.getElementById('page');
const pct = (v) => (v == null ? null : `${num(v, 1)} %`);
const mm = (v) => (v == null ? null : `${num(v, 1)} mm`);
const link = (id) => h('a', { href: `study.html?id=${id}` }, `#${id}`);

const REPORTS = {
  'dose-check': {
    title: 'CT Dose Check', about: 'Acquisitions that went past the scanner\'s notification or alert value (NEMA XR-25), per protocol and room, with the reason the operator gave. Only scanners that export the Dose Check details appear: a room with none may have the function off.',
    render: (r) => [
      kpiRow([['Acquisitions', r.events], ['Notifications', r.notifications], ['Alerts', r.alerts]]),
      block('Protocols', dataTable([{ key: 'protocol', label: 'Protocol' }, { key: 'events', label: 'Acquisitions', right: true }, { key: 'notifications', label: 'Notifications', right: true }, { key: 'alerts', label: 'Alerts', right: true }, { key: 'rate_pct', label: 'Rate', right: true, fmt: pct }, { key: 'median_fwd_dlp', label: 'Median forward DLP', right: true }], r.protocols)),
      block('Rooms', dataTable([{ key: 'device', label: 'Room' }, { key: 'events', label: 'Acquisitions', right: true }, { key: 'notifications', label: 'Notifications', right: true }, { key: 'alerts', label: 'Alerts', right: true }, { key: 'rate_pct', label: 'Rate', right: true, fmt: pct }], r.rooms)),
      block('Reasons given for proceeding', dataTable([{ key: 'reason', label: 'Reason' }, { key: 'n', label: 'n', right: true }], r.reasons)),
    ],
  },
  'exposure-index': {
    title: 'Exposure index', about: 'Deviation index (IEC 62494-1) of digital radiography per room and protocol: above +3 the image is overexposed, below −2 it is noisy. The target is the one the detector reports; a protocol whose target is not the same in every exposure is flagged.',
    render: (r) => [
      kpiRow([['Exposures with a DI', r.exposures], ['Outside −2…+3', r.outside], ['Share', r.exposures ? `${num(100 * r.outside / r.exposures, 1)} %` : '—']]),
      dataTable([{ key: 'device', label: 'Room' }, { key: 'protocol', label: 'Protocol' }, { key: 'n', label: 'n', right: true }, { key: 'median_di', label: 'Median DI', right: true }, { key: 'p5_di', label: 'P5', right: true }, { key: 'p95_di', label: 'P95', right: true }, { key: 'above_pct', label: '> +3', right: true, fmt: pct }, { key: 'below_pct', label: '< −2', right: true, fmt: pct }, { key: 'target_ei', label: 'Target EI', right: true }, { get: (x) => (x.targets_differ ? tag('varies', 'warn') : null), label: 'Target' }], r.rows),
    ],
  },
  collimation: {
    title: 'Collimation', about: 'The exposed field area (DAP ÷ air kerma) per room against the department for the same exam and patient group (ICRP 135 / RP 185: head by age, the rest by weight). Every exam high by the same factor points to a DAP-meter calibration error; only some exams high points to open collimation. A calibration factor from the QA register is applied first.',
    render: (r) => [
      dataTable([{ key: 'device', label: 'Room' }, { get: (x) => tag({ uniform_high: 'all exams high · calibration?', uniform_low: 'all exams low · calibration?', collimation: 'some exams high · collimation', ok: 'ok' }[x.verdict], x.verdict === 'ok' ? 'ok' : 'warn'), label: 'Verdict' }, { key: 'groups', label: 'Groups', right: true }, { key: 'median_ratio', label: 'Median ratio', right: true }, { key: 'high_groups', label: `High groups (≥ ${r.high}×)`, right: true }, { key: 'exposures', label: 'Exposures', right: true }], r.rooms),
      block('Where it is high', dataTable([{ key: 'device', label: 'Room' }, { key: 'exam', label: 'Exam' }, { key: 'band', label: 'Age' }, { key: 'n', label: 'n', right: true }, { key: 'median_area_cm2', label: 'Field area, cm²', right: true }, { key: 'dept_area_cm2', label: 'Department, cm²', right: true }, { key: 'ratio', label: 'Ratio', right: true }],
        r.rooms.flatMap((x) => x.detail.filter((g) => g.ratio >= r.high).map((g) => ({ device: x.device, ...g }))))),
    ],
  },
  'scan-length': {
    title: 'CT scan length', about: 'Series much longer than the department\'s for the same exam and patient group (ICRP 135 / RP 185: head by age, the rest by weight; ≥ the ratio × the median and above P95): a head scan running into the neck, an abdomen extended into the chest. Extra rotations at the ends of a helical scan are not in the dose report and are not measured.',
    render: (r) => [
      kpiRow([['Flagged series', r.flagged_total]]),
      block('Exam and patient group', dataTable([{ key: 'exam', label: 'Exam' }, { key: 'band', label: 'Patient group' }, { key: 'n', label: 'n', right: true }, { key: 'median_mm', label: 'Median', right: true }, { key: 'p75_mm', label: 'P75', right: true }, { key: 'p95_mm', label: 'P95', right: true }, { key: 'max_mm', label: 'Max', right: true }, { key: 'flagged', label: 'Flagged', right: true }], r.groups)),
      block('Rooms', dataTable([{ key: 'device', label: 'Room' }, { key: 'series', label: 'Series', right: true }, { key: 'flagged', label: 'Flagged', right: true }, { key: 'flagged_pct', label: 'Share', right: true, fmt: pct }], r.rooms)),
      block('Flagged series', dataTable([{ get: (x) => link(x.study_id), label: 'Study' }, { key: 'device', label: 'Room' }, { key: 'time', label: 'Time', fmt: (v) => v.replace('T', ' ') }, { key: 'exam', label: 'Exam' }, { key: 'band', label: 'Age' }, { key: 'protocol', label: 'Protocol' }, { key: 'length_mm', label: 'Length, mm', right: true }, { key: 'median_mm', label: 'Median, mm', right: true }, { key: 'ratio', label: 'Ratio', right: true }], r.flagged)),
    ],
  },
  repeats: {
    title: 'Possible repeats', about: 'A proxy for the reject rate, not the rate. An exposure of the same exam, projection, laterality and protocol as an earlier one in its study, within the gap, is counted. It includes legitimate duplicates and misses a retake under another protocol; no rejected-image notes are received, so true rejects are not available. Compare rooms and exams, do not quote the figure.',
    extra: (state) => [h('label', { class: 'chk', title: 'Per operator (admin only)', style: { 'padding-bottom': '6px' } }, Object.assign(h('input', { type: 'checkbox' }), { checked: !!state.operators, onchange: (e) => { state.operators = e.target.checked || undefined; } }), ' per operator')],
    render: (r) => [
      kpiRow([['Exposures', r.exposures], ['Possible repeats', r.repeats], ['Rate', pct(r.rate_pct)], ['Rooms flagged', r.flagged]]),
      block('Rooms and exams', dataTable([{ key: 'device', label: 'Room' }, { key: 'exam', label: 'Exam' }, { key: 'exposures', label: 'Exposures', right: true }, { key: 'repeats', label: 'Repeats', right: true }, { key: 'rate_pct', label: 'Rate', right: true, fmt: pct }, { key: 'department_pct', label: 'Department', right: true, fmt: pct }, { key: 'ratio', label: 'Ratio', right: true }, { get: (x) => (x.flagged ? tag('flagged', 'warn') : null), label: '' }], r.rooms)),
      block('Department, per exam', dataTable([{ key: 'exam', label: 'Exam' }, { key: 'exposures', label: 'Exposures', right: true }, { key: 'repeats', label: 'Repeats', right: true }, { key: 'rate_pct', label: 'Rate', right: true, fmt: pct }], r.exams)),
      r.operators ? block('Operators', dataTable([{ key: 'operator', label: 'Operator' }, { key: 'exam', label: 'Exam' }, { key: 'exposures', label: 'Exposures', right: true }, { key: 'repeats', label: 'Repeats', right: true }, { key: 'rate_pct', label: 'Rate', right: true, fmt: pct }, { key: 'ratio', label: 'Ratio', right: true }], r.operators)) : null,
    ],
  },
  'effective-dose': {
    title: 'CT effective dose', about: 'DLP × k by body region, age and CTDI phantom: a population-level estimate for comparing exams, rooms and years, not a patient\'s dose. Studies without a coefficient for their region, age and phantom have no estimate. Organ doses are not estimated.',
    render: (r) => [
      r.coefficients ? null : h('p', { class: 'panel', style: { padding: 'var(--sp-3)' } }, 'No conversion coefficients have been entered, so nothing is estimated. ', h('a', { href: 'kfactors.html' }, 'Enter them')),
      kpiRow([['CT studies', r.studies], ['Estimated', r.estimated], ['Partly estimated', r.partial]]),
      block('Exam and age group', dataTable([{ key: 'exam', label: 'Exam' }, { key: 'band', label: 'Age' }, { key: 'n', label: 'n', right: true }, { key: 'median_msv', label: 'Median, mSv', right: true }, { key: 'p75_msv', label: 'P75', right: true }, { key: 'max_msv', label: 'Max', right: true }, { key: 'total_msv', label: 'Total, mSv', right: true }], r.groups)),
      block('Collective dose per room', dataTable([{ key: 'device', label: 'Room' }, { key: 'n', label: 'Studies', right: true }, { key: 'mean_msv', label: 'Mean, mSv', right: true }, { key: 'collective_msv', label: 'Collective, mSv', right: true }], r.rooms)),
      block('Not estimated', dataTable([{ key: 'reason', label: 'Reason' }, { key: 'n', label: 'Studies', right: true }], r.not_estimated)),
    ],
  },
  centering: {
    title: 'CT centering', about: 'How far patients lay from the gantry isocentre, from one axial image (vertical + = lower than the isocentre). Off unless [ingest] measure_centering is on, and not yet verified on real images: rows whose isocentre is the image centre rest on an assumption and are listed apart.',
    render: (r) => [
      kpiRow([['Measured', r.measured], ['Not measured', r.not_measured]]),
      dataTable([{ key: 'device', label: 'Room' }, { get: (x) => tag(x.method === 'data_collection_center' ? 'header isocentre' : 'image centre (assumed)', x.method === 'data_collection_center' ? 'ok' : 'warn'), label: 'Isocentre from' }, { key: 'n', label: 'n', right: true }, { key: 'median_vertical_mm', label: 'Median vertical', right: true, fmt: mm }, { key: 'p25_vertical_mm', label: 'P25', right: true, fmt: mm }, { key: 'p75_vertical_mm', label: 'P75', right: true, fmt: mm }, { key: 'median_lateral_mm', label: 'Median lateral', right: true, fmt: mm }, { key: 'beyond_10_pct', label: '> 10 mm', right: true, fmt: pct }, { key: 'beyond_20_pct', label: '> 20 mm', right: true, fmt: pct }], r.rooms, 'Nothing measured: turn on [ingest] measure_centering and receive images.'),
      block('Not measured', dataTable([{ key: 'reason', label: 'Reason' }, { key: 'n', label: 'n', right: true }], r.reasons)),
    ],
  },
  optimisation: {
    title: 'Before / after', about: 'The doses of the period before a protocol change against the period after it, per exam and age group, with a Mann-Whitney test. It says the dose changed, not that the images are still good: record the image-quality review in the note.',
    form: true,
    render: (r) => [
      r.image_quality_note ? h('p', { class: 'panel', style: { padding: 'var(--sp-3)' } }, h('strong', {}, 'Image quality: '), r.image_quality_note) : h('p', { class: 'muted' }, 'No image-quality note was recorded for this comparison.'),
      kpiRow(Object.entries(r.summary).map(([k, v]) => [k.replace('_', ' '), v])),
      dataTable([{ get: (x) => (x._first ? x.exam : h('span', {})), label: 'Exam' }, { key: 'band', label: 'Group' }, { key: 'quantity', label: 'Quantity' }, { key: 'n_before', label: 'n before', right: true }, { key: 'n_after', label: 'n after', right: true }, { key: 'median_before', label: 'Median before', right: true }, { key: 'median_after', label: 'Median after', right: true }, { key: 'change', label: 'Change', right: true, fmt: (v) => `${v > 0 ? '+' : ''}${num(100 * v, 1)} %` }, { key: 'p', label: 'p', right: true, fmt: (v) => (v < 0.0001 ? '< 0.0001' : num(v, 4)) }, { key: 'ratio_after', label: '× DRL after', right: true }, { get: (x) => tag(x.verdict.replace('_', ' '), x.verdict === 'lower' ? 'ok' : x.verdict === 'higher' ? 'alert' : ''), label: '' }], examBlocks(r.rows)),
    ],
  },
};

function block(title, body) { return section(title, { level: 3 }, body); }

/** Rows of one exam form a block: the exam shows on its first row only, and the block starts a new group. */
function examBlocks(rows) {
  let last = null;
  return rows.map((x) => {
    const first = x.exam !== last;
    last = x.exam;
    return { ...x, _first: first, _cls: first ? 'group-start' : '' };
  });
}

const url = new URLSearchParams(location.search);
const state = { name: REPORTS[url.get('r')] ? url.get('r') : 'dose-check', q: url.get('q') ?? 'origin:local', quarters: Number(url.get('quarters')) || 1,
  year: url.get('year') || '', quarter: url.get('quarter') || '', change: url.get('change') || '', days: url.get('days') || '180', set: '', note: '' };
const out = h('div', { style: { display: 'grid', gap: 'var(--sp-3)' } });

function params() {
  const p = { q: state.q, quarters: state.quarters, operators: state.operators };
  if (state.year && state.quarter) { p.year = state.year; p.quarter = state.quarter; }
  if (REPORTS[state.name].form) Object.assign(p, { change: state.change, days: state.days, set: state.set, note: state.note });
  return p;
}

async function run() {
  const def = REPORTS[state.name];
  const u = new URL(location.href);
  u.search = new URLSearchParams({ r: state.name, ...(state.q ? { q: state.q } : {}), ...(state.year ? { year: state.year, quarter: state.quarter } : {}), ...(state.change ? { change: state.change } : {}) }).toString();
  history.replaceState(null, '', u);
  if (def.form && !state.change) { clear(out).append(h('p', { class: 'muted' }, 'Choose the date of the change.')); return; }
  clear(out).append(h('p', { class: 'muted' }, 'Computing…'));
  let r;
  try { r = await analysisReport(state.name, params()); } catch (e) { clear(out).append(h('p', { class: 'error' }, e.message)); return; }
  clear(out).append(
    h('p', { class: 'muted small' }, def.form ? `Before ${r.before.join(' – ')} · after ${r.after.join(' – ')}` : `${r.start} – ${r.end}`,
      ' · ', h('a', { href: analysisUrl(state.name, params()) || '#', download: '' }, 'Download CSV')),
    ...def.render(r).filter(Boolean));
}

function controls() {
  const def = REPORTS[state.name];
  const q = h('input', { class: 'input', type: 'text', value: state.q, placeholder: 'a study query', title: 'A study query, as on Studies', style: { 'min-width': '16em' }, onchange: (e) => { state.q = e.target.value; } });
  const year = h('input', { class: 'input num-input', type: 'number', min: 2000, max: 2100, value: state.year, placeholder: 'auto', onchange: (e) => { state.year = e.target.value; } });
  const quarter = h('select', { class: 'input', onchange: (e) => { state.quarter = e.target.value; } },
    ['', 1, 2, 3, 4].map((v) => h('option', { value: v, selected: String(v) === String(state.quarter) }, v === '' ? 'last complete' : `Q${v}`)));
  const quarters = h('select', { class: 'input', onchange: (e) => { state.quarters = Number(e.target.value); } },
    [1, 2, 4, 8].map((v) => h('option', { value: v, selected: v === state.quarters }, `${v} quarter${v > 1 ? 's' : ''}`)));
  const go = h('button', { class: 'btn primary', type: 'button', onclick: run }, 'Show');
  const form = def.form ? [
    field('Change date', h('input', { class: 'input', type: 'date', value: state.change, onchange: (e) => { state.change = e.target.value; } })),
    field('Days each side', h('input', { class: 'input num-input', type: 'number', min: 7, max: 1500, value: state.days, onchange: (e) => { state.days = e.target.value; } })),
    field('Image-quality note', h('input', { class: 'input', type: 'text', maxlength: 1000, value: state.note, placeholder: 'reader scores, noise, feedback', style: { 'min-width': '22em' }, onchange: (e) => { state.note = e.target.value; } }), null),
  ] : [field('Period', h('span', { class: 'row' }, year, quarter, quarters))];
  return h('div', { class: 'row', style: { 'align-items': 'flex-end', gap: 'var(--sp-3)', 'flex-wrap': 'wrap' } },
    field('Studies', q), ...form, ...(def.extra ? def.extra(state) : []), go);
}

// The reports are tabs; switching keeps the query and period. Before / after belongs to Doses
// (it compares doses around a protocol change), so there the Doses bar and rail entry show.
function tabs() {
  const href = `analysis.html?r=${state.name}`;
  const dose = !!REPORTS[state.name].form;
  const shell = document.querySelector('dx-shell');
  shell?.querySelectorAll('nav.rail a').forEach((a) => {
    const on = a.getAttribute('href') === (dose ? 'drl.html' : 'analysis.html');
    a.setAttribute('aria-current', on ? 'page' : 'false');
  });
  shell?.setSub?.(dose ? 'Before / after' : REPORTS[state.name].title);
  const bar = dose ? doseTabs(href) : analysisTabs(href);
  bar.addEventListener('click', (e) => {
    const a = e.target.closest('a');
    const k = a && new URL(a.href).pathname.endsWith('analysis.html') && new URL(a.href).searchParams.get('r');
    if (!k || !REPORTS[k] || !!REPORTS[k].form !== dose) return;
    e.preventDefault(); state.name = k; draw();
  });
  return bar;
}

function draw() {
  clear(page).append(
    tabs(),
    pageHead(REPORTS[state.name].title, { sub: REPORTS[state.name].about }),
    controls(), out);
  run();
}

draw();
