// Data quality: rule counts, completeness per device (exam code, age, weight, sex) and the
// most recent studies with findings. Rules are explained in dozimetrix/quality.py.

import { confirmDialog } from '../core/confirm.js';
import '../components/dx-shell.js';
import { analysisTabs } from '../components/admin-tabs.js';
import '../components/dx-query-bar.js';
import { h, clear, append, section } from '../core/dom.js';
import { num } from '../core/format.js';
import { isAdmin, quality, studyHref, importSizes } from '../core/api.js';
import { prefs } from '../core/prefs.js';
import { adminDefaults, defaultsActions, keepOwn } from '../core/page-defaults.js';

const page = document.getElementById('page');
const admin = await isAdmin();
const defaults = await adminDefaults('quality');
const DEFAULT_Q = defaults.value.q || 'origin:local date:90d';
if (prefs.get('quality.q') === DEFAULT_Q) prefs.set('quality.q', undefined);  // the same as the default: not the user's own
// a link's query (?q=) for this visit, then the user's own, then the default
let q = new URLSearchParams(location.search).get('q') ?? prefs.get('quality.q') ?? DEFAULT_Q;

function pct(x) {
  if (x == null) return h('span', { class: 'muted' }, '—');
  const v = Math.round(x * 1000) / 10;
  const cls = v >= 95 ? 'ok' : v >= 80 ? 'warn' : 'alert';
  return h('span', { class: `tag ${cls}` }, `${v} %`);
}

function render() {
  const qb = h('dx-query-bar', { value: q });
  qb.addEventListener('query-change', (e) => { q = e.detail; keepOwn('quality.q', q, DEFAULT_Q); load(); });
  clear(page).append(
    analysisTabs('quality.html'),
    h('div', { class: 'row', style: { 'align-items': 'flex-start' } }, h('div', { style: { flex: '1 1 600px' } }, qb),
      ...defaultsActions('quality', { admin: defaults.admin, prefKeys: ['quality.q'], current: () => ({ q }) })),
    h('div', { id: 'qa' }), admin ? sizesPanel() : null);
  load();
}

async function load() {
  const u = new URL(location.href);  // the query in the address bar: a copied link opens the same checks
  u.search = q ? `?q=${encodeURIComponent(q)}` : '';
  history.replaceState(null, '', u);
  const host = document.getElementById('qa');
  clear(host).append(h('p', { class: 'muted' }, 'Checking…'));
  let r;
  try { r = await quality({ q, limit: 200 }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const rules = Object.entries(r.rules);
  const body = h('tbody', {}, r.flagged.map((f) => h('tr', {},
    h('td', {}, h('a', { href: studyHref(f) }, (f.date || '').replace('T', ' '))), h('td', {}, f.modality),
    h('td', {}, f.device_name || ''), h('td', {}, f.description || ''),
    h('td', {}, f.findings.map((x) => h('div', {}, h('span', { class: `tag ${x.severity === 'error' ? 'alert' : 'warn'}` }, x.rule.replaceAll('_', ' ')), ' ', x.detail))))));
  if (!r.flagged.length) body.append(h('tr', {}, h('td', { colspan: 5, class: 'empty' }, 'No findings for this query.')));
  clear(host).append(
    h('div', { class: 'kpis drl-kpis' }, rules.map(([, v]) => h('div', { class: 'kpi', title: v.title },
      h('span', { class: 'code' }, v.title), h('span', { class: 'v' }, num(v.count)),
      h('span', { class: 'm' }, v.severity === 'error' ? 'error' : 'check')))),
    section('Completeness per device', { sub: `${num(r.studies)} studies checked` },
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, ['Device', 'Studies', 'Exam code', 'Age', 'Weight', 'Weight measured', 'Weight HIS record', 'Sex', 'HIS request', 'Flagged']
          .map((c, i) => h('th', { class: i ? 'r' : '' }, c)))),
        h('tbody', {}, r.devices.map((d) => h('tr', {},
          h('td', {}, d.device_name || '(no device)'), h('td', { class: 'r' }, num(d.studies)),
          ['exam_code', 'age', 'weight', 'weight_measured', 'weight_his_undated', 'sex', 'request'].map((k) => h('td', { class: 'r' }, pct(d.share[k]))),
          h('td', { class: 'r' }, num(d.flagged))))))))),
    section('Studies with findings', { sub: 'most recent first' },
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, ['Date', 'Mod.', 'Device', 'Description', 'Findings'].map((c) => h('th', {}, c)))), body)))));
}

// Weight / height from a CSV file (RIS export, ward records). Check = dry run; Apply writes.
function sizesPanel() {
  const file = h('input', { type: 'file', accept: '.csv,.txt,text/csv' });
  const out = h('div', {});
  const run = async (apply) => {
    if (!file.files.length) { clear(out).append(h('p', { class: 'muted' }, 'Choose a CSV file first.')); return; }
    clear(out).append(h('p', { class: 'muted' }, apply ? 'Writing…' : 'Checking…'));
    let r;
    try { r = await importSizes(file.files[0], apply); } catch (e) { clear(out).append(h('p', { class: 'error' }, e.message)); return; }
    clear(out).append(
      h('p', {}, h('span', { class: `tag ${r.applied ? 'ok' : 'warn'}` }, r.applied ? 'applied' : 'dry run — nothing written'),
        ' ', Object.entries(r.counts).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${num(v)}`).join(' · ')),
      r.issues.length ? h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, ['Row', 'Outcome', 'Detail'].map((c) => h('th', {}, c)))),
        h('tbody', {}, r.issues.map((i) => h('tr', {}, h('td', {}, String(i.row)), h('td', {}, i.outcome.replaceAll('_', ' ')), h('td', {}, i.detail)))))) : '');
  };
  return section('Import weight, height, indication (CSV)', {},
    h('div', { class: 'panel', style: { padding: 'var(--sp-3)', display: 'grid', gap: 'var(--sp-2)' } },
      h('p', { class: 'muted small', style: { margin: 0 } }, 'Columns: a key (study_uid, accession, or patient_id + date [+ modality]), then weight_kg and/or height_m / height_cm (optionally source: measured, chart_same_day, chart_72h, imported, or measured_at), and/or indication (the request text) / indication_code. A weight or height replaces only a lower-ranked source, and only when plausible for age and sex.'),
      h('div', { class: 'row' }, file,
        h('button', { class: 'btn', onclick: () => run(false) }, 'Check'),
        h('button', { class: 'btn primary', onclick: async () => { if (await confirmDialog('Write these weights / heights?', { title: 'Write weights and heights', label: 'Write' })) run(true); } }, 'Apply')),
      out));
}

render();
