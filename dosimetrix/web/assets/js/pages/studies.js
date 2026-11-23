// Study list (CT): saved-query tabs, typed query bar, server-paged grid and a preview pane.
// The query, sort and page live in the URL (?q=&sort=&dir=&page=), so views can be shared.

import '../components/dt-shell.js';
import '../components/dt-query-bar.js';
import '../components/dt-data-grid.js';
import { h, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, smart, dateTime, patientSummary } from '../core/format.js';
import { studies, deviceMap, drl, studyHref, exportUrl } from '../core/api.js';
import { prefs } from '../core/prefs.js';

const DEFAULT_Q = 'modality:CT date:90d age:>=18 origin:local -phantom';
const SAVED_KEY = 'savedQueries.ct';
const BUILTIN = [
  { name: 'Adult CT, last 90 days', q: DEFAULT_Q },
  { name: 'Above DRL, last 30 days', q: 'modality:CT date:30d flag:drl -phantom' },
  { name: 'Head CT, emergency', q: 'modality:CT desc:head device:"CT 1 – Emergency" -phantom' },
];

const page = document.getElementById('page');
const url = new URL(location.href);
const initial = {
  q: url.searchParams.get('q') ?? DEFAULT_Q,
  sort: url.searchParams.get('sort') || 'date',
  dir: url.searchParams.get('dir') || 'desc',
  page: Number(url.searchParams.get('page')) || 1,
};

function syncUrl(state, q) {
  const u = new URL(location.href);
  u.search = '';
  if (q) u.searchParams.set('q', q);
  if (state.sort !== 'date' || state.dir !== 'desc') { u.searchParams.set('sort', state.sort); u.searchParams.set('dir', state.dir); }
  if (state.page > 1) u.searchParams.set('page', state.page);
  history.replaceState(null, '', u);
}

function flagCell(s) {
  const f = s.flags.find((x) => x.level === 'alert') || s.flags[0];
  return f ? h('span', { class: `flagbar ${f.level}`, title: f.text, role: 'img', 'aria-label': f.text }) : '';
}

const COLUMNS = [
  { key: 'flag', srLabel: 'Flag', sortable: false, width: '22px', render: flagCell },
  { key: 'date', label: 'Date', width: '118px', render: (s) => h('span', { class: 'mono' }, dateTime(s.date).slice(5)) },
  { key: 'device', label: 'Device', render: (s) => s.device_name },
  { key: 'accession', label: 'Accession', width: '140px', render: (s) => h('a', { class: 'mono', href: studyHref(s) }, s.accession) },
  { key: 'patient', label: 'Patient', width: '130px', render: (s) => h('span', { class: 'mono' }, patientSummary(s.patient)) },
  { key: 'description', label: 'Study description' },
  { key: 'events', label: 'Ev S/A/L', align: 'right', width: '80px', render: (s) => `${s.events.spiral}/${s.events.axial}/${s.events.localizer}` },
  { key: 'ctdivol_max', label: 'CTDIvol max', unit: 'mGy', align: 'right', width: '110px', hidden: true, render: (s) => smart(s.ctdivol_max) },
  { key: 'dlp_total', label: 'DLP', unit: 'mGy·cm', align: 'right', width: '110px', render: (s) => h('strong', {}, num(s.dlp_total)) },
];

function savedQueries() {
  return prefs.get(SAVED_KEY) || [];
}

function tabs(currentQ, onPick) {
  const all = [...BUILTIN, ...savedQueries()];
  const nav = h('nav', { class: 'tabs', 'aria-label': 'Saved queries' });
  const match = all.find((s) => s.q === currentQ);
  for (const s of all) {
    nav.append(h('button', { type: 'button', 'aria-current': s === match ? 'page' : false, title: s.q, onclick: () => onPick(s.q) }, s.name));
  }
  nav.append(h('button', { type: 'button', onclick: () => {
    const name = prompt('Name for this query', '');
    if (!name) return;
    prefs.set(SAVED_KEY, [...savedQueries(), { name, q: currentQ }]);
    toast(`Saved “${name}” (this browser only)`);
    onPick(currentQ);
  } }, match ? '+ Save as…' : '● Unsaved · Save…'));
  return nav;
}

function preview(s) {
  if (!s) return h('p', { class: 'muted', style: { padding: '12px' } }, 'Select a study.');
  const ev = s.events;
  const flags = s.flags.map((f) => h('span', { class: `tag ${f.level}` }, f.text));
  return h('div', {},
    h('dl', { class: 'kv' },
      h('dt', {}, 'Accession'), h('dd', { class: 'mono' }, s.accession),
      h('dt', {}, 'Study'), h('dd', {}, s.description),
      h('dt', {}, 'Protocol'), h('dd', {}, s.protocol),
      h('dt', {}, 'Device'), h('dd', {}, s.device_name),
      h('dt', {}, 'Patient'), h('dd', { class: 'mono' }, `${patientSummary(s.patient)} · ${s.patient.pseudo_id}`),
      h('dt', {}, 'Events'), h('dd', { class: 'mono' }, `${ev.spiral} spiral · ${ev.axial} axial · ${ev.localizer} loc.`),
      h('dt', {}, 'CTDIvol max'), h('dd', { class: 'mono' }, `${smart(s.ctdivol_max)} mGy`),
      h('dt', {}, 'Total DLP'), h('dd', { class: 'mono', style: { 'font-weight': '700', color: s.flags.length ? 'var(--warn)' : 'inherit' } }, `${num(s.dlp_total)} mGy·cm`)),
    flags.length ? h('div', { class: 'row', style: { padding: '0 12px 12px' } }, flags) : null);
}

async function init() {
  const [devs, drlSet] = await Promise.all([deviceMap(), drl()]);
  const physical = new Map([...devs.entries()].filter(([id, d]) => id === d.id));
  let currentQ = initial.q;

  const qb = h('dt-query-bar', { value: currentQ });
  qb.context = { devices: physical, drl: drlSet.ct_dlp_mgycm };
  const tabsHost = h('div');
  const previewBody = h('div');
  const grid = h('dt-data-grid', { label: 'CT studies', noun: t('studies.count'), 'storage-key': 'grid.ct' },
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows('csv') }, 'CSV'),
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows('xlsx') }, 'XLSX'),
    h('a', { slot: 'actions', class: 'btn primary', href: '#', onclick: (e) => { e.currentTarget.href = `charts.html?q=${encodeURIComponent(currentQ)}`; } }, t('studies.chart')));

  const renderTabs = () => tabsHost.replaceChildren(tabs(currentQ, (q) => { qb.value = q; apply(q); }));
  const apply = (q) => {
    currentQ = q;
    grid.setState({ page: 1 });
    renderTabs();
    grid.reload();
  };

  grid.columns = COLUMNS;
  grid.rowId = (s) => s.id ?? s.accession;
  // first visit: CTDIvol column hidden by default
  if (!prefs.get('grid.ct.hidden')) prefs.set('grid.ct.hidden', COLUMNS.filter((c) => c.hidden).map((c) => c.key));
  grid.setState({ sort: initial.sort, dir: initial.dir, page: initial.page, size: 50 });
  grid.fetcher = (state) => studies({ q: currentQ, ...state }).then((res) => {
    res.rows.forEach((r) => { r._rowClass = r.flags.some((f) => f.level === 'alert') ? 'hot' : ''; });
    return res;
  });
  grid.addEventListener('state-change', (e) => syncUrl(e.detail, currentQ));
  grid.addEventListener('row-focus', (e) => previewBody.replaceChildren(preview(e.detail)));
  grid.addEventListener('row-open', (e) => {
    if (e.detail.id != null) location.href = studyHref(e.detail);
    else toast(`Full detail for ${e.detail.accession}: only the XA sample study has a detail page in sample mode.`);
  });
  qb.addEventListener('query-change', (e) => apply(e.detail));

  async function exportRows(kind) {
    const server = exportUrl({ q: currentQ, format: kind });
    if (server) {  // the server writes every row (CSV or XLSX); the browser just downloads
      h('a', { href: server, download: '' }).click();
      toast('Export started: the file downloads when the server has written it');
      return;
    }
    const res = await studies({ q: currentQ, sort: grid.state.sort, dir: grid.state.dir, page: 1, size: 100000 });
    if (kind === 'xlsx') { toast('XLSX export is produced by the server; sample mode offers CSV.'); return; }
    const head = ['date', 'device', 'accession', 'age', 'sex', 'weight_kg', 'description', 'protocol', 'spiral', 'axial', 'localizer', 'ctdivol_max_mgy', 'dlp_total_mgycm'];
    const lines = [head.join(',')].concat(res.rows.map((s) => [s.date, s.device_name, s.accession, s.patient.age ?? '', s.patient.sex, s.patient.weight ?? '',
      s.description, s.protocol, s.events.spiral, s.events.axial, s.events.localizer, s.ctdivol_max, s.dlp_total]
      .map((v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v)).join(',')));
    const href = URL.createObjectURL(new Blob(['﻿', lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }));
    h('a', { href, download: 'ct-studies.csv' }).click();
    toast(`Exported ${num(res.rows.length)} rows`);
  }

  page.replaceChildren(
    tabsHost,
    qb,
    h('p', { class: 'muted small' }, t('studies.query.hint')),
    h('div', { class: 'split' }, grid,
      h('aside', { class: 'panel preview', 'aria-labelledby': 'h-prev' },
        h('div', { class: 'panel-head' }, h('h2', { id: 'h-prev' }, t('studies.preview'))),
        previewBody)));
  renderTabs();
  grid.reload();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
