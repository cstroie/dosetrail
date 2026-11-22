// Data quality: rule counts, completeness per device (exam code, age, weight, sex) and the
// most recent studies with findings. Rules are explained in dosetrail/quality.py.

import '../components/dt-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import '../components/dt-query-bar.js';
import { h, clear, append } from '../core/dom.js';
import { num } from '../core/format.js';
import { quality, studyHref } from '../core/api.js';
import { prefs } from '../core/prefs.js';

const page = document.getElementById('page');
let q = prefs.get('quality.q') || 'origin:local date:90d';

function pct(x) {
  const v = Math.round(x * 1000) / 10;
  const cls = v >= 95 ? 'ok' : v >= 80 ? 'warn' : 'alert';
  return h('span', { class: `tag ${cls}` }, `${v} %`);
}

function render() {
  const qb = h('dt-query-bar', { value: q });
  qb.addEventListener('query-change', (e) => { q = e.detail; prefs.set('quality.q', q); load(); });
  clear(page).append(
    adminTabs('quality.html'),
    qb, h('div', { id: 'qa' }));
  load();
}

async function load() {
  const host = document.getElementById('qa');
  clear(host).append(h('p', { class: 'muted' }, 'Checking…'));
  let r;
  try { r = await quality({ q, limit: 200 }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const rules = Object.entries(r.rules);
  clear(host).append(
    h('div', { class: 'kpis drl-kpis' }, rules.map(([, v]) => h('div', { class: 'kpi', title: v.title },
      h('span', { class: 'code' }, v.title), h('span', { class: 'v' }, num(v.count)),
      h('span', { class: 'm' }, v.severity === 'error' ? 'error' : 'check')))),
    h('p', { class: 'muted small' }, `${num(r.studies)} studies checked`),
    h('h2', { class: 'section-title' }, 'Completeness per device'),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['Device', 'Studies', 'Exam code', 'Age', 'Weight', 'Weight measured', 'Sex', 'Flagged']
        .map((c, i) => h('th', { class: i ? 'r' : '' }, c)))),
      h('tbody', {}, r.devices.map((d) => h('tr', {},
        h('td', {}, d.device_name || '(no device)'), h('td', { class: 'r' }, num(d.studies)),
        ['exam_code', 'age', 'weight', 'weight_measured', 'sex'].map((k) => h('td', { class: 'r' }, pct(d.share[k]))),
        h('td', { class: 'r' }, num(d.flagged)))))))),
    h('h2', { class: 'section-title' }, 'Studies with findings (most recent first)'));
  const body = h('tbody', {}, r.flagged.map((f) => h('tr', {},
    h('td', {}, h('a', { href: studyHref(f) }, (f.date || '').replace('T', ' '))), h('td', {}, f.modality),
    h('td', {}, f.device_name || ''), h('td', {}, f.description || ''),
    h('td', {}, f.findings.map((x) => h('div', {}, h('span', { class: `tag ${x.severity === 'error' ? 'alert' : 'warn'}` }, x.rule.replaceAll('_', ' ')), ' ', x.detail))))));
  if (!r.flagged.length) body.append(h('tr', {}, h('td', { colspan: 5, class: 'empty' }, 'No findings for this query.')));
  append(host, [h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, ['Date', 'Mod.', 'Device', 'Description', 'Findings'].map((c) => h('th', {}, c)))), body)))]);
}

render();
