// Study list, per modality group: group tabs, saved-query tabs, typed query bar, server-paged grid and a preview pane.
// The query, sort and page live in the URL (?q=&sort=&dir=&page=), so views can be shared.

import '../components/dx-shell.js';
import '../components/dx-query-bar.js';
import '../components/dx-data-grid.js';
import { h, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, smart, dap, dapUnit, mmss, dateTime, patientSummary } from '../core/format.js';
import { studies, deviceMap, drl, studyHref, studyLabel, exportUrl, modalities, profileOf } from '../core/api.js';
import { prefs } from '../core/prefs.js';

const DEFAULT_Q = 'modality:CT date:90d age:>=18 origin:local -phantom';
const SAVED_KEY = 'savedQueries.ct';  // the key predates the other modalities; kept for users' saved queries
// One starter query, copied into the user's own saved queries the first time (nothing stored
// yet); from then on it is theirs: deleting it keeps it deleted.
const STARTER = { name: 'Adult CT, last 90 days', q: DEFAULT_Q };

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

const range = (a, b, d = 0) => (a == null ? '—' : a === b ? num(a, d) : `${num(a, d)}–${num(b, d)}`);
const diCell = (s) => {
  const out = range(s.di_min, s.di_max, 1);
  const bad = Math.max(Math.abs(s.di_min ?? 0), Math.abs(s.di_max ?? 0)) > 3;
  return bad ? h('span', { class: 'tag warn', title: '|DI| > 3: exposure outside the target' }, out) : out;
};
const agdMax = (s) => (s.agd_left == null && s.agd_right == null ? null : Math.max(s.agd_left ?? 0, s.agd_right ?? 0));
const dapUnitLabel = () => dapUnit();

const COMMON = [
  { key: 'flag', srLabel: 'Flag', sortable: false, width: '22px', render: flagCell },
  { key: 'date', label: 'Date', width: '118px', render: (s) => h('span', { class: 'mono' }, dateTime(s.date).slice(5)) },
  { key: 'device', label: 'Device', render: (s) => s.device_name },
  { key: 'accession', label: 'Accession', width: '140px', render: (s) => h('a', { class: `mono${s.accession ? '' : ' muted'}`, href: studyHref(s), title: s.accession ? null : 'No accession number from the device' }, studyLabel(s)) },
  { key: 'patient', label: 'Patient', width: '130px', render: (s) => h('span', { class: 'mono' }, patientSummary(s.patient)) },
  { key: 'description', label: 'Study description' },
];

// every per-modality column, by the study-row key it shows (profiles list which ones a group gets)
const CATALOGUE = {
  events: { label: 'Ev S/A/L', align: 'right', width: '80px', render: (s) => `${s.events.spiral}/${s.events.axial}/${s.events.localizer}` },
  ctdivol_max: { label: 'CTDIvol max', unit: 'mGy', align: 'right', width: '110px', hidden: true, render: (s) => smart(s.ctdivol_max) },
  dlp_total: { label: 'DLP', unit: 'mGy·cm', align: 'right', width: '110px', render: (s) => (s.dlp_total == null ? '—' : h('strong', {}, num(s.dlp_total))) },
  n_events: { label: 'Events', align: 'right', width: '70px', render: (s) => num(s.n_events) },
  kvp_max: { label: 'kV max', unit: 'kV', align: 'right', width: '80px', sortable: false, render: (s) => num(s.kvp_max) },
  ei_range: { label: 'EI', align: 'right', width: '100px', sortable: false, render: (s) => range(s.ei_min, s.ei_max) },
  di_range: { label: 'DI', align: 'right', width: '90px', sortable: false, render: diCell },
  dap_total: { label: 'DAP', unit: dapUnitLabel(), align: 'right', width: '110px', render: (s) => (s.dap_total == null ? '—' : h('strong', {}, dap(s.dap_total))) },
  fluoro_time: { label: 'Fluoro time', unit: 'm:s', align: 'right', width: '90px', render: (s) => (s.fluoro_time == null ? '—' : mmss(s.fluoro_time)) },
  fluoro_dap: { label: 'DAP fluoro', unit: dapUnitLabel(), align: 'right', width: '100px', hidden: true, render: (s) => (s.fluoro_dap == null ? '—' : dap(s.fluoro_dap)) },
  acq_dap: { label: 'DAP acq.', unit: dapUnitLabel(), align: 'right', width: '100px', hidden: true, render: (s) => (s.acq_dap == null ? '—' : dap(s.acq_dap)) },
  kar_total: { label: 'Ka,r', unit: 'mGy', align: 'right', width: '90px', render: (s) => (s.kar_total == null ? '—' : num(s.kar_total * 1000)) },
  psd: { label: 'PSD (est.)', unit: 'mGy', align: 'right', width: '90px', render: (s) => (s.psd == null ? '—' : num(s.psd)) },
  agd_left: { label: 'AGD L', unit: 'mGy', align: 'right', width: '80px', render: (s) => (s.agd_left == null ? '—' : num(s.agd_left, 2)) },
  agd_right: { label: 'AGD R', unit: 'mGy', align: 'right', width: '80px', render: (s) => (s.agd_right == null ? '—' : num(s.agd_right, 2)) },
  activity: { label: 'Activity', unit: 'MBq', align: 'right', width: '100px', render: (s) => (s.activity == null ? '—' : h('strong', {}, num(s.activity))) },
};

// a mixed list (no single modality): one "main dose" column, each row in its own quantity
function mainDose(s, profiles) {
  const p = profiles.find((x) => x.code === s.group);
  const key = p ? p.main : (s.dlp_total != null ? 'dlp_total' : 'dap_total');
  if (key === 'dlp_total' && s.dlp_total != null) return `${num(s.dlp_total)} mGy·cm`;
  if (key === 'dap_total' && s.dap_total != null) return `${dap(s.dap_total)} ${dapUnit()}`;
  if (key === 'agd_max' && agdMax(s) != null) return `${num(agdMax(s), 2)} mGy AGD`;
  if (key === 'activity' && s.activity != null) return `${num(s.activity)} MBq`;
  return '—';
}

function columnsFor(profile, profiles) {
  if (!profile) {
    return [...COMMON,
      { key: 'modality', label: 'Mod.', width: '60px', sortable: false, render: (s) => s.modality },
      { key: 'main', label: 'Dose', align: 'right', width: '140px', sortable: false, render: (s) => h('span', { class: 'mono' }, mainDose(s, profiles)) }];
  }
  return [...COMMON, ...profile.columns.filter((k) => CATALOGUE[k]).map((k) => ({ key: k, ...CATALOGUE[k] }))];
}

function savedQueries() {
  const saved = prefs.get(SAVED_KEY);
  if (saved) return saved;
  prefs.set(SAVED_KEY, [{ ...STARTER }]);
  return prefs.get(SAVED_KEY);
}

function tabs(currentQ, onPick) {
  const saved = savedQueries();
  const nav = h('nav', { class: 'tabs', 'aria-label': 'Saved queries' });
  const match = saved.find((s) => s.q === currentQ);
  for (const s of saved) {
    const tab = h('button', { type: 'button', 'aria-current': s === match ? 'page' : false, title: s.q, onclick: () => onPick(s.q) }, s.name);
    // every tab is the user's own (this browser only) and can be deleted
    nav.append(h('span', { class: 'tab-saved' }, tab,
      h('button', { type: 'button', class: 'tab-x', 'aria-label': `Delete “${s.name}”`, title: 'Delete this saved query', onclick: () => {
        if (!confirm(`Delete the saved query “${s.name}”?`)) return;
        prefs.set(SAVED_KEY, savedQueries().filter((x) => x.name !== s.name || x.q !== s.q));
        toast(`Deleted “${s.name}”`);
        onPick(currentQ);
      } }, '×')));
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

function preview(s, profile, profiles) {
  if (!s) return h('p', { class: 'muted', style: { padding: '12px' } }, 'Select a study.');
  const flags = s.flags.map((f) => h('span', { class: `tag ${f.level}` }, f.text));
  const kv = [
    ['Accession', s.accession || '—', true], ['Study', s.description], ['Protocol', s.protocol],
    ['Device', s.device_name], ['Patient', `${patientSummary(s.patient)} · ${s.patient.pseudo_id}`, true],
  ];
  const p = profile || profiles.find((x) => x.code === s.group);
  for (const k of p ? p.columns : ['n_events']) {
    const c = CATALOGUE[k];
    if (!c) continue;
    const v = c.render(s);
    kv.push([c.unit ? `${c.label} (${c.unit})` : c.label, typeof v === 'string' ? v : v.textContent, true]);
  }
  return h('div', {},
    h('div', { class: 'row', style: { padding: '12px 12px 0' } }, h('a', { class: 'btn xs', href: studyHref(s) }, `Open ${studyLabel(s)}`)),
    h('dl', { class: 'kv' }, kv.flatMap(([k, v, mono]) => [h('dt', {}, k), h('dd', mono ? { class: 'mono' } : {}, v ?? '—')])),
    flags.length ? h('div', { class: 'row', style: { padding: '0 12px 12px' } }, flags) : null);
}

function groupTabs(profiles, current, onPick) {
  const nav = h('nav', { class: 'tabs', 'aria-label': 'Modality' });
  const mk = (label, code, q) => h('button', { type: 'button', 'aria-current': current === code ? 'page' : false, onclick: () => onPick(q) }, label);
  nav.append(mk('All', null, 'date:90d origin:local -phantom'));
  for (const p of profiles) nav.append(mk(p.label, p.code, `modality:${p.modalities.join(',')} date:90d origin:local -phantom`));
  return nav;
}

async function init() {
  const [devs, drlSet, profiles] = await Promise.all([deviceMap(), drl(), modalities()]);
  const physical = new Map([...devs.entries()].filter(([id, d]) => id === d.id));
  let currentQ = initial.q;

  const qb = h('dx-query-bar', { value: currentQ });
  qb.context = { devices: physical, drl: drlSet.ct_dlp_mgycm };
  const tabsHost = h('div');
  const previewBody = h('div');
  let profile = profileOf(profiles, currentQ);
  const gridKey = (p) => `grid.${p ? p.code.toLowerCase() : 'all'}`;
  const gridLabel = (p) => (p ? `${p.label} studies` : 'Studies');
  const grid = h('dx-data-grid', { label: gridLabel(profile), noun: t('studies.count'), 'storage-key': gridKey(profile) },
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows('csv') }, 'CSV'),
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows('xlsx') }, 'XLSX'),
    h('a', { slot: 'actions', class: 'btn primary', href: '#', onclick: (e) => { e.currentTarget.href = `charts.html?q=${encodeURIComponent(currentQ)}`; } }, t('studies.chart')));

  const pick = (q) => { qb.value = q; apply(q); };
  const renderTabs = () => tabsHost.replaceChildren(groupTabs(profiles, profile?.code ?? null, pick), tabs(currentQ, pick));
  // columns, their hidden set and the title follow the modality the query is about
  const setProfile = (p) => {
    profile = p;
    const cols = columnsFor(p, profiles);
    const key = gridKey(p);
    if (!prefs.get(`${key}.hidden`)) prefs.set(`${key}.hidden`, cols.filter((c) => c.hidden).map((c) => c.key));
    if (grid.storageId) { grid.storageId = key; grid.hiddenCols = new Set(prefs.get(`${key}.hidden`) || []); }
    grid.setAttribute('storage-key', key);
    grid.setAttribute('label', gridLabel(p));
    grid.table?.setAttribute('aria-label', gridLabel(p));
    grid.columns = cols;
    document.title = `${gridLabel(p)} · Dozimetrix`;
    document.querySelector('dx-shell')?.setSub(p?.label ?? null);  // the crumb follows the query
    if (!cols.some((c) => c.key === grid.state?.sort) && grid.state && grid.state.sort !== 'date') grid.setState({ sort: 'date', dir: 'desc' });
  };
  const apply = (q) => {
    currentQ = q;
    const p = profileOf(profiles, q);
    if (p !== profile) setProfile(p);
    grid.setState({ page: 1 });
    renderTabs();
    grid.reload();
  };

  setProfile(profile);
  grid.rowId = (s) => s.id ?? s.accession;
  grid.setState({ sort: initial.sort, dir: initial.dir, page: initial.page, size: 50 });
  grid.fetcher = (state) => studies({ q: currentQ, ...state }).then((res) => {
    res.rows.forEach((r) => { r._rowClass = r.flags.some((f) => f.level === 'alert') ? 'hot' : ''; });
    return res;
  });
  grid.addEventListener('state-change', (e) => syncUrl(e.detail, currentQ));
  grid.addEventListener('row-focus', (e) => previewBody.replaceChildren(preview(e.detail, profile, profiles)));
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
    h('a', { href, download: 'studies.csv' }).click();
    toast(`Exported ${num(res.rows.length)} rows`);
  }

  page.replaceChildren(
    tabsHost,
    qb,
    h('div', { class: 'split' }, grid,
      h('aside', { class: 'panel preview', 'aria-labelledby': 'h-prev' },
        h('div', { class: 'panel-head' }, h('h2', { id: 'h-prev' }, t('studies.preview'))),
        previewBody)));
  renderTabs();
  grid.reload();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
