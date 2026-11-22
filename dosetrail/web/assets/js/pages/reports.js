// Regulatory reports. First: the quarterly centralised report of Order MS 1255/2016 (INSP),
// radiology and CT sheets. The XLSX keeps the official column order so the numeric block
// can be pasted into the INSP workbook.

import '../components/dt-shell.js';
import { h, clear, append, toast } from '../core/dom.js';
import { num, smart } from '../core/format.js';
import { inspReport, inspReportUrl, surveyUrl, drlTrend, drlTrendUrl, gridReport, gridReportUrl } from '../core/api.js';

const page = document.getElementById('page');
const today = new Date();
const lastQuarter = Math.floor(today.getMonth() / 3) || 4;
const state = { year: lastQuarter === 4 && today.getMonth() < 3 ? today.getFullYear() - 1 : today.getFullYear(), quarter: lastQuarter, estimated: true };

function render() {
  const years = [];
  for (let y = today.getFullYear(); y >= today.getFullYear() - 8; y -= 1) years.push(y);
  const yearSel = h('select', { class: 'input', 'aria-label': 'Year', onchange: (e) => { state.year = Number(e.target.value); } },
    years.map((y) => h('option', { value: y, selected: y === state.year }, String(y))));
  const qSel = h('select', { class: 'input', 'aria-label': 'Quarter', onchange: (e) => { state.quarter = Number(e.target.value); } },
    [1, 2, 3, 4].map((q) => h('option', { value: q, selected: q === state.quarter }, `T${q}`)));
  clear(page).append(
    h('h1', { class: 'section-title' }, 'Raportare trimestrială — OMS 1255/2016 (INSP)'),
    h('div', { class: 'toolbar' }, yearSel, qSel,
      h('button', { class: 'btn primary', type: 'button', onclick: load }, 'Preview'),
      h('button', { class: 'btn', type: 'button', onclick: download }, 'Download XLSX'),
      h('span', { class: 'muted small' }, 'Local devices only. One examination = one study × one procedure code.')),
    h('div', { id: 'rep' }),
    h('h1', { class: 'section-title', style: { 'margin-top': 'var(--sp-4)' } }, 'Analiza semestrială — medii trimestriale față de NRD (OMS 1245/2021)'),
    h('div', { class: 'toolbar' },
      h('button', { class: 'btn primary', type: 'button', onclick: loadTrend }, 'Show 6 quarters ending at the selected one'),
      h('button', { class: 'btn', type: 'button', onclick: () => {
        const href = drlTrendUrl({ year: state.year, quarter: state.quarter });
        if (!href) { toast('Reports need the server'); return; }
        h('a', { href, download: '' }).click();
      } }, 'Download XLSX'),
      h('span', { class: 'muted small' }, 'Quarterly mean / national DRL; median shown to spot outliers. Rows above the DRL most often first.')),
    h('div', { id: 'trend' }),
    h('h1', { class: 'section-title', style: { 'margin-top': 'var(--sp-4)' } }, 'Grila ICRP 135 / RP 185 (extinsă) — alături de grila națională'),
    h('div', { class: 'toolbar' },
      h('button', { class: 'btn primary', type: 'button', onclick: loadGrid }, 'Show the selected quarter'),
      h('button', { class: 'btn', type: 'button', onclick: () => {
        const href = gridReportUrl({ year: state.year, quarter: state.quarter, estimated: state.estimated });
        if (!href) { toast('Reports need the server'); return; }
        h('a', { href, download: '' }).click();
      } }, 'Download XLSX'),
      h('label', { class: 'row', title: 'Missing or implausible weights replaced by the median weight for age and sex (WHO to 10 y, CDC 2000 to 20 y). Off: those body exams count as "Weight unknown".' },
        h('input', { type: 'checkbox', checked: state.estimated, onchange: (e) => { state.estimated = e.target.checked; loadGrid(); } }),
        'Include estimated weights (WHO / CDC median)'),
      h('span', { class: 'muted small' }, 'Head exams by age (0–<3 mo, 3 mo–<1 y, 1–<6 y, 6–<18 y, ≥18 y), body exams by weight (<5 … ≥110 kg). Median vs RP 185 EDRL where it exists.')),
    h('div', { id: 'grid' }),
    h('h1', { class: 'section-title', style: { 'margin-top': 'var(--sp-4)' } }, 'Studiul național de dozimetrie pediatrică (plan draft)'),
    surveyBar(),
  );
  load();
}

async function loadTrend() {
  const host = document.getElementById('trend');
  clear(host).append(h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try { r = await drlTrend({ year: state.year, quarter: state.quarter }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const cell = (x) => {
    if (!x) return h('td', { class: 'r muted' }, '—');
    const cls = x.mean_over_drl > 1.5 ? 'alert' : x.mean_over_drl > 1 ? 'warn' : 'ok';
    return h('td', { class: 'r', title: `n ${x.n} · mean ${smart(x.mean)} · median ${smart(x.median)}` },
      h('span', { class: `tag ${cls}` }, `${num(x.mean_over_drl, 2)}×`), h('br'), h('span', { class: 'muted small' }, `med ${smart(x.median)} · n ${x.n}`));
  };
  clear(host).append(h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Exam'), h('th', {}, 'Group'), h('th', { class: 'r' }, 'DRL'),
      r.quarters.map((q) => h('th', { class: 'r' }, q)), h('th', { class: 'r' }, 'Above'), h('th', { class: 'r' }, 'Trend'))),
    h('tbody', {}, r.rows.map((x) => h('tr', { class: x.last_above ? 'hot' : '' },
      h('td', { title: x.exam }, x.exam_meaning), h('td', {}, x.band_label || 'all'),
      h('td', { class: 'r' }, `${smart(x.reference)} ${x.unit}`), x.quarters.map(cell),
      h('td', { class: 'r' }, `${x.quarters_above}/${x.quarters_with_data}`),
      h('td', { class: 'r' }, x.trend_pct_per_quarter == null ? '' : `${x.trend_pct_per_quarter > 0 ? '+' : ''}${num(x.trend_pct_per_quarter, 0)} %/q`)))))))); 
}

async function loadGrid() {
  const host = document.getElementById('grid');
  clear(host).append(h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try { r = await gridReport({ year: state.year, quarter: state.quarter, estimated: state.estimated }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const tone = { below: 'ok', above: 'warn', well_above: 'alert', insufficient: '' };
  const label = { below: '≤ EDRL', above: '≤ 1.5×', well_above: '> 1.5×', insufficient: `n < ${r.min_n}` };
  const n = r.notes;
  clear(host).append(h('p', { class: 'muted small' }, `${r.start} – ${r.end} · body values: ${num(n.body_values)}, of which weight estimated (median for age and sex, WHO / CDC) ${num(n.body_weight_estimated || 0)}, no weight ${num(n.body_weight_unknown)}`
    + (n.body_values && n.body_weight_unknown / n.body_values > 0.5 ? ' — the weight grid needs weights (RIS or entered at the modality).' : '')
    + (r.estimated_weights ? '. Estimated weights fill the weight groups; they are never measured values.' : '. Estimated weights left out: only reported weights place body exams in a group.')));
  for (const [param, title] of [['age', 'Head examinations · by age'], ['weight', 'Body examinations · by weight']]) {
    const rows = r.rows.filter((x) => x.parameter === param);
    let last = null;
    const body = h('tbody', {}, rows.map((x) => {
      const first = `${x.exam}|${x.quantity}` !== last;
      last = `${x.exam}|${x.quantity}`;
      return h('tr', { class: first ? 'group-start' : '' },
        h('td', { title: x.exam }, first ? x.exam_meaning : ''), h('td', {}, first ? `${x.quantity} (${x.unit})` : ''),
        h('td', {}, x.group_label), h('td', { class: 'r' }, num(x.n)), h('td', { class: 'r muted' }, x.excluded ? num(x.excluded) : ''),
        h('td', { class: 'r muted', title: 'values with an estimated weight' }, x.estimated ? num(x.estimated) : ''),
        h('td', { class: 'r' }, x.median == null ? '' : smart(x.median)), h('td', { class: 'r' }, x.p75 == null ? '' : smart(x.p75)),
        h('td', { class: 'r' }, x.mean == null ? '' : smart(x.mean)),
        h('td', { class: 'r' }, x.edrl == null ? '—' : smart(x.edrl)),
        h('td', { class: 'r' }, x.status ? h('span', { class: `tag ${tone[x.status]}`, title: x.ratio ? `median / EDRL = ${num(x.ratio, 2)}` : '' }, x.ratio && x.status !== 'insufficient' ? `${num(x.ratio, 2)}×` : label[x.status]) : ''));
    }));
    if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 11, class: 'empty' }, 'No examination in this quarter.')));
    append(host, [h('h2', { class: 'section-title' }, title),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, ['Exam', 'Quantity', 'Group', 'n', 'Excl.', 'Est. wt', 'Median', 'P75', 'Mean', 'EDRL', 'vs EDRL'].map((c, i) => h('th', { class: i >= 3 ? 'r' : '' }, c)))), body)))]);
  }
}

function surveyBar() {
  const prev = new Date(today.getFullYear(), today.getMonth() - 1, 1);
  const month = h('input', { class: 'input', type: 'month', 'aria-label': 'Month',
    value: `${prev.getFullYear()}-${String(prev.getMonth() + 1).padStart(2, '0')}` });
  const fmt = h('select', { class: 'input', 'aria-label': 'Format' }, ['csv', 'json', 'xlsx'].map((f) => h('option', { value: f }, f.toUpperCase())));
  return h('div', { class: 'toolbar' }, month, fmt,
    h('button', { class: 'btn', type: 'button', onclick: () => {
      const href = surveyUrl({ month: month.value, format: fmt.value });
      if (!href) { toast('Exports need the server'); return; }
      h('a', { href, download: '' }).click();
      toast('Survey export started');
    } }, 'Download survey export'),
    h('span', { class: 'muted small' }, 'Patients < 18 y, local devices. Month only, keyed codes, no name / CNP / birth date. CT one row per series; checks DLP ±10 % and phantom.'));
}

function download() {
  const href = inspReportUrl({ year: state.year, quarter: state.quarter });
  if (!href) { toast('Reports need the server'); return; }
  h('a', { href, download: '' }).click();
  toast('Report export started');
}

async function load() {
  const host = document.getElementById('rep');
  clear(host).append(h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try {
    r = await inspReport({ year: state.year, quarter: state.quarter, q: 'origin:local' });
  } catch (e) {
    clear(host).append(h('p', { class: 'error' }, e.message));
    return;
  }
  const kpis = h('div', { class: 'kpis drl-kpis' },
    r.sheets.filter((s) => s.rows.some((x) => x.total)).map((s) => h('div', { class: 'kpi' }, h('span', { class: 'code' }, s.title),
      h('span', { class: 'v' }, num(s.rows.reduce((a, x) => a + x.total, 0))),
      h('span', { class: 'm' }, `examinations in ${s.rows.filter((x) => x.total).length} codes`))),
    h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Not reported'), h('span', { class: 'v' }, num(r.notes.unclassified)),
      h('span', { class: 'm' }, 'studies without a national code (e.g. limbs)')),
    h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Sex other / unknown'), h('span', { class: 'v' }, num(r.notes.other_sex)),
      h('span', { class: 'm' }, 'counted in totals, not in M/F columns')));
  clear(host).append(h('p', { class: 'muted small' }, `${r.period.from} – ${r.period.to}`), kpis);
  for (const s of r.sheets) {
    const rows = s.rows.filter((x) => x.total);
    const qs = s.quantities.filter((q) => q.key);
    const head = h('thead', {}, h('tr', {},
      h('th', {}, 'Cod'), h('th', {}, 'Examinare'), h('th', { class: 'r' }, 'Total'),
      s.count_bands.map((b) => h('th', { class: 'r' }, `${b.label} M/F`)),
      qs.map((q) => s.mean_bands.map((b) => h('th', { class: 'r' }, `${q.label} ${b.label}`)))));
    const body = h('tbody', {}, rows.map((x) => h('tr', {},
      h('td', { class: 'mono' }, x.code), h('td', {}, [x.exam, x.type].filter(Boolean).join(' — ')),
      h('td', { class: 'r' }, num(x.total)),
      s.count_bands.map((b) => h('td', { class: 'r' }, `${x.counts[b.key].M || '·'} / ${x.counts[b.key].F || '·'}`)),
      qs.map((q) => s.mean_bands.map((b) => h('td', { class: 'r' }, x.means[q.key][b.key] == null ? '' : smart(x.means[q.key][b.key])))))));
    if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 40, class: 'empty' }, 'No examination in this quarter.')));
    append(host, [h('h2', { class: 'section-title' }, `${s.title} · ${qs.map((q) => `${q.label} (${q.unit})`).join(' · ')}`),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' }, head, body)))]);
  }
}

render();
