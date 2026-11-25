// Charts workspace, every modality: each browser panel charts one modality group. One query is shared by every panel. Panels can be added, re-ordered
// (drag the grip, or ← →), resized (S/M/L/XL), re-configured inline and exported (PNG / CSV).
// Each panel has a summary table (n, median, P75, IQR, P75/DRL). A user's workspaces are kept on
// the server per user; admins also publish shared ones, which everyone sees and only admins change.

import { confirmDialog } from '../core/confirm.js';
import '../components/dx-shell.js';
import '../components/dx-query-bar.js';
import '../components/dx-chart.js';
import { baseOption } from '../components/dx-chart.js';
import { h, clear, toast, esc } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, smart, dapUnit, DAP_UNITS } from '../core/format.js';
import { groupRows, physicalDevices, drl, isSample, getPref, putPref, me, sharedViews, createView, updateView, deleteView } from '../core/api.js';
import { summary, groupBy, histogram } from '../core/stats.js';
import { prefs } from '../core/prefs.js';
import { SERVER_TYPES, buildServer, serverOptions, serverSummary, defaultsFor, setDevices } from './charts-server.js';

const page = document.getElementById('page');

// every browser-panel metric (field names are unique across groups); GROUPS says which a group offers
const METRICS = {
  ctdivol: { label: 'CTDIvol', unit: 'mGy' }, dlp: { label: 'DLP', unit: 'mGy·cm' },
  dap: { label: 'DAP per exposure', get unit() { return dapUnit(); } }, kvp: { label: 'kVp', unit: 'kV' }, mas: { label: 'mAs', unit: 'mA·s' },
  ei: { label: 'Exposure index', unit: '' }, di: { label: 'Deviation index', unit: '' },
  agd: { label: 'AGD per view', unit: 'mGy' }, thickness: { label: 'Compressed thickness', unit: 'mm' },
  dap_total: { label: 'DAP total', get unit() { return dapUnit(); } }, kar_total: { label: 'Ka,r total', unit: 'mGy' },
  fluoro_time: { label: 'Fluoroscopy time', unit: 's' }, psd: { label: 'Peak skin dose', unit: 'mGy' },
  activity: { label: 'Administered activity', unit: 'MBq' }, activity_kg: { label: 'Activity per kg', unit: 'MBq/kg' },
};
for (const [k, m] of Object.entries(METRICS)) m.field = k;
// rows arrive with DAP in Gy·cm² (stored unit); shown in the user's DAP unit, DRLs included
const DAP_FIELDS = ['dap', 'dap_total'];
function toShown(r) {
  const f = DAP_UNITS[dapUnit()].factor;
  const out = { ...r, drl: r.drl ? { ...r.drl } : r.drl };
  for (const k of DAP_FIELDS) {
    if (typeof out[k] === 'number') out[k] *= f;
    if (out.drl && typeof out.drl[k] === 'number') out.drl[k] *= f;
  }
  return out;
}
// per group: metrics, scatter axes (x: size of the patient, y: dose), what one row is
const GROUPS = {
  CT: { label: 'CT', metrics: ['ctdivol', 'dlp'], scatter: ['weight', 'dlp'], unit: 'acquisitions' },
  DX: { label: 'DX/CR', metrics: ['dap', 'kvp', 'mas', 'ei', 'di'], scatter: ['weight', 'dap'], unit: 'exposures' },
  RF: { label: 'RF', metrics: ['dap_total', 'kar_total', 'fluoro_time'], scatter: ['weight', 'dap_total'], unit: 'studies' },
  XA: { label: 'XA', metrics: ['dap_total', 'kar_total', 'fluoro_time', 'psd'], scatter: ['weight', 'dap_total'], unit: 'studies' },
  MG: { label: 'MG', metrics: ['agd', 'thickness', 'kvp', 'mas'], scatter: ['thickness', 'agd'], unit: 'views' },
  NM: { label: 'NM', metrics: ['activity', 'activity_kg'], scatter: ['weight', 'activity'], unit: 'studies' },
  PT: { label: 'PET', metrics: ['activity', 'activity_kg'], scatter: ['weight', 'activity'], unit: 'studies' },
};
const AXIS = { weight: ['weight', 'kg'], thickness: ['compressed thickness', 'mm'] };
const groupOf = (cfg) => (cfg.group in GROUPS ? cfg.group : 'CT');
const metricOf = (cfg) => (GROUPS[groupOf(cfg)].metrics.includes(cfg.metric) ? cfg.metric : GROUPS[groupOf(cfg)].metrics[0]);
const TYPES = { dots: 'Dots vs DRL', box: 'Boxplot', bar: 'Median / mean bar', hist: 'Histogram', trend: 'Trend', scatter: 'Dose vs size', heat: 'Workload' };
const SPANS = { S: 4, M: 6, L: 8, XL: 12 };
const PERIODS = { week: 'Week', month: 'Month', quarter: 'Quarter', year: 'Year' };
const DEFAULT_Q = 'date:2026-07-01..2026-11-21 origin:local';
const DEFAULT_PANELS = [
  { type: 'dots', metric: 'ctdivol', span: 'L' },
  { type: 'dots', group: 'DX', metric: 'dap', span: 'L' },
  { type: 'box', metric: 'dlp', span: 'S', sort: 'n' },
  { type: 'trend', metric: 'ctdivol', span: 'M', period: 'month', stat: 'median' },
  { type: 'scatter', metric: 'dlp', span: 'M' },
  { type: 'hist', metric: 'dlp', span: 'M' },
  { type: 'heat', metric: 'ctdivol', span: 'M' },
  { ...defaultsFor('sdrl'), span: 'L' },
  { ...defaultsFor('sbox'), modality: 'DX', metric: 'dap', by: 'exam', series: 'age_band', system: 'ro-drl-ms838-2025', span: 'L' },
];

// One ready-made audit per modality (tabs, like the Studies list): the charts that suit it.
// Each is the user's own once changed (saved like any workspace); Reset brings these back.
const srv = (type, modality, metric, extra = {}) => ({ ...defaultsFor(type), modality, metric, ...extra });
const MOD_PANELS = {
  CT: DEFAULT_PANELS.filter((p) => (p.group || 'CT') === 'CT' && p.modality !== 'DX'),
  DX: [
    { type: 'dots', group: 'DX', metric: 'dap', span: 'L' },
    srv('sbox', 'DX,CR', 'dap', { by: 'exam', series: 'age_band', system: 'ro-drl-ms838-2025', span: 'L' }),
    { type: 'box', group: 'DX', metric: 'dap', span: 'M', sort: 'n' },
    { type: 'trend', group: 'DX', metric: 'dap', span: 'M', period: 'month', stat: 'median' },
    { type: 'hist', group: 'DX', metric: 'di', span: 'M' },
    { type: 'box', group: 'DX', metric: 'ei', span: 'M', sort: 'n' },
    { type: 'scatter', group: 'DX', span: 'M' },
    { type: 'heat', group: 'DX', span: 'M' },
    srv('sdrl', 'DX,CR', 'dap', { span: 'L' }),
  ],
  RF: [
    { type: 'dots', group: 'RF', metric: 'dap_total', span: 'L' },
    { type: 'box', group: 'RF', metric: 'dap_total', span: 'M', sort: 'n' },
    { type: 'box', group: 'RF', metric: 'fluoro_time', span: 'M', sort: 'n' },
    { type: 'trend', group: 'RF', metric: 'kar_total', span: 'M', period: 'month', stat: 'median' },
    srv('sbox', 'RF', 'dap_total', { by: 'exam', series: 'age_band', span: 'M' }),
    { type: 'scatter', group: 'RF', span: 'M' },
    { type: 'heat', group: 'RF', span: 'M' },
    srv('sdrl', 'RF', 'dap_total', { span: 'L' }),
  ],
  XA: [
    { type: 'box', group: 'XA', metric: 'dap_total', span: 'M', sort: 'n' },
    { type: 'box', group: 'XA', metric: 'kar_total', span: 'M', sort: 'n' },
    { type: 'hist', group: 'XA', metric: 'psd', span: 'M' },
    { type: 'trend', group: 'XA', metric: 'fluoro_time', span: 'M', period: 'month', stat: 'median' },
    srv('sbox', 'XA', 'kar_total', { by: 'device', span: 'M' }),
    { type: 'heat', group: 'XA', span: 'M' },
  ],
  MG: [
    { type: 'box', group: 'MG', metric: 'agd', span: 'M', sort: 'n' },
    { type: 'scatter', group: 'MG', span: 'M' },
    { type: 'hist', group: 'MG', metric: 'thickness', span: 'M' },
    { type: 'trend', group: 'MG', metric: 'agd', span: 'M', period: 'month', stat: 'median' },
    srv('sbox', 'MG', 'agd_breast', { by: 'device', span: 'M' }),
    { type: 'heat', group: 'MG', span: 'M' },
    srv('sdrl', 'MG', 'agd', { span: 'L' }),
  ],
  NM: [
    { type: 'dots', group: 'NM', metric: 'activity', span: 'L' },
    { type: 'box', group: 'NM', metric: 'activity', span: 'M', sort: 'n' },
    { type: 'box', group: 'NM', metric: 'activity_kg', span: 'M', sort: 'n' },
    { type: 'scatter', group: 'NM', span: 'M' },
    { type: 'trend', group: 'NM', metric: 'activity', span: 'M', period: 'month', stat: 'median' },
    srv('sdrl', 'NM', 'activity', { span: 'L' }),
  ],
  PT: [
    { type: 'dots', group: 'PT', metric: 'activity', span: 'L' },
    { type: 'box', group: 'PT', metric: 'activity', span: 'M', sort: 'n' },
    { type: 'box', group: 'PT', metric: 'activity_kg', span: 'M', sort: 'n' },
    { type: 'scatter', group: 'PT', span: 'M' },
    { type: 'trend', group: 'PT', metric: 'activity', span: 'M', period: 'month', stat: 'median' },
    srv('sdrl', 'PT', 'activity', { span: 'L' }),
  ],
};
const MOD_Q = 'date:90d origin:local';
const isMod = (name) => /^mod-[A-Z]+$/.test(name || '');
const modOf = (name) => name.slice(4);

Object.assign(TYPES, SERVER_TYPES);
let ws;         // { name, q, panels }
let rows = [];  // rows of the panel being drawn (one modality group)
const rowsBy = {};  // group → rows for the current query
let devices = [];
let drlSet;

function wsKey(name) { return `workspace.${name}`; }
const DEFAULT_WS = 'My audit · CT 2026 H2';
function wsNames() { const n = prefs.get('workspaces'); return n?.length ? n : [DEFAULT_WS]; }
function defaultsOf(name) {
  return isMod(name) ? { q: MOD_Q, panels: structuredClone(MOD_PANELS[modOf(name)] || []) }
    : { q: DEFAULT_Q, panels: structuredClone(DEFAULT_PANELS) };
}
// Shared workspaces (published by an admin) are named "shared:<id>" in the select and in
// workspace.current; their layout lives in the view, not in the user's preferences.
let shared = [];       // [{ id, name, value: { q, panels } }]
let isAdmin = false;
let dirtyEl = null;    // shown when a non-admin changed a shared workspace (the change is not kept)
const sharedOf = (key) => shared.find((v) => `shared:${v.id}` === key);
function loadWs(name) {
  const view = sharedOf(name);
  if (view) return { name: view.name, sharedId: view.id, q: view.value?.q ?? DEFAULT_Q, panels: structuredClone(view.value?.panels || []) };
  const saved = prefs.get(wsKey(name));
  return saved ? structuredClone(saved) : { name, ...defaultsOf(name) };
}
let flushShared = async () => {};
let sharedTimer;
function saveWs() {
  if (ws.sharedId == null) { prefs.set(wsKey(ws.name), ws); return; }
  if (!isAdmin) { if (dirtyEl) dirtyEl.hidden = false; return; }
  // an admin's change to a shared workspace is published (debounced, audited on the server)
  clearTimeout(sharedTimer);
  flushShared = () => { clearTimeout(sharedTimer); flushShared = async () => {}; return updateView(ws.sharedId, { value: { q: ws.q, panels: ws.panels } }); };
  sharedTimer = setTimeout(() => flushShared().catch((e) => toast(`Shared workspace not saved: ${e.message}`)), 800);
}

/* ---------------- data helpers ---------------- */

function deviceIndex(id) { return devices.findIndex((d) => d.id === id); }
function devColor(th, id) { return th.devices[deviceIndex(id) % 8]; }
function drlFor(metric, protocol) { return metric === 'ctdivol' ? drlSet.ct_ctdivol_mgy?.[protocol] : metric === 'dlp' ? drlSet.ct_dlp_mgycm?.[protocol] : undefined; }
/** The DRL that applies to one acquisition. The API sends it per row (exam + patient age or
 *  weight group, national OMS 838/2025 or RP 185); sample data has one value per protocol. */
function refOf(r, metric) { return r.drl ? r.drl[metric] ?? undefined : drlFor(metric, r.protocol); }
/** One reference per protocol for labels and markers: the median of its rows' references. */
function protoRef(metric, protocol) {
  const refs = rows.filter((r) => r.protocol === protocol).map((r) => refOf(r, metric)).filter((v) => v != null);
  return refs.length ? summary(refs).median : undefined;
}
/** Which set the references come from ("OMS 838", "RP 185", or both): the API names it per
 *  row (DLP: national, else RP 185; CTDIvol: RP 185 only); sample data names none. */
function refSources(metric, protocols) {
  const keep = protocols ? new Set([].concat(protocols)) : null;
  const short = (s) => (s.startsWith('OMS 838') ? 'OMS 838' : s.startsWith('RP 185') ? 'RP 185' : s.split(' ')[0]);
  const found = new Set(rows.filter((r) => !keep || keep.has(r.protocol)).map((r) => r.drl?.source?.[metric]).filter(Boolean).map(short));
  return [...found].sort().join(' + ');
}
function drlLabel(metric, protocols) { const src = refSources(metric, protocols); return src ? `DRL ${src}` : 'DRL'; }
function rowSource(r, metric) { return r.drl?.source?.[metric] || ''; }
function refRange(metric, protocol) {
  const refs = [...new Set(rows.filter((r) => r.protocol === protocol).map((r) => refOf(r, metric)).filter((v) => v != null))].sort((a, b) => a - b);
  if (!refs.length) return '—';
  return refs.length === 1 ? num(refs[0]) : `${num(refs[0])}–${num(refs[refs.length - 1])}`;
}

// a saved protocol that is not in the current data (e.g. from the sample data, or a
// changed query) falls back to the most frequent protocol that has data
function protoFor(cfg, metric, need = () => true) {
  const ok = new Set(rows.filter(need).map((r) => r.protocol));
  if (cfg.protocol && ok.has(cfg.protocol)) return cfg.protocol;
  return protocolsBy('n', metric).find((p) => ok.has(p)) || protocolsBy('n', metric)[0];
}

const cmp = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
/** Anatomy, then projection, then the size band by its numbers ("5-15 kg" before "15-30 kg"; no band last).
 *  Rows that are not grouped by anatomy (the raw protocol names) compare equal: those sort by name. */
function natural(a, b) {
  if (!a.anatomy || !b.anatomy) return 0;
  const lo = (r) => r.band_lo ?? Infinity;
  const hi = (r) => r.band_hi ?? Infinity;
  return a.anatomy.localeCompare(b.anatomy) || a.projection.localeCompare(b.projection) || cmp(a.band_kind, b.band_kind) || cmp(lo(a), lo(b)) || cmp(hi(a), hi(b));
}

function protocolsBy(sort, metric) {
  const g = groupBy(rows, (r) => r.protocol);
  const list = [...g.entries()].map(([p, rs]) => ({ p, n: rs.length, med: summary(rs.map((r) => r[METRICS[metric].field])).median }));
  if (sort === 'name') list.sort((a, b) => natural(g.get(a.p)[0], g.get(b.p)[0]) || a.p.localeCompare(b.p));
  else if (sort === 'value') list.sort((a, b) => b.med / (protoRef(metric, b.p) || 1) - a.med / (protoRef(metric, a.p) || 1));
  else list.sort((a, b) => b.n - a.n);
  return list.map((x) => x.p);
}

const TOPS = { all: 'All', 5: '5', 10: '10', 12: '12', 20: '20', 30: '30', 50: '50' };
const DEFAULT_TOP = 10;  // a panel starts with the 10 busiest protocols, never with all of them
/** The protocols a panel charts: the first `top` of the list in the chosen order (sorted by name,
 *  the busiest `top` protocols, listed alphabetically). The summary table keeps every protocol. */
function limitTop(cfg, list, dflt) {
  const top = cfg.top === 'all' ? Infinity : Number(cfg.top) || dflt;
  if (list.length <= top) return list;
  if ((cfg.sort || 'n') !== 'name') return list.slice(0, top);
  const busiest = new Set(protocolsBy('n', cfg.metric).filter((p) => list.includes(p)).slice(0, top));
  return list.filter((p) => busiest.has(p));
}

function summaryRows(metric, protocolFilter) {
  const out = [];
  const g = groupBy(rows.filter((r) => !protocolFilter || r.protocol === protocolFilter), (r) => r.protocol, (r) => r.device_id);
  for (const p of protocolsBy('n', metric).filter((x) => g.has(x))) {
    for (const [dev, rs] of g.get(p)) {
      const s = summary(rs.map((r) => r[METRICS[metric].field]));
      const ref = protoRef(metric, p);
      out.push({ protocol: p, device_id: dev, device: rs[0].device, ...s, drl: ref, ratio: ref ? s.p75 / ref : null });
    }
  }
  return out;
}

function period(iso, per) {
  const y = iso.slice(0, 4);
  const m = Number(iso.slice(5, 7));
  if (per === 'year') return y;
  if (per === 'quarter') return `${y}-Q${Math.ceil(m / 3)}`;
  if (per === 'week') {
    // ISO 8601 week: the week belongs to the year of its Thursday
    const d = new Date(`${iso.slice(0, 10)}T12:00Z`);
    d.setUTCDate(d.getUTCDate() + 3 - ((d.getUTCDay() + 6) % 7));
    const week = 1 + Math.round((d - Date.UTC(d.getUTCFullYear(), 0, 4)) / 604800000 - 3 / 7 + ((new Date(Date.UTC(d.getUTCFullYear(), 0, 4)).getUTCDay() + 6) % 7) / 7);
    return `${d.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
  }
  return iso.slice(0, 7);
}

/* ---------------- chart builders: return (theme) => option, plus csv ---------------- */

function buildDots(cfg) {
  const metric = METRICS[cfg.metric];
  if (!rows.some((r) => refOf(r, cfg.metric))) return null;  // no per-row DRL: the server 'Median vs DRL' panel covers it
  const withRef = rows.filter((r) => refOf(r, cfg.metric));
  const allProtos = protocolsBy(cfg.sort || 'n', cfg.metric).filter((p) => withRef.some((r) => r.protocol === p));
  const protos = limitTop(cfg, allProtos, DEFAULT_TOP);  // the summary table has every protocol
  const devIds = [...new Set(rows.map((r) => r.device_id))].sort((a, b) => a - b);
  const csv = [['protocol', 'device', `${metric.label} ${metric.unit}`, 'percent_of_DRL']];
  const option = (th) => {
    const base = baseOption(th);
    let seed = 7;
    const rnd = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
    const series = [];
    devIds.forEach((dev, k) => {
      const pts = [];
      const meds = [];
      protos.forEach((p, i) => {
        const rs = withRef.filter((r) => r.protocol === p && r.device_id === dev);
        if (!rs.length) return;
        const y = i + (k - (devIds.length - 1) / 2) * 0.26;
        const pct = rs.map((r) => (r[metric.field] / refOf(r, cfg.metric)) * 100);
        rs.forEach((r, j) => pts.push([pct[j], y + (rnd() - 0.5) * 0.16, r[metric.field], rowSource(r, cfg.metric)]));
        meds.push([summary(pct).median, y]);
      });
      const name = devices[deviceIndex(dev)]?.display_name || `Device ${dev}`;
      series.push({ name, type: 'scatter', data: pts, symbolSize: 4, itemStyle: { color: devColor(th, dev), opacity: 0.45 }, large: true });
      series.push({ name, type: 'scatter', data: meds, symbol: 'rect', symbolSize: [3, 18], itemStyle: { color: devColor(th, dev) }, z: 5,
        tooltip: { formatter: (p) => `${esc(name)} · median ${num(p.value[0])}% of ${esc(drlLabel(cfg.metric, protos[Math.round(p.value[1])]))}` } });
    });
    series[0].markLine = { silent: true, symbol: 'none', data: [{ xAxis: 100 }], lineStyle: { color: th.ref, type: 'dashed', width: 2 },
      label: { formatter: drlLabel(cfg.metric, protos), position: 'start', color: th.ref, fontFamily: th.mono } };
    return {
      ...base,
      legend: { ...base.legend, data: devIds.map((d) => devices[deviceIndex(d)]?.display_name) },
      grid: { left: 8, right: 24, top: 34, bottom: 40, containLabel: true },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => `${esc(p.seriesName)}<br>${smart(p.value[2])} ${esc(metric.unit)} · ${num(p.value[0])}% of DRL${p.value[3] ? ` (${esc(p.value[3])})` : ''}` },
      xAxis: { type: 'value', min: 0, max: 200, name: `% of applicable DRL (${metric.label}, exam + age/weight group)`, nameLocation: 'middle', nameGap: 24, ...base._axis, axisLine: { ...base._axis.axisLine, onZero: false } },
      yAxis: [
        { type: 'value', min: -0.5, max: protos.length - 0.5, inverse: true, show: false },
        { type: 'category', data: protos, inverse: true, position: 'left', ...base._axis, axisTick: { show: false }, splitLine: { show: false },
          axisLabel: { ...base._axis.axisLabel, fontFamily: th.sans, color: th.text, width: labelWidth(), overflow: 'truncate', lineHeight: 15, formatter: (p) => `${p}\n{n|n ${num(withRef.filter((r) => r.protocol === p).length)} · DRL ${refRange(cfg.metric, p)} ${metric.unit}}`,
            rich: { n: { color: th.muted, fontFamily: th.mono, fontSize: 10, lineHeight: 14 } } } },
      ],
      series,
    };
  };
  withRef.forEach((r) => { const ref = refOf(r, cfg.metric); csv.push([r.protocol, r.device, r[metric.field], num((r[metric.field] / ref) * 100, 1)]); });
  const missing = rows.length - withRef.length;
  const note = (allProtos.length > protos.length ? ` · top ${protos.length} of ${allProtos.length} protocols` : '')
    + (missing ? ` · ${num(missing)} of ${num(rows.length)} without an applicable DRL (no exam DRL, or body exam without weight)` : '');
  return { option, csv, title: `${metric.label} vs applicable DRL, per protocol and device${note}`, summary: summaryRows(cfg.metric),
    height: String(Math.max(320, 44 * protos.length + 110)) };
}

// y-axis label width: protocol names are truncated (full name in the summary table)
const labelWidth = () => (matchMedia('(max-width: 700px)').matches ? 110 : 220);

function buildBoxOrBar(cfg) {
  const metric = METRICS[cfg.metric];
  const allProtos = protocolsBy(cfg.sort || 'n', cfg.metric);
  const protos = limitTop(cfg, allProtos, DEFAULT_TOP);
  const devIds = [...new Set(rows.map((r) => r.device_id))].sort((a, b) => a - b);
  const sums = summaryRows(cfg.metric);
  const get = (p, d) => sums.find((s) => s.protocol === p && s.device_id === d);
  const isBox = cfg.type === 'box';
  const stat = cfg.stat || 'median';
  const option = (th) => {
    const base = baseOption(th);
    const series = devIds.map((d) => {
      const name = devices[deviceIndex(d)]?.display_name;
      const data = protos.map((p) => { const s = get(p, d); if (!s) return isBox ? [] : null; return isBox ? [s.min, s.q1, s.median, s.q3, s.max] : (stat === 'mean' ? s.mean : s.median); });
      return isBox
        ? { name, type: 'boxplot', data, itemStyle: { color: 'transparent', borderColor: devColor(th, d), borderWidth: 1.5 }, boxWidth: [4, 10] }
        : { name, type: 'bar', data, itemStyle: { color: devColor(th, d) }, barMaxWidth: 10 };
    });
    const drlName = drlLabel(cfg.metric, protos);
    series.push({ name: drlName, type: 'scatter', symbol: 'rect', symbolSize: [2, 26], data: protos.map((p, i) => [protoRef(cfg.metric, p) ?? null, i]),
      itemStyle: { color: th.ref }, z: 6, tooltip: { formatter: (p) => `Applicable ${esc(drlLabel(cfg.metric, protos[p.dataIndex]))} (median) ${num(p.value[0])} ${metric.unit}` } });
    return {
      ...base,
      legend: { ...base.legend, data: [...devIds.map((d) => devices[deviceIndex(d)]?.display_name), drlName] },
      grid: { left: 8, right: 20, top: 34, bottom: 40, containLabel: true },
      tooltip: { ...base.tooltip, trigger: 'item' },
      xAxis: { type: 'log', name: `${metric.label} ${metric.unit} (log)`, nameLocation: 'middle', nameGap: 24, ...base._axis },
      yAxis: { type: 'category', data: protos, inverse: true, ...base._axis, axisLabel: { ...base._axis.axisLabel, fontFamily: th.sans, color: th.text, width: labelWidth(), overflow: 'truncate' } },
      series,
    };
  };
  const csv = [['protocol', 'device', 'n', 'min', 'q1', 'median', 'mean', 'q3', 'max', 'drl']]
    .concat(sums.map((s) => [s.protocol, s.device, s.n, s.min, s.q1, s.median, s.mean, s.q3, s.max, s.drl ?? '']));
  return { option, csv, title: `${metric.label} per protocol and device${isBox ? '' : ` · ${stat}`}${allProtos.length > protos.length ? ` · top ${protos.length} of ${allProtos.length} protocols` : ''}`, summary: sums,
    height: String(Math.max(320, 30 * protos.length + 110)) };
}

function buildHist(cfg) {
  const metric = METRICS[cfg.metric];
  const proto = protoFor(cfg, cfg.metric);
  const vals = rows.filter((r) => r.protocol === proto).map((r) => r[metric.field]);
  const bins = histogram(vals, 16);
  const ref = protoRef(cfg.metric, proto);
  const option = (th) => {
    const base = baseOption(th);
    return {
      ...base,
      legend: { show: false },
      grid: { left: 56, right: 20, top: 24, bottom: 40 },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => { const b = bins[p.dataIndex]; return `${num(b.from)}–${num(b.to)} ${metric.unit}: ${b.n}`; } },
      xAxis: { type: 'value', min: bins[0]?.from, max: bins[bins.length - 1]?.to, name: `${metric.label} ${metric.unit}`, nameLocation: 'middle', nameGap: 24, ...base._axis,
        axisLabel: { ...base._axis.axisLabel, formatter: (v) => num(v), showMinLabel: false, showMaxLabel: false } },
      yAxis: { type: 'value', name: 'studies', nameLocation: 'middle', nameGap: 40, ...base._axis },
      series: [{ type: 'custom', name: 'n', renderItem: (params, api) => {
        const b = bins[params.dataIndex];
        const p0 = api.coord([b.from, b.n]);
        const p1 = api.coord([b.to, 0]);
        return { type: 'rect', shape: { x: p0[0] + 1, y: p0[1], width: Math.max(1, p1[0] - p0[0] - 2), height: p1[1] - p0[1] }, style: { fill: th.seq } };
      }, data: bins.map((b) => [b.from, b.n]), encode: { x: 0, y: 1 },
      markLine: ref ? { silent: true, symbol: 'none', data: [{ xAxis: ref }], lineStyle: { color: th.ref, type: 'dashed', width: 2 }, label: { formatter: `${drlLabel(cfg.metric, proto)} ${num(ref)}`, color: th.ref } } : undefined }],
    };
  };
  return { option, csv: [['from', 'to', 'n']].concat(bins.map((b) => [b.from.toFixed(2), b.to.toFixed(2), b.n])), title: `Histogram · ${metric.label}, ${proto}`, summary: summaryRows(cfg.metric, proto), protocol: proto };
}

function buildTrend(cfg) {
  const metric = METRICS[cfg.metric];
  const proto = protoFor(cfg, cfg.metric);
  const per = cfg.period || 'month';
  const stat = cfg.stat || 'median';
  const rs = rows.filter((r) => r.protocol === proto);
  const periods = [...new Set(rs.map((r) => period(r.time, per)))].sort();
  const byDev = groupBy(rs, (r) => r.device_id, (r) => period(r.time, per));
  const ref = protoRef(cfg.metric, proto);
  const csv = [['device', 'period', 'n', stat]];
  const option = (th) => {
    const base = baseOption(th);
    const series = [...byDev.entries()].sort((a, b) => a[0] - b[0]).map(([dev, m]) => ({
      name: devices[deviceIndex(dev)]?.display_name, type: 'line', symbolSize: 5, lineStyle: { width: 2, color: devColor(th, dev) }, itemStyle: { color: devColor(th, dev) },
      data: periods.map((p) => { const v = m.get(p); if (!v) return null; const s = summary(v.map((r) => r[metric.field])); return stat === 'mean' ? s.mean : s.median; }),
    }));
    if (ref && series[0]) series[0].markLine = { silent: true, symbol: 'none', data: [{ yAxis: ref }], lineStyle: { color: th.ref, type: 'dashed' }, label: { formatter: drlLabel(cfg.metric, proto), color: th.ref } };
    return {
      ...base,
      legend: { ...base.legend, data: series.map((s) => s.name) },
      grid: { left: 60, right: 36, top: 34, bottom: 30 },
      tooltip: { ...base.tooltip, trigger: 'axis' },
      xAxis: { type: 'category', data: periods, ...base._axis },
      yAxis: { type: 'value', name: `${stat} ${metric.label} ${metric.unit}`, nameLocation: 'middle', nameGap: 44, scale: true, ...base._axis },
      series,
    };
  };
  for (const [dev, m] of byDev) for (const [p, v] of m) csv.push([devices[deviceIndex(dev)]?.display_name, p, v.length, smart(summary(v.map((r) => r[metric.field]))[stat === 'mean' ? 'mean' : 'median'])]);
  return { option, csv, title: `Trend · ${stat} ${metric.label} per ${per}, ${proto}`, summary: summaryRows(cfg.metric, proto), protocol: proto };
}

function buildScatter(cfg) {
  const [xKey, yKey] = GROUPS[groupOf(cfg)].scatter;
  const [xName, xUnit] = AXIS[xKey];
  const y = METRICS[yKey];
  const proto = protoFor(cfg, yKey, (r) => r[xKey] && r[yKey] != null);
  const rs = rows.filter((r) => r.protocol === proto && r[xKey] && r[yKey] != null);
  const option = (th) => {
    const base = baseOption(th);
    const byDev = groupBy(rs, (r) => r.device_id);
    return {
      ...base,
      legend: { ...base.legend, data: [...byDev.keys()].sort().map((d) => devices[deviceIndex(d)]?.display_name) },
      grid: { left: 66, right: 20, top: 34, bottom: 40 },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => `${esc(p.seriesName)}<br>${p.value[0]} ${xUnit} · ${smart(p.value[1])} ${y.unit}` },
      xAxis: { type: 'value', name: `${xName} ${xUnit}`, nameLocation: 'middle', nameGap: 24, scale: true, ...base._axis },
      yAxis: { type: 'value', name: `${y.label} ${y.unit}`, nameLocation: 'middle', nameGap: 50, ...base._axis },
      series: [...byDev.entries()].sort((a, b) => a[0] - b[0]).map(([dev, list]) => ({ name: devices[deviceIndex(dev)]?.display_name, type: 'scatter', symbolSize: 5,
        itemStyle: { color: devColor(th, dev), opacity: 0.7 }, data: list.map((r) => [r[xKey], r[yKey]]) })),
    };
  };
  return { option, csv: [['device', `${xKey}_${xUnit}`, `${yKey} ${y.unit}`]].concat(rs.map((r) => [r.device, r[xKey], r[yKey]])),
    title: rs.length ? `${y.label} vs ${xName} · ${proto} (n ${num(rs.length)})` : `${y.label} vs ${xName} · no rows with a ${xName} in this query`, summary: summaryRows(yKey, proto), protocol: proto };
}

function buildHeat(cfg) {
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const counts = days.map(() => Array(24).fill(0));
  for (const r of rows) {
    const d = new Date(`${r.time}:00`);
    counts[(d.getDay() + 6) % 7][d.getHours()] += 1;
  }
  const data = [];
  counts.forEach((hrs, di) => hrs.forEach((n, hr) => data.push([hr, di, n])));
  const max = Math.max(1, ...data.map((x) => x[2]));
  const option = (th) => {
    const base = baseOption(th);
    return {
      ...base,
      grid: { left: 44, right: 16, top: 12, bottom: 56 },
      tooltip: { ...base.tooltip, formatter: (p) => `${days[p.value[1]]} ${String(p.value[0]).padStart(2, '0')}:00 · ${p.value[2]} acquisitions` },
      xAxis: { type: 'category', data: [...Array(24).keys()].map((x) => String(x).padStart(2, '0')), ...base._axis, splitArea: { show: false } },
      yAxis: { type: 'category', data: days, inverse: true, ...base._axis },
      visualMap: { min: 0, max, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemHeight: 120, itemWidth: 10,
        inRange: { color: [th.panel, th.seq] }, textStyle: { color: th.muted, fontFamily: th.mono } },
      series: [{ type: 'heatmap', data, itemStyle: { borderColor: th.panel, borderWidth: 1 } }],
    };
  };
  return { option, csv: [['weekday', 'hour', 'n']].concat(data.map((x) => [days[x[1]], x[0], x[2]])), title: `Workload · ${GROUPS[groupOf(cfg)].label} ${GROUPS[groupOf(cfg)].unit} by weekday × hour`, summary: null };
}

const BUILDERS = { dots: buildDots, box: buildBoxOrBar, bar: buildBoxOrBar, hist: buildHist, trend: buildTrend, scatter: buildScatter, heat: buildHeat };

/* ---------------- panel UI ---------------- */

function summaryTable(list) {
  if (!list || !list.length) return null;
  return h('details', { class: 'panel-summary' },
    h('summary', { class: 'muted', style: { cursor: 'pointer', 'font-size': '12px' } }, `Summary table (${list.length} rows)`),
    h('div', { class: 'panel-scroll' }, h('table', { class: 'stats' },
      h('thead', {}, h('tr', {}, h('th', { class: 'l' }, 'Protocol · device'), ['n', 'Median', 'P75', 'IQR', 'DRL', 'P75/DRL'].map((x) => h('th', {}, x)))),
      h('tbody', {}, list.map((s) => h('tr', {},
        h('td', { class: 'l' }, h('span', { class: 'swatch', style: { background: `var(--d${(deviceIndex(s.device_id) % 8) + 1})` } }), `${s.protocol} · ${s.device}`),
        h('td', {}, num(s.n)), h('td', {}, smart(s.median)), h('td', {}, smart(s.p75)), h('td', {}, smart(s.iqr)), h('td', {}, s.drl ? num(s.drl) : '—'),
        h('td', { class: s.ratio > 1 ? 'over' : '' }, s.ratio ? `${num(s.ratio * 100)}%` : '—')))))));
}

function select(label, value, options, onChange) {
  return h('label', {}, `${label} `, h('select', { 'aria-label': label, onchange: (e) => onChange(e.target.value) },
    Object.entries(options).map(([v, text]) => h('option', { value: v, selected: v === value }, text))));
}

function renderPanel(cfg, index) {
  const server = cfg.type in SERVER_TYPES;
  if (!server) {  // browser panels: this group's rows, and a metric the group has
    cfg.group = groupOf(cfg);
    cfg.metric = metricOf(cfg);
    rows = rowsBy[cfg.group] || [];
    // Radiography: the same exam on every device meets under one name, "Torace AP · 5-15 kg"
    // (anatomy from the exposure's exam code, projection, size band: the name's, else the patient's)
    if (cfg.group === 'DX' && (cfg.by || 'exam') === 'exam') rows = rows.map((r) => (r.group ? { ...r, protocol: r.group } : r));
  }
  const built = server ? null : rows.length ? BUILDERS[cfg.type](cfg) : null;
  const g = GROUPS[groupOf(cfg)];
  const chart = h('dx-chart', { label: built?.title || 'Chart', height: built?.height || (cfg.type === 'sdrl' ? '420' : '320') });
  const update = (patch) => { Object.assign(cfg, patch); saveWs(); if (patch.group && !(patch.group in rowsBy)) loadRows(); else renderAll(); };
  const protos = server ? {} : Object.fromEntries(protocolsBy('name', cfg.metric).map((p) => [p, p]));
  const opts = server ? [select('Type', cfg.type, TYPES, (v) => update({ ...defaultsFor(v), top: undefined, modality: cfg.modality, span: cfg.span, type: v })),
    ...serverOptions(cfg, update, select), h('span', { class: 'spacer' }),
    h('span', { class: 'seg', role: 'group', 'aria-label': 'Panel width' }, Object.keys(SPANS).map((s) => h('button', { type: 'button', 'aria-pressed': String(cfg.span === s), onclick: () => update({ span: s }) }, s)))] : [
    select('Type', cfg.type, Object.fromEntries(Object.entries(TYPES).filter(([k]) => !(k in SERVER_TYPES))), (v) => update({ type: v })),
    select('Modality', cfg.group, Object.fromEntries(Object.entries(GROUPS).map(([k, x]) => [k, x.label])), (v) => update({ group: v, protocol: undefined })),
    cfg.type !== 'scatter' && cfg.type !== 'heat' ? select('Metric', cfg.metric, Object.fromEntries(g.metrics.map((k) => [k, METRICS[k].label])), (v) => update({ metric: v })) : null,
    cfg.group === 'DX' && cfg.type !== 'heat' ? select('Group protocols by', cfg.by || 'exam', { exam: 'Anatomy · projection · band', name: 'Protocol name (raw)' }, (v) => update({ by: v, protocol: undefined })) : null,
    ['hist', 'trend', 'scatter'].includes(cfg.type) ? select('Protocol', built?.protocol, protos, (v) => update({ protocol: v })) : null,
    ['bar', 'trend'].includes(cfg.type) ? select('Stat', cfg.stat || 'median', { median: 'Median', mean: 'Mean' }, (v) => update({ stat: v })) : null,
    cfg.type === 'trend' ? select('Period', cfg.period || 'month', PERIODS, (v) => update({ period: v })) : null,
    ['dots', 'box', 'bar'].includes(cfg.type) ? select('Sort', cfg.sort || 'n', { n: 'n', name: 'Name', value: 'Value / DRL' }, (v) => update({ sort: v })) : null,
    ['dots', 'box', 'bar'].includes(cfg.type) ? select('Top', String(cfg.top || DEFAULT_TOP), TOPS, (v) => update({ top: v })) : null,
    h('span', { class: 'spacer' }),
    h('span', { class: 'seg', role: 'group', 'aria-label': 'Panel width' }, Object.keys(SPANS).map((s) => h('button', { type: 'button', 'aria-pressed': String(cfg.span === s), onclick: () => update({ span: s }) }, s))),
  ];
  const panel = h('section', { class: 'ws-panel', style: { '--span': SPANS[cfg.span] || 6 }, 'aria-label': built?.title || TYPES[cfg.type], dataset: { index },
    ondragover: (e) => { e.preventDefault(); panel.classList.add('drop-target'); },
    ondragleave: () => panel.classList.remove('drop-target'),
    ondrop: (e) => { e.preventDefault(); panel.classList.remove('drop-target'); move(Number(e.dataTransfer.getData('text/plain')), index); } },
  h('div', { class: 'panel-head' },
    h('button', { class: 'grip', type: 'button', draggable: 'true', 'aria-label': 'Drag to move panel', title: 'Drag to move',
      ondragstart: (e) => { e.dataTransfer.setData('text/plain', String(index)); panel.classList.add('dragging'); },
      ondragend: () => panel.classList.remove('dragging') }, '⋮⋮'),
    h('h3', {}, server ? TYPES[cfg.type] : `${g.label} · ${built?.title || TYPES[cfg.type]}`), h('span', { class: 'spacer' }),
    h('button', { class: 'btn xs', type: 'button', 'aria-label': 'Move panel earlier', title: 'Move earlier', disabled: index === 0, onclick: () => move(index, index - 1) }, '←'),
    h('button', { class: 'btn xs', type: 'button', 'aria-label': 'Move panel later', title: 'Move later', disabled: index === ws.panels.length - 1, onclick: () => move(index, index + 1) }, '→'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => chart.downloadPNG(slug(built?.title)) }, 'PNG'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => chart.downloadCSV(slug(built?.title)) }, 'CSV'),
    h('button', { class: 'btn xs', type: 'button', 'aria-label': 'Remove panel', title: 'Remove panel', onclick: () => { ws.panels.splice(index, 1); saveWs(); renderAll(); } }, '×')),
  h('div', { class: 'ws-opts' }, opts),
  server ? h('div', { class: 'ws-body' }, chart, h('div', { class: 'srv-summary' }))
    : h('div', { class: 'ws-body' }, built ? chart : h('p', { class: 'muted' },
      cfg.type === 'dots' && rows.length ? `No per-${g.unit.replace(/s$/, '')} DRL for ${g.label} here: add a “Median vs DRL (server)” panel.` : t('state.empty')),
    built ? summaryTable(built.summary) : null));
  if (built) {
    chart.csv = built.csv;
    queueMicrotask(() => chart.draw(built.option));
  }
  if (server) {
    const head = panel.querySelector('h3');
    head.textContent = 'Loading…';
    buildServer(cfg, ws.q).then((b) => {
      head.textContent = b.title;
      if (b.height) chart.setAttribute('height', b.height);
      chart.csv = b.csv;
      chart.draw(b.option);
      if (b.toolbar) chart.before(b.toolbar(chart));
      panel.querySelector('.srv-summary').replaceChildren(serverSummary(b.summary, b.unit) || '');
    }).catch((err) => { head.textContent = TYPES[cfg.type]; chart.replaceWith(h('p', { class: 'error' }, err.message)); });
  }
  return panel;
}

function slug(text = 'chart') { return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }

function move(from, to) {
  if (to < 0 || to >= ws.panels.length || from === to) return;
  const [p] = ws.panels.splice(from, 1);
  ws.panels.splice(to, 0, p);
  saveWs();
  renderAll();
}

let gridHost;
let countEl;
function renderAll() {
  countEl.textContent = Object.entries(rowsBy).map(([k, rs]) => `${GROUPS[k].label} ${num(rs.length)} ${GROUPS[k].unit}`).join(' · ');
  clear(gridHost);
  ws.panels.forEach((cfg, i) => gridHost.append(renderPanel(cfg, i)));
  gridHost.append(h('div', { class: 'ws-add' }, h('span', {}, `${t('charts.add')}:`),
    Object.entries(TYPES).map(([type, label]) => h('button', { class: 'btn', type: 'button', onclick: () => {
      // a new panel starts on the modality of the tab it is added to
      const g = isMod(ws.name) ? modOf(ws.name) : 'CT';
      const srvMod = g === 'DX' ? 'DX,CR' : g === 'PT' ? 'PT' : g;
      ws.panels.push(type in SERVER_TYPES ? { ...defaultsFor(type), modality: srvMod, metric: g === 'CT' ? 'dlp_total' : undefined }
        : { type, group: g, metric: GROUPS[g].metrics[g === 'CT' && type === 'scatter' ? 1 : 0], span: 'M' });
      if (!(type in SERVER_TYPES) && !(g in rowsBy)) { saveWs(); loadRows(); return; }
      saveWs();
      renderAll();
      gridHost.lastElementChild.previousElementSibling.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } }, `+ ${label}`)),
    // per-exposure / per-study DRLs exist for these too (OMS 838/2025, by exam and age group)
    [['DX', 'dap', 'DAP dots vs DRL (DX/CR)'], ['RF', 'dap_total', 'DAP dots vs DRL (RF)'], ['NM', 'activity', 'Activity dots vs DRL (NM)'], ['PT', 'activity', 'Activity dots vs DRL (PET)']]
      .map(([group, metric, label]) => h('button', { class: 'btn', type: 'button', onclick: () => {
        ws.panels.push({ type: 'dots', group, metric, span: 'L' });
        saveWs();
        loadRows().then(() => gridHost.lastElementChild.previousElementSibling.scrollIntoView({ behavior: 'smooth', block: 'center' }));
      } }, `+ ${label}`))));
}

async function loadRows() {
  // one request per modality group the browser panels use
  const groups = [...new Set(ws.panels.filter((p) => !(p.type in SERVER_TYPES)).map(groupOf))];
  for (const k of Object.keys(rowsBy)) delete rowsBy[k];
  const got = await Promise.all(groups.map((k) => groupRows(k, ws.q).catch(() => [])));
  groups.forEach((k, i) => { rowsBy[k] = got[i].map(toShown); });
  renderAll();
}

// Workspaces follow the user: kept on the server (per user) and mirrored in this browser.
// The first visit after this change uploads the workspaces this browser already had.
let flushPrefs = async () => {};
// a reload must not drop the last change still waiting to be sent
const reloadPage = () => Promise.all([flushPrefs().catch(() => {}), flushShared().catch(() => {})]).finally(() => location.reload());

async function syncWorkspaces() {
  if (isSample) return;
  const names = await getPref('workspaces');
  if (names?.length) {
    prefs.set('workspaces', names);
    const [current, ...wss] = await Promise.all([getPref('workspace.current'), ...names.map((n) => getPref(wsKey(n)))]);
    names.forEach((n, i) => { if (wss[i]) prefs.set(wsKey(n), wss[i]); });
    if (current) prefs.set('workspace.current', current);
  } else {
    const local = wsNames();
    await Promise.all([putPref('workspaces', local), ...local.filter((n) => prefs.get(wsKey(n))).map((n) => putPref(wsKey(n), prefs.get(wsKey(n))))]);
  }
  const pending = new Map();
  let timer;
  prefs.subscribe((name, value) => {
    if (!name.startsWith('workspace')) return;
    pending.set(name, value);
    clearTimeout(timer);
    timer = setTimeout(() => flushPrefs().catch((e) => toast(`Workspace not saved on the server: ${e.message}`)), 600);
  });
  flushPrefs = () => {
    clearTimeout(timer);
    const batch = [...pending];
    pending.clear();
    return Promise.all(batch.map(([k, v]) => putPref(k, v)));
  };
}

async function init() {
  await syncWorkspaces().catch(() => {});  // the server copy is a convenience: the page works without it
  let user;
  [devices, drlSet, shared, user] = await Promise.all([physicalDevices(), drl(),
    sharedViews('workspace').catch(() => []), me().catch(() => null)]);
  setDevices(devices);
  isAdmin = !!user?.roles?.includes('admin');
  const names = wsNames();
  const urlQ = new URL(location.href).searchParams.get('q');
  // a link from the Studies list names its modality: open that modality's charts
  const urlMod = /(?:^|\s)modality:(\S+)/.exec(urlQ || '')?.[1]?.toUpperCase().split(',')[0];
  const fromUrl = urlMod && Object.entries(GROUPS).find(([k]) => k === urlMod || (k === 'DX' && urlMod === 'CR'))?.[0];
  const current = fromUrl ? `mod-${fromUrl}` : prefs.get('workspace.current') || 'mod-CT';
  ws = loadWs(isMod(current) || names.includes(current) || sharedOf(current) ? current : 'mod-CT');
  if (urlQ) ws.q = urlQ.split(' ').filter((x) => !/^(modality|age|acc|pid|spiral|axial|loc|dlp|ctdi|flag):/.test(x) && x !== '-phantom').join(' ');

  const qb = h('dx-query-bar', { value: ws.q });
  qb.context = { devices: new Map(devices.map((d) => [d.id, d])), drl: drlSet.ct_dlp_mgycm };
  qb.addEventListener('query-change', (e) => { ws.q = e.detail; saveWs(); loadRows(); });
  countEl = h('span', { class: 'muted mono' });
  gridHost = h('div', { class: 'ws' });

  const wsSelect = h('select', { 'aria-label': t('charts.workspace'), onchange: (e) => {
    if (e.target.value === '__new') {
      const name = (prompt('Name of the new workspace', '') || '').trim();
      if (!name) { e.target.value = ws.name; return; }
      if (wsNames().includes(name)) { toast(`A workspace named “${name}” exists already.`); e.target.value = ws.name; return; }
      prefs.set('workspaces', [...wsNames(), name]);
      prefs.set('workspace.current', name);
    } else prefs.set('workspace.current', e.target.value);
    reloadPage();
  } }, isMod(ws.name) ? h('option', { value: '', selected: true }, '— workspaces —') : null,
  shared.length ? h('optgroup', { label: 'Shared' }, shared.map((v) => h('option', { value: `shared:${v.id}`, selected: v.id === ws.sharedId }, `◆ ${v.name}`))) : null,
  h('optgroup', { label: shared.length ? 'Mine' : 'My workspaces' }, wsNames().map((n) => h('option', { value: n, selected: ws.sharedId == null && n === ws.name }, n))),
  h('option', { value: '__new' }, '+ New workspace…'));

  // a shared workspace: admins change it for everyone; anyone else can keep a copy of their changes
  const saveMine = () => {
    let name = (prompt('Name of your copy', `${ws.name} (mine)`) || '').trim();
    if (!name) return;
    if (wsNames().includes(name)) { toast(`A workspace named “${name}” exists already.`); return; }
    prefs.set(wsKey(name), { name, q: ws.q, panels: ws.panels });
    prefs.set('workspaces', [...wsNames(), name]);
    prefs.set('workspace.current', name);
    reloadPage();
  };
  dirtyEl = h('span', { class: 'row', hidden: true, style: { gap: '6px' } },
    h('span', { class: 'tag warn' }, 'Changes not saved'),
    h('button', { class: 'btn primary', type: 'button', onclick: saveMine }, 'Save as my workspace'));
  const shareWs = async () => {
    try {
      const v = await createView('workspace', ws.name, { q: ws.q, panels: ws.panels });
      // moved, not copied: the personal one goes
      const left = wsNames().filter((n) => n !== ws.name);
      prefs.set(wsKey(ws.name), null);
      prefs.set('workspaces', left.length ? left : [DEFAULT_WS]);
      prefs.set('workspace.current', `shared:${v.id}`);
      toast(`“${ws.name}” is now shared with everyone`);
      reloadPage();
    } catch (e) { toast(`Not shared: ${e.message}`); }
  };
  const isShared = ws.sharedId != null;
  const note = isSample ? 'saved in this browser' : isShared ? (isAdmin ? 'shared · your changes are published to everyone' : 'shared by an admin · changes are not kept unless you save a copy')
    : 'auto-saved to your account';
  document.querySelector('dx-shell')?.setSub(isMod(ws.name) ? (GROUPS[modOf(ws.name)]?.label || modOf(ws.name)) : ws.name);
  const modTabs = h('nav', { class: 'tabs', 'aria-label': 'Modality' }, Object.entries(GROUPS).map(([k, g]) => h('button', {
    type: 'button', 'aria-current': ws.name === `mod-${k}` ? 'page' : false,
    onclick: () => { prefs.set('workspace.current', `mod-${k}`); reloadPage(); } }, g.label)));

  page.replaceChildren(
    modTabs,
    h('div', { class: 'row' },
      h('span', { class: 'pill' }, `${t('charts.workspace')}:`, wsSelect),
      h('span', { class: 'muted small' }, note),
      dirtyEl,
      h('span', { class: 'spacer' }),
      h('a', { class: 'btn', href: '#', onclick: (e) => { const g = isMod(ws.name) ? modOf(ws.name) : 'CT'; e.currentTarget.href = `studies.html?q=${encodeURIComponent(`modality:${g === 'DX' ? 'DX,CR' : g} ${ws.q} -phantom`)}`; } }, 'Studies ↗'),
      h('button', { class: 'btn', type: 'button', onclick: () => { navigator.clipboard?.writeText(location.href.split('?')[0] + '?q=' + encodeURIComponent(ws.q)); toast('Link to this query copied.'); } }, 'Copy link'),
      isShared && !isAdmin ? h('button', { class: 'btn', type: 'button', onclick: saveMine }, 'Copy to mine')
        : h('button', { class: 'btn', type: 'button', onclick: async () => { if (await confirmDialog(isShared ? 'Reset this shared workspace to the default panels, for everyone?' : 'Reset this workspace to the default panels?', { title: 'Reset workspace', label: 'Reset', danger: true })) { Object.assign(ws, defaultsOf(ws.name)); saveWs(); reloadPage(); } } }, t('charts.reset')),
      isAdmin && !isShared && !isMod(ws.name) ? h('button', { class: 'btn', type: 'button', title: 'Publish this workspace for every user', onclick: shareWs }, 'Share with everyone') : null,
      isShared && isAdmin ? h('button', { class: 'btn danger', type: 'button', onclick: async () => {
        if (!await confirmDialog(`Remove the shared workspace “${ws.name}” for everyone? This cannot be undone.`, { title: 'Remove shared workspace', label: 'Remove', danger: true })) return;
        try { await deleteView(ws.sharedId); } catch (e) { toast(`Not removed: ${e.message}`); return; }
        clearTimeout(sharedTimer);
        flushShared = async () => {};
        prefs.set('workspace.current', 'mod-CT');
        reloadPage();
      } }, 'Delete') : null,
      isMod(ws.name) || isShared ? null : h('button', { class: 'btn danger', type: 'button', onclick: async () => {
        if (!await confirmDialog(`Delete the workspace “${ws.name}” and its panels? This cannot be undone.`, { title: 'Delete workspace', label: 'Delete', danger: true })) return;
        const left = wsNames().filter((n) => n !== ws.name);
        prefs.set(wsKey(ws.name), null);
        prefs.set('workspaces', left.length ? left : [DEFAULT_WS]);  // the last one is replaced by a fresh default
        prefs.set('workspace.current', (left.length ? left : [DEFAULT_WS])[0]);
        reloadPage();
      } }, 'Delete')),
    h('div', { class: 'row', style: { 'align-items': 'flex-start' } },
      h('div', { style: { flex: '1 1 600px' } }, qb),
      h('span', { class: 'mt-3' }, countEl)),
    gridHost);
  await loadRows();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
