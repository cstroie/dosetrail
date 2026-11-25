// Reference levels: local medians against a DRL set (national OMS 838/2025, European RP 185).
// One row per exam × patient group × quantity, per facility or per device (ICRP 135 room
// medians). Status: below / above (≤ 1.5×) / well above (> 1.5×) / n below the minimum.

import '../components/dx-shell.js';
import '../components/dx-query-bar.js';
import { h, clear, append, toast } from '../core/dom.js';
import { scopeWarnings } from '../core/query.js';
import { num, smart, shown, shownUnit } from '../core/format.js';
import { drlSets, drlCompare, drlDownloadUrl } from '../core/api.js';
import { prefs } from '../core/prefs.js';
import { adminDefaults, defaultsActions, keepOwn } from '../core/page-defaults.js';

const page = document.getElementById('page');
const STATUS = {
  well_above: ['alert', '> 1.5× DRL'],
  above: ['warn', 'Above DRL'],
  below: ['ok', 'Below DRL'],
  insufficient: ['info', 'n too small'],
  no_data: ['', 'No data'],
  unavailable: ['', 'Not collected'],
};
const QUANTITY = { dlp: 'DLP', dlp_study: 'DLP (study)', ctdivol: 'CTDIvol', dap: 'DAP', kae: 'Ka,e', mgd: 'MGD', activity: 'Activity' };

const defaults = await adminDefaults('drl');
const D = { q: 'origin:local date:365d', set: 'ro-ms838-2025', by: 'facility', minN: 20, ...defaults.value };
// stored before only what differs was kept: a value equal to the default is not the user's own
for (const [pk, k] of [['drl.q', 'q'], ['drl.set', 'set'], ['drl.by', 'by'], ['drl.minN', 'minN']]) if (prefs.get(pk) === D[k]) prefs.set(pk, undefined);
// a link's filters (?q=&set=&by=&min_n=&group=) for this visit, then the user's own, then the defaults
const url = new URLSearchParams(location.search);
const state = {
  q: url.get('q') ?? prefs.get('drl.q') ?? D.q,
  set: url.get('set') || prefs.get('drl.set') || D.set,
  by: url.get('by') || prefs.get('drl.by') || D.by,
  minN: Number(url.get('min_n')) || prefs.get('drl.minN') || D.minN,
  estimated: false,
  showEmpty: false,
  group: url.get('group') || prefs.get('drl.group') || null,
};
// the view in the address bar, so a copied link opens the same table
function syncUrl() {
  const u = new URL(location.href);
  u.search = '';
  for (const [k, v] of [['q', state.q], ['set', state.set], ['by', state.by], ['min_n', state.minN], ['group', state.group]]) if (v != null && v !== '') u.searchParams.set(k, v);
  history.replaceState(null, '', u);
}
const GROUP_LABEL = { CT: 'CT', DX: 'DX/CR', RF: 'RF', XA: 'XA', MG: 'MG', NM: 'NM', PT: 'PT' };


function tag(status) {
  const [cls, label] = STATUS[status] || ['', status];
  return h('span', { class: `tag ${cls}` }, label);
}

function ratioBar(ratio) {
  if (ratio == null) return '';
  const w = Math.min(ratio / 2, 1) * 100;
  const cls = ratio > 1.5 ? 'alert' : ratio > 1 ? 'warn' : 'ok';
  return h('span', { class: 'drl-ratio', title: `${smart(ratio)} × DRL` },
    h('span', { class: `drl-ratio-fill ${cls}`, style: { width: `${w}%` } }),
    h('span', { class: 'drl-ratio-mark' }),
    h('span', { class: 'drl-ratio-num' }, `${num(ratio, 2)}×`));
}

async function render() {
  clear(page);
  let sets;
  try {
    sets = await drlSets();
  } catch (e) {
    page.append(h('p', { class: 'empty' }, e.message));
    return;
  }
  const setSel = h('select', { class: 'input', 'aria-label': 'Reference set', onchange: (e) => { state.set = e.target.value; keepOwn('drl.set', state.set, D.set); load(); } },
    sets.map((s) => h('option', { value: s.key, selected: s.key === state.set }, s.name)));
  const byBtns = ['facility', 'device'].map((b) => h('button', {
    type: 'button', 'aria-pressed': String(state.by === b), dataset: { by: b },
    onclick: () => { state.by = b; keepOwn('drl.by', b, D.by); byBtns.forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.by === b))); load(); },
  }, b === 'facility' ? 'Facility' : 'Per device'));
  const minN = h('input', { type: 'number', min: 1, max: 500, value: state.minN, class: 'input num-input', 'aria-label': 'Minimum n',
    onchange: (e) => { state.minN = Math.max(1, Number(e.target.value) || 20); keepOwn('drl.minN', state.minN, D.minN); load(); } });
  const empty = h('label', { class: 'chk' }, h('input', { type: 'checkbox', onchange: (e) => { state.showEmpty = e.target.checked; draw(); } }), ' show groups without data');
  const qb = h('dx-query-bar', { value: state.q });
  qb.check = (toks) => scopeWarnings(toks, null, [], null, { tabsOwn: true });
  qb.addEventListener('query-change', (e) => { state.q = e.detail; keepOwn('drl.q', state.q, D.q); load(); });

  const download = (kind) => () => {
    const href = drlDownloadUrl(kind, { q: state.q, set: state.set, min_n: state.minN });
    if (!href) { toast('Downloads need the server'); return; }
    h('a', { href, download: '' }).click();
    toast('Export started: the file downloads when the server has written it');
  };
  page.append(
    h('div', { class: 'toolbar' }, setSel, h('span', { class: 'seg' }, byBtns), h('label', { class: 'chk' }, 'min n ', minN),
      h('label', { class: 'chk', title: 'Weight bands (RP 185 body exams): also use weights estimated from the growth charts (median for age and sex)' },
        h('input', { type: 'checkbox', checked: state.estimated, onchange: (e) => { state.estimated = e.target.checked; load(); } }), ' estimated weights'), empty,
      h('span', { class: 'spacer' }),
      ...defaultsActions('drl', { admin: defaults.admin, prefKeys: ['drl.q', 'drl.set', 'drl.by', 'drl.minN'],
        current: () => ({ q: state.q, set: state.set, by: state.by, minN: state.minN }) }),
      h('button', { class: 'btn', type: 'button', title: 'Per room and facility: n, median, quartiles, DRL ratio (XLSX)', onclick: download('summary') }, 'ICRP 135 summary'),
      h('button', { class: 'btn', type: 'button', title: 'One row per study and DRL exam, pseudonymous, with a data dictionary (XLSX)', onclick: download('dataset') }, 'DRL dataset')),
    qb,
    h('nav', { id: 'drl-groups', class: 'tabs', 'aria-label': 'Modality' }),
    h('div', { id: 'drl-kpis', class: 'kpis drl-kpis' }),
    h('p', { id: 'drl-source', class: 'muted small' }),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid', id: 'drl-table' }))),
  );
  await load();
}

let last = null;

async function load() {
  syncUrl();
  const table = document.getElementById('drl-table');
  clear(table).append(h('tbody', {}, h('tr', {}, h('td', {}, 'Loading…'))));
  try {
    last = await drlCompare({ q: state.q, set: state.set, by: state.by, min_n: state.minN, estimated: state.estimated });
  } catch (e) {
    clear(table).append(h('tbody', {}, h('tr', {}, h('td', { class: 'error' }, e.message))));
    toast(e.message);
    return;
  }
  draw();
}

function draw() {
  if (!last) return;
  // one table per modality group: a DLP row never sits next to a DAP or MGD row
  const present = Object.keys(GROUP_LABEL).filter((k) => last.rows.some((r) => r.group === k));
  if (!present.includes(state.group)) {
    state.group = present.find((k) => last.rows.some((r) => r.group === k && r.groups.length)) || present[0] || null;
    syncUrl();
  }
  append(clear(document.getElementById('drl-groups')), present.map((k) => {
    const n = last.rows.filter((r) => r.group === k && r.groups.length).length;
    return h('button', { type: 'button', 'aria-current': k === state.group ? 'page' : false,
      onclick: () => { state.group = k; prefs.set('drl.group', k); syncUrl(); draw(); } }, `${GROUP_LABEL[k]} `, h('span', { class: 'muted' }, `(${n})`));
  }));
  const mine = last.rows.filter((r) => r.group === state.group);
  const rows = mine.filter((r) => state.showEmpty || r.groups.length);
  const counts = {};
  for (const r of mine) for (const g of r.groups) counts[g.status] = (counts[g.status] || 0) + 1;
  append(clear(document.getElementById('drl-kpis')), 
    ['well_above', 'above', 'below', 'insufficient'].map((s) => h('div', { class: 'kpi' },
      h('span', { class: 'code' }, STATUS[s][1]), h('span', { class: 'v' }, num(counts[s] || 0)),
      h('span', { class: 'm' }, state.by === 'device' ? 'device groups' : 'groups'))));
  clear(document.getElementById('drl-source')).append(`${last.name}. ${last.notes || ''} Source: ${last.source || '—'}`);

  const head = h('thead', {}, h('tr', {},
    ['Exam', 'Group', 'Quantity', 'DRL', state.by === 'device' ? 'Device' : '', 'n', 'P25', 'Median', 'P75', 'Median / DRL', 'Status']
      .map((c, i) => h('th', { class: i >= 5 && i <= 8 ? 'r' : '' }, c))));
  const body = h('tbody');
  for (const r of rows) {
    const groups = r.groups.length ? r.groups : [null];
    groups.forEach((g, i) => {
      body.append(h('tr', { class: g && g.status === 'well_above' ? 'hot' : '' },
        h('td', { title: r.exam }, i ? '' : r.exam_meaning),
        h('td', {}, i ? '' : (r.band_label || 'all')),
        h('td', {}, i ? '' : `${QUANTITY[r.quantity] || r.quantity} · ${r.applies_to}`),
        h('td', { class: 'r' }, i ? '' : `${shown(r.reference, r.unit)} ${shownUnit(r.unit).unit}`),
        h('td', {}, g && g.device_name ? g.device_name : ''),
        h('td', { class: 'r' }, g ? num(g.n) : '—'),
        h('td', { class: 'r' }, g && g.p25 != null ? shown(g.p25, r.unit) : ''),
        h('td', { class: 'r' }, g && g.median != null ? shown(g.median, r.unit) : ''),
        h('td', { class: 'r' }, g && g.p75 != null ? shown(g.p75, r.unit) : ''),
        h('td', {}, g ? ratioBar(g.ratio) : (r.reason || '')),
        h('td', {}, tag(g ? g.status : r.status))));
    });
  }
  if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 11, class: 'empty' }, `No ${GROUP_LABEL[state.group] || ''} exam of this reference set has data for the query.`)));
  clear(document.getElementById('drl-table')).append(head, body);
}

render();
