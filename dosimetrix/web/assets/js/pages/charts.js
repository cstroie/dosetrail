// Charts workspace (CT). One query is shared by every panel. Panels can be added, re-ordered
// (drag the grip, or ← →), resized (S/M/L/XL), re-configured inline and exported (PNG / CSV).
// Each panel has a summary table (n, median, P75, IQR, P75/DRL). The workspace layout is saved
// per browser for now; the server will store workspaces per user, with read-only sharing.

import '../components/dt-shell.js';
import '../components/dt-query-bar.js';
import '../components/dt-chart.js';
import { baseOption } from '../components/dt-chart.js';
import { h, clear, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, smart } from '../core/format.js';
import { ctAcquisitions, physicalDevices, drl, isSample } from '../core/api.js';
import { summary, groupBy, histogram } from '../core/stats.js';
import { prefs } from '../core/prefs.js';
import { SERVER_TYPES, buildServer, serverOptions, serverSummary, defaultsFor } from './charts-server.js';

const page = document.getElementById('page');

const METRICS = { ctdivol: { label: 'CTDIvol', unit: 'mGy', field: 'ctdivol' }, dlp: { label: 'DLP', unit: 'mGy·cm', field: 'dlp' } };
const TYPES = { dots: 'CT dots vs DRL', box: 'CT boxplot', bar: 'CT median / mean bar', hist: 'CT histogram', trend: 'CT trend', scatter: 'CT DLP vs weight', heat: 'CT workload' };
const SPANS = { S: 4, M: 6, L: 8, XL: 12 };
const PERIODS = { week: 'Week', month: 'Month', quarter: 'Quarter', year: 'Year' };
const DEFAULT_Q = 'date:2026-07-01..2026-11-21 origin:local';
const DEFAULT_PANELS = [
  { type: 'dots', metric: 'ctdivol', span: 'L' },
  { type: 'box', metric: 'dlp', span: 'S', sort: 'n' },
  { type: 'trend', metric: 'ctdivol', span: 'M', period: 'month', stat: 'median' },
  { type: 'scatter', metric: 'dlp', span: 'M' },
  { type: 'hist', metric: 'dlp', span: 'M' },
  { type: 'heat', metric: 'ctdivol', span: 'M' },
  { ...defaultsFor('sdrl'), span: 'L' },
  { ...defaultsFor('sbox'), modality: 'DX', metric: 'dap', by: 'exam', series: 'age_band', system: 'ro-drl-ms838-2025', span: 'L' },
];

Object.assign(TYPES, SERVER_TYPES);
let ws;         // { name, q, panels }
let rows = [];  // acquisitions for the current query
let devices = [];
let drlSet;

function wsKey(name) { return `workspace.${name}`; }
function wsNames() { return prefs.get('workspaces') || ['My audit · CT 2026 H2']; }
function loadWs(name) {
  const saved = prefs.get(wsKey(name));
  return saved ? structuredClone(saved) : { name, q: DEFAULT_Q, panels: structuredClone(DEFAULT_PANELS) };
}
function saveWs() { prefs.set(wsKey(ws.name), ws); }

/* ---------------- data helpers ---------------- */

function deviceIndex(id) { return devices.findIndex((d) => d.id === id); }
function devColor(th, id) { return th.devices[deviceIndex(id) % 8]; }
function drlFor(metric, protocol) { return (metric === 'ctdivol' ? drlSet.ct_ctdivol_mgy : drlSet.ct_dlp_mgycm)[protocol]; }
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

function protocolsBy(sort, metric) {
  const g = groupBy(rows, (r) => r.protocol);
  const list = [...g.entries()].map(([p, rs]) => ({ p, n: rs.length, med: summary(rs.map((r) => r[METRICS[metric].field])).median }));
  if (sort === 'name') list.sort((a, b) => a.p.localeCompare(b.p));
  else if (sort === 'value') list.sort((a, b) => b.med / (protoRef(metric, b.p) || 1) - a.med / (protoRef(metric, a.p) || 1));
  else list.sort((a, b) => b.n - a.n);
  return list.map((x) => x.p);
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
  const withRef = rows.filter((r) => refOf(r, cfg.metric));
  const TOP = 12;  // more rows than this do not fit the panel; the summary table has them all
  const allProtos = protocolsBy(cfg.sort || 'n', cfg.metric).filter((p) => withRef.some((r) => r.protocol === p));
  const protos = allProtos.slice(0, TOP);
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
        tooltip: { formatter: (p) => `${name} · median ${num(p.value[0])}% of ${drlLabel(cfg.metric, protos[Math.round(p.value[1])])}` } });
    });
    series[0].markLine = { silent: true, symbol: 'none', data: [{ xAxis: 100 }], lineStyle: { color: th.ref, type: 'dashed', width: 2 },
      label: { formatter: drlLabel(cfg.metric, protos), position: 'start', color: th.ref, fontFamily: th.mono } };
    return {
      ...base,
      legend: { ...base.legend, data: devIds.map((d) => devices[deviceIndex(d)]?.display_name) },
      grid: { left: 8, right: 24, top: 34, bottom: 40, containLabel: true },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => `${p.seriesName}<br>${smart(p.value[2])} ${metric.unit} · ${num(p.value[0])}% of DRL${p.value[3] ? ` (${p.value[3]})` : ''}` },
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
  const note = (allProtos.length > TOP ? ` · top ${TOP} of ${allProtos.length} protocols` : '')
    + (missing ? ` · ${num(missing)} of ${num(rows.length)} without an applicable DRL (no exam DRL, or body exam without weight)` : '');
  return { option, csv, title: `${metric.label} vs applicable DRL, per protocol and device${note}`, summary: summaryRows(cfg.metric),
    height: String(Math.max(320, 44 * protos.length + 110)) };
}

// y-axis label width: protocol names are truncated (full name in the summary table)
const labelWidth = () => (matchMedia('(max-width: 700px)').matches ? 110 : 220);

function buildBoxOrBar(cfg) {
  const metric = METRICS[cfg.metric];
  const protos = protocolsBy(cfg.sort || 'n', cfg.metric);
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
      itemStyle: { color: th.ref }, z: 6, tooltip: { formatter: (p) => `Applicable ${drlLabel(cfg.metric, protos[p.dataIndex])} (median) ${num(p.value[0])} ${metric.unit}` } });
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
  return { option, csv, title: `${metric.label} per protocol and device${isBox ? '' : ` · ${stat}`}`, summary: sums,
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
  const proto = protoFor(cfg, 'dlp', (r) => r.weight && r.dlp);
  const rs = rows.filter((r) => r.protocol === proto && r.weight);
  const option = (th) => {
    const base = baseOption(th);
    const byDev = groupBy(rs, (r) => r.device_id);
    return {
      ...base,
      legend: { ...base.legend, data: [...byDev.keys()].sort().map((d) => devices[deviceIndex(d)]?.display_name) },
      grid: { left: 66, right: 20, top: 34, bottom: 40 },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => `${p.seriesName}<br>${p.value[0]} kg · ${num(p.value[1])} mGy·cm` },
      xAxis: { type: 'value', name: 'weight kg', nameLocation: 'middle', nameGap: 24, scale: true, ...base._axis },
      yAxis: { type: 'value', name: 'DLP mGy·cm', nameLocation: 'middle', nameGap: 50, ...base._axis },
      series: [...byDev.entries()].sort((a, b) => a[0] - b[0]).map(([dev, list]) => ({ name: devices[deviceIndex(dev)]?.display_name, type: 'scatter', symbolSize: 5,
        itemStyle: { color: devColor(th, dev), opacity: 0.7 }, data: list.map((r) => [r.weight, r.dlp]) })),
    };
  };
  return { option, csv: [['device', 'weight_kg', 'dlp_mgycm']].concat(rs.map((r) => [r.device, r.weight, r.dlp])),
    title: rs.length ? `DLP vs patient weight · ${proto} (n ${num(rs.length)} with weight)` : "DLP vs patient weight · no acquisitions with a patient weight in this query", summary: summaryRows('dlp', proto), protocol: proto };
}

function buildHeat() {
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
  return { option, csv: [['weekday', 'hour', 'n']].concat(data.map((x) => [days[x[1]], x[0], x[2]])), title: 'Workload · acquisitions by weekday × hour', summary: null };
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
  const built = server ? null : rows.length ? BUILDERS[cfg.type](cfg) : null;
  const chart = h('dt-chart', { label: built?.title || 'Chart', height: built?.height || (cfg.type === 'sdrl' ? '420' : '320') });
  const update = (patch) => { Object.assign(cfg, patch); saveWs(); renderAll(); };
  const protos = Object.fromEntries(protocolsBy('name', 'ctdivol').map((p) => [p, p]));
  const opts = server ? [select('Type', cfg.type, TYPES, (v) => update({ ...defaultsFor(v), modality: cfg.modality, span: cfg.span, type: v })),
    ...serverOptions(cfg, update, select), h('span', { class: 'spacer' }),
    h('span', { class: 'seg', role: 'group', 'aria-label': 'Panel width' }, Object.keys(SPANS).map((s) => h('button', { type: 'button', 'aria-pressed': String(cfg.span === s), onclick: () => update({ span: s }) }, s)))] : [
    select('Type', cfg.type, TYPES, (v) => update({ type: v })),
    cfg.type !== 'scatter' && cfg.type !== 'heat' ? select('Metric', cfg.metric, { ctdivol: 'CTDIvol', dlp: 'DLP' }, (v) => update({ metric: v })) : null,
    ['hist', 'trend', 'scatter'].includes(cfg.type) ? select('Protocol', built?.protocol, protos, (v) => update({ protocol: v })) : null,
    ['bar', 'trend'].includes(cfg.type) ? select('Stat', cfg.stat || 'median', { median: 'Median', mean: 'Mean' }, (v) => update({ stat: v })) : null,
    cfg.type === 'trend' ? select('Period', cfg.period || 'month', PERIODS, (v) => update({ period: v })) : null,
    ['dots', 'box', 'bar'].includes(cfg.type) ? select('Sort', cfg.sort || 'n', { n: 'n', name: 'Name', value: 'Value / DRL' }, (v) => update({ sort: v })) : null,
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
    h('h3', {}, built?.title || TYPES[cfg.type]), h('span', { class: 'spacer' }),
    h('button', { class: 'btn xs', type: 'button', 'aria-label': 'Move panel earlier', disabled: index === 0, onclick: () => move(index, index - 1) }, '←'),
    h('button', { class: 'btn xs', type: 'button', 'aria-label': 'Move panel later', disabled: index === ws.panels.length - 1, onclick: () => move(index, index + 1) }, '→'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => chart.downloadPNG(slug(built?.title)) }, 'PNG'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => chart.downloadCSV(slug(built?.title)) }, 'CSV'),
    h('button', { class: 'btn xs', type: 'button', 'aria-label': 'Remove panel', onclick: () => { ws.panels.splice(index, 1); saveWs(); renderAll(); } }, '×')),
  h('div', { class: 'ws-opts' }, opts),
  server ? h('div', { class: 'ws-body' }, chart, h('div', { class: 'srv-summary' }))
    : h('div', { class: 'ws-body' }, built ? chart : h('p', { class: 'muted' }, t('state.empty')), built ? summaryTable(built.summary) : null));
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
  countEl.textContent = `${num(rows.length)} acquisitions`;
  clear(gridHost);
  ws.panels.forEach((cfg, i) => gridHost.append(renderPanel(cfg, i)));
  gridHost.append(h('div', { class: 'ws-add' }, h('span', {}, `${t('charts.add')}:`),
    Object.entries(TYPES).map(([type, label]) => h('button', { class: 'btn', type: 'button', onclick: () => {
      ws.panels.push(type in SERVER_TYPES ? defaultsFor(type) : { type, metric: type === 'scatter' ? 'dlp' : 'ctdivol', span: 'M' });
      saveWs();
      renderAll();
      gridHost.lastElementChild.previousElementSibling.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } }, `+ ${label}`))));
}

async function loadRows() {
  rows = await ctAcquisitions(ws.q);
  renderAll();
}

async function init() {
  [devices, drlSet] = await Promise.all([physicalDevices(), drl()]);
  devices = devices.filter((d) => d.modality === 'CT');
  const names = wsNames();
  const urlQ = new URL(location.href).searchParams.get('q');
  ws = loadWs(prefs.get('workspace.current') || names[0]);
  if (urlQ) ws.q = urlQ.split(' ').filter((x) => !/^(modality|age|acc|pid|desc|spiral|axial|loc|dlp|ctdi|flag):/.test(x) && x !== '-phantom').join(' ');

  const qb = h('dt-query-bar', { value: ws.q });
  qb.context = { devices: new Map(devices.map((d) => [d.id, d])), drl: drlSet.ct_dlp_mgycm };
  qb.addEventListener('query-change', (e) => { ws.q = e.detail; saveWs(); loadRows(); });
  countEl = h('span', { class: 'muted mono' });
  gridHost = h('div', { class: 'ws' });

  const wsSelect = h('select', { 'aria-label': t('charts.workspace'), onchange: (e) => {
    if (e.target.value === '__new') {
      const name = prompt('Name of the new workspace', '');
      if (!name) { e.target.value = ws.name; return; }
      prefs.set('workspaces', [...wsNames(), name]);
      prefs.set('workspace.current', name);
    } else prefs.set('workspace.current', e.target.value);
    location.reload();
  } }, wsNames().map((n) => h('option', { value: n, selected: n === ws.name }, n)), h('option', { value: '__new' }, '+ New workspace…'));

  page.replaceChildren(
    h('div', { class: 'row' },
      h('span', { class: 'pill' }, `${t('charts.workspace')}:`, wsSelect),
      h('span', { class: 'muted', style: { 'font-size': '11px' } }, isSample ? 'saved in this browser' : 'auto-saved'),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn', type: 'button', onclick: () => { navigator.clipboard?.writeText(location.href.split('?')[0] + '?q=' + encodeURIComponent(ws.q)); toast('Link to this query copied. Sharing whole workspaces needs the server.'); } }, 'Share'),
      h('button', { class: 'btn', type: 'button', onclick: () => { if (confirm('Reset this workspace to the default panels?')) { ws.panels = structuredClone(DEFAULT_PANELS); ws.q = DEFAULT_Q; saveWs(); location.reload(); } } }, t('charts.reset'))),
    h('div', { class: 'row', style: { 'align-items': 'flex-start' } },
      h('div', { style: { flex: '1 1 600px' } }, qb),
      h('a', { class: 'btn', href: '#', style: { 'margin-top': '7px' }, onclick: (e) => { e.currentTarget.href = `studies.html?q=${encodeURIComponent(`modality:CT ${ws.q} -phantom`)}`; } }, 'Studies ↗'),
      h('span', { style: { 'margin-top': '12px' } }, countEl)),
    h('p', { class: 'muted', style: { 'font-size': '11px' } }, `${t('charts.query')}: every panel uses it. Keys here: date, device, proto, weight, origin. Protocol names are standardised and case-merged on the server.`),
    gridHost);
  await loadRows();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
