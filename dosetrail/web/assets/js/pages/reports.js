// Regulatory reports. First: the quarterly centralised report of Order MS 1255/2016 (INSP),
// radiology and CT sheets. The XLSX keeps the official column order so the numeric block
// can be pasted into the INSP workbook.

import '../components/dt-shell.js';
import { h, clear, append, toast } from '../core/dom.js';
import { num, smart } from '../core/format.js';
import { inspReport, inspReportUrl, surveyUrl } from '../core/api.js';

const page = document.getElementById('page');
const today = new Date();
const lastQuarter = Math.floor(today.getMonth() / 3) || 4;
const state = { year: lastQuarter === 4 && today.getMonth() < 3 ? today.getFullYear() - 1 : today.getFullYear(), quarter: lastQuarter };

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
    h('h1', { class: 'section-title', style: { 'margin-top': 'var(--sp-4)' } }, 'Studiul național de dozimetrie pediatrică (plan draft)'),
    surveyBar(),
  );
  load();
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
