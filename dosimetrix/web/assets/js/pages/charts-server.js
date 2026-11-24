// Server-side statistics panels for the charts workspace (any modality).
// The workspace query applies, plus the panel's modality. Percentiles come from
// GET /api/stats (type-7 quantiles, the same numbers as exports and reports); the
// "vs DRL" panel uses GET /api/drl/compare.

import { baseOption } from '../components/dx-chart.js';
import { h } from '../core/dom.js';
import { num, smart, dapUnit, DAP_UNITS } from '../core/format.js';
import { stats, drlCompare } from '../core/api.js';

export const SERVER_TYPES = {
  sbox: 'Boxplot (server)', sbar: 'Median / mean bars (server)', strend: 'Trend (server)',
  shist: 'Histogram (server)', sdrl: 'Median vs DRL (server)',
};

const MODALITIES = { CT: 'CT', 'DX,CR': 'Radiography DX + CR', DX: 'Radiography DX', CR: 'Radiography CR', RF: 'Fluoroscopy RF', XA: 'Angiography XA', MG: 'Mammography', NM: 'Nuclear medicine', PT: 'PET' };
const METRICS = {
  CT: { dlp_total: 'DLP study total', ctdivol_max: 'CTDIvol max', dlp: 'DLP per series', ctdivol: 'CTDIvol per series', scan_length: 'Scan length' },
  DX: { dap: 'DAP per exposure', dap_total: 'DAP study total', kvp: 'kVp', mas: 'mAs', ei: 'Exposure index', di: 'Deviation index', exposures: 'Exposures per study' },
  RF: { dap_total: 'DAP total', kar_total: 'Ka,r total', fluoro_time: 'Fluoroscopy time', dap_fluoro: 'DAP fluoroscopy', dap_acq: 'DAP acquisition' },
  MG: { agd: 'AGD per view', agd_breast: 'AGD per breast', thickness: 'Compressed thickness', force: 'Compression force', kvp: 'kVp', mas: 'mAs' },
  NM: { activity: 'Administered activity', activity_kg: 'Activity per kg' },
};
METRICS.CR = METRICS.DX;
METRICS['DX,CR'] = METRICS.DX;
METRICS.PT = METRICS.NM;
METRICS.XA = { ...METRICS.RF, psd: 'Peak skin dose (estimate)' };
const DIMS = { exam: 'Exam', device: 'Device', age_band: 'Age group', weight_band: 'Weight group', ref_group: 'ICRP 135 group', view: 'Projection', protocol: 'Protocol (raw)', sex: 'Sex', indication: 'Clinical indication', year: 'Year' };
const PERIODS = { month: 'Month', quarter: 'Quarter', year: 'Year' };
const SYSTEMS = { '': 'Local nomenclator', 'ro-drl-ms838-2025': 'National DRL exams', 'ec-rp185': 'RP 185 exams' };
const AGE = { 'ro-drl-age': 'National 1–4/5–9/10–14/15+', 'rp185-head-age': 'RP 185 head', 'ext-head-age': 'RP 185 head + adults' };
const DRL_SETS = { 'ro-ms838-2025': 'National OMS 838/2025', 'ec-rp185-2018': 'European RP 185' };
const TOPS = { all: 'All', 5: '5', 8: '8', 10: '10', 16: '16', 20: '20', 30: '30', 50: '50' };
/** How many groups a panel charts: its Top option, else the panel's default. */
const topOf = (cfg, dflt) => (cfg.top === 'all' ? Infinity : Number(cfg.top) || dflt);
const DEFAULT_TOP = 10;  // every Top-N panel starts with 10, never with all
const topSelect = (cfg, select, update) => select('Top', String(cfg.top || DEFAULT_TOP), TOPS, (v) => update({ top: v }));

export function defaultsFor(type) {
  return { type, modality: 'CT', metric: 'dlp_total', by: type === 'strend' ? 'month' : 'exam', series: type === 'strend' ? 'exam' : '',
    system: '', age_scheme: 'ro-drl-age', set: 'ro-ms838-2025', stat: 'median', min_n: 5, span: 'M' };
}

/** Options row for a server panel. `select(label, value, options, onChange)` comes from charts.js. */
export function serverOptions(cfg, update, select) {
  const mod = cfg.modality || 'CT';
  const metrics = METRICS[mod];
  const out = [select('Modality', mod, MODALITIES, (v) => update({ modality: v, metric: Object.keys(METRICS[v])[0] }))];
  if (cfg.type === 'sdrl') {
    out.push(select('Reference', cfg.set || 'ro-ms838-2025', DRL_SETS, (v) => update({ set: v })));
    out.push(topSelect(cfg, select, update));
    return out;
  }
  out.push(select('Metric', metrics[cfg.metric] ? cfg.metric : Object.keys(metrics)[0], metrics, (v) => update({ metric: v })));
  if (cfg.type === 'strend') out.push(select('Period', cfg.by || 'month', PERIODS, (v) => update({ by: v })));
  else if (cfg.type !== 'shist') out.push(select('Group by', cfg.by || 'exam', DIMS, (v) => update({ by: v })));
  out.push(select(cfg.type === 'shist' ? 'Split by' : 'Series', cfg.series || '', { '': '—', ...DIMS }, (v) => update({ series: v })));
  if ([cfg.by, cfg.series].includes('exam')) out.push(select('Exams', cfg.system || '', SYSTEMS, (v) => update({ system: v })));
  if ([cfg.by, cfg.series].includes('age_band')) out.push(select('Age groups', cfg.age_scheme || 'ro-drl-age', AGE, (v) => update({ age_scheme: v })));
  if (cfg.type === 'sbar' || cfg.type === 'strend') out.push(select('Stat', cfg.stat || 'median', { median: 'Median', mean: 'Mean', p75: 'P75' }, (v) => update({ stat: v })));
  if (cfg.type !== 'shist') out.push(topSelect(cfg, select, update));
  out.push(select('Min n', String(cfg.min_n ?? 5), { 1: '1', 5: '5', 10: '10', 20: '20' }, (v) => update({ min_n: Number(v) })));
  return out;
}

function query(cfg, q) {
  return `modality:${cfg.modality || 'CT'} ${q || ''}`.trim();
}

function unitLabel(u) {
  return { 'mGy.cm': 'mGy·cm', 'Gy.cm2': 'Gy·cm²', 'mGy.cm2': 'mGy·cm²', 'µGy.m2': 'µGy·m²', 'mA.s': 'mAs', 1: '' }[u] ?? u;
}

/** Stored units → shown units: DAP in the user's unit (mGy·cm² by default), Ka,r in mGy.
 *  [factor, shown unit code] for a stored unit code, or null when shown as stored. */
function shownUnit(u) {
  if (u === 'Gy') return [1000, 'mGy'];
  if (u === 'Gy.cm2') { const d = dapUnit(); return [DAP_UNITS[d].factor, d.replace('·', '.').replace('²', '2')]; }
  return null;
}
const scaleKeys = (o, keys, f) => { for (const k of keys) if (typeof o[k] === 'number') o[k] *= f; };
const STAT_KEYS = ['mean', 'sd', 'p25', 'median', 'p75', 'min', 'max'];

/** /api/stats: every dose value (and the histogram edges) in the shown unit, at one place. */
function toShown(res) {
  const s = shownUnit(res.unit);
  if (!s) return res;
  const [f, unit] = s;
  for (const g of res.groups) scaleKeys(g, STAT_KEYS, f);
  if (res.overall) scaleKeys(res.overall, STAT_KEYS, f);
  if (res.bins) res.bins = res.bins.map((v) => v * f);
  res.unit = unit;
  return res;
}

/** Fetch and build: resolves to { title, option(th), csv, summary } */
export async function buildServer(cfg, q, devColor) {
  if (cfg.type === 'sdrl') return buildDrl(cfg, q);
  const mod = cfg.modality || 'CT';
  const metric = METRICS[mod][cfg.metric] ? cfg.metric : Object.keys(METRICS[mod])[0];
  const dims = cfg.type === 'shist' ? [cfg.series].filter(Boolean) : [cfg.by || 'exam', cfg.series].filter(Boolean);
  const res = toShown(await stats({ q: query(cfg, q), metric, by: [...new Set(dims)].join(','), system: cfg.system || undefined,
    age_scheme: cfg.age_scheme || 'ro-drl-age', bins: cfg.type === 'shist' ? 24 : 0, min_n: cfg.min_n ?? 5 }));
  const unit = unitLabel(res.unit);
  const [d1, d2] = res.group_by;
  const lab = (g, d) => (d ? (g.label[d] ?? g.key[d] ?? '—') : 'all');
  const stat = cfg.stat || 'median';
  const csv = [[...res.group_by, 'n', 'n_studies', 'mean', 'p25', 'median', 'p75', 'min', 'max']]
    .concat(res.groups.map((g) => [...res.group_by.map((d) => lab(g, d)), g.n, g.n_studies, g.mean, g.p25, g.median, g.p75, g.min, g.max]));
  const summary = res.groups.map((g) => ({ label: res.group_by.map((d) => lab(g, d)).join(' · '), n: g.n, median: g.median, p75: g.p75, mean: g.mean }));
  const title = `${res.label} (${unit || '1'}) · ${MODALITIES[mod]}${d1 ? ` by ${(DIMS[d1] || PERIODS[d1] || d1).toLowerCase()}` : ''}${d2 ? ` × ${(DIMS[d2] || d2).toLowerCase()}` : ''} · n ${num(res.n)}`;

  // categories (first dimension) ordered as the server sorted them (bands in order), top N by n
  const cats = [];
  const catN = new Map();
  for (const g of res.groups) {
    const c = lab(g, d1);
    if (!catN.has(c)) { cats.push(c); catN.set(c, 0); }
    catN.set(c, catN.get(c) + g.n);
  }
  const isTime = ['month', 'quarter', 'year'].includes(d1);
  let shown = isTime ? cats : cats.filter((c) => catN.get(c) >= (cfg.min_n ?? 5));
  const banded = ['age_band', 'weight_band', 'ref_group'].includes(d1);
  const limit = topOf(cfg, banded ? Infinity : DEFAULT_TOP);  // a time axis is not cut; bands keep their own order
  if (cfg.type !== 'strend' && cfg.type !== 'shist' && shown.length > limit) {
    if (banded) { const keep = new Set([...shown].sort((a, b) => catN.get(b) - catN.get(a)).slice(0, limit)); shown = shown.filter((c) => keep.has(c)); }
    else shown = [...shown].sort((a, b) => catN.get(b) - catN.get(a)).slice(0, limit);
  }
  let serNames = d2 ? [...new Set(res.groups.map((g) => lab(g, d2)))] : ['all'];
  const order = res.orders?.[d2];
  if (order) serNames = [...order.filter((x) => serNames.includes(x)), ...serNames.filter((x) => !order.includes(x))];
  const pick = (c, s) => res.groups.find((g) => lab(g, d1) === c && (!d2 || lab(g, d2) === s));
  const color = (th, i) => th.devices[i % th.devices.length];
  const nSeries = d2 ? new Set(res.groups.map((g) => lab(g, d2))).size : 0;
  const truncated = cfg.type === 'strend' ? (nSeries > topOf(cfg, DEFAULT_TOP) ? ` · top ${topOf(cfg, DEFAULT_TOP)} of ${nSeries} series` : '')
    : shown.length < cats.length ? ` · top ${shown.length} of ${cats.length}` : '';

  const option = (th) => {
    const base = baseOption(th);
    if (cfg.type === 'shist') {
      const edges = res.bins || [];
      const labels = edges.slice(0, -1).map((e, i) => `${smart(e)}–${smart(edges[i + 1])}`);
      return { ...base, legend: { ...base.legend, data: serNames }, grid: { left: 8, right: 16, top: 40, bottom: 40, containLabel: true },
        tooltip: { ...base.tooltip, trigger: 'axis' },
        xAxis: { type: 'category', data: labels, name: unit, nameLocation: 'middle', nameGap: 26, ...base._axis },
        yAxis: { type: 'value', name: 'n', ...base._axis },
        series: res.groups.map((g, i) => ({ name: lab(g, d2) || 'all', type: 'bar', stack: 'h', data: g.hist || [], itemStyle: { color: color(th, i) } })) };
    }
    if (cfg.type === 'strend') {
      const top = [...new Set(res.groups.map((g) => lab(g, d2)))].map((s) => [s, res.groups.filter((g) => lab(g, d2) === s).reduce((a, g) => a + g.n, 0)])
        .sort((a, b) => b[1] - a[1]).slice(0, topOf(cfg, DEFAULT_TOP)).map(([s]) => s);
      return { ...base, legend: { ...base.legend, data: top }, grid: { left: 8, right: 16, top: 40, bottom: 30, containLabel: true },
        tooltip: { ...base.tooltip, trigger: 'axis' },
        xAxis: { type: 'category', data: cats, ...base._axis }, yAxis: { type: 'value', name: unit, ...base._axis },
        series: top.map((s, i) => ({ name: s, type: 'line', connectNulls: true, symbolSize: 6, itemStyle: { color: color(th, i) },
          data: cats.map((c) => { const g = pick(c, s); return g ? g[stat] : null; }) })) };
    }
    const yCat = { type: 'category', data: shown, inverse: true, ...base._axis, axisLabel: { ...base._axis.axisLabel, fontFamily: th.sans, color: th.text, width: 220, overflow: 'truncate' } };
    const series = serNames.map((s, i) => (cfg.type === 'sbox'
      ? { name: s, type: 'boxplot', boxWidth: [4, 12], itemStyle: { color: 'transparent', borderColor: color(th, i), borderWidth: 1.5 },
        data: shown.map((c) => { const g = pick(c, s); return g ? [g.min, g.p25, g.median, g.p75, g.max] : []; }) }
      : { name: s, type: 'bar', barMaxWidth: 12, itemStyle: { color: color(th, i) },
        data: shown.map((c) => { const g = pick(c, s); return g ? g[stat] : null; }) }));
    const positive = series.flatMap((s) => s.data.flat()).filter((v) => typeof v === 'number' && v > 0);
    const logMin = positive.length ? 10 ** Math.floor(Math.log10(Math.min(...positive))) : 1;
    return { ...base, legend: { ...base.legend, data: serNames }, grid: { left: 8, right: 20, top: 40, bottom: 40, containLabel: true },
      tooltip: { ...base.tooltip, trigger: 'item' },
      xAxis: { type: cfg.type === 'sbox' ? 'log' : 'value', ...(cfg.type === 'sbox' ? { min: logMin, logBase: 10 } : {}),
        name: `${unit}${cfg.type === 'sbox' ? ' (log)' : ''}`, nameLocation: 'middle', nameGap: 24, ...base._axis },
      yAxis: yCat, series };
  };
  return { title: title + truncated, option, csv, summary, unit };
}

async function buildDrl(cfg, q) {
  const res = await drlCompare({ q: query(cfg, q), set: cfg.set || 'ro-ms838-2025', min_n: 1 });
  for (const r of res.rows) {  // DAP rows in the shown unit; the ratios do not change
    const s = shownUnit(r.unit);
    if (!s) continue;
    if (typeof r.reference === 'number') r.reference *= s[0];
    for (const g of r.groups) scaleKeys(g, STAT_KEYS, s[0]);
    r.unit = unitLabel(s[1]);
  }
  const all = res.rows.filter((r) => r.groups.length && r.groups[0].ratio != null);
  const lim = topOf(cfg, DEFAULT_TOP);  // the highest median / DRL ratios, kept in the server's order
  const keep = all.length > lim ? new Set([...all].sort((a, b) => b.groups[0].ratio - a.groups[0].ratio).slice(0, lim)) : null;
  const rows = keep ? all.filter((r) => keep.has(r)) : all;
  const labels = rows.map((r) => `${r.exam_meaning} · ${r.band_label || 'all'} · ${r.quantity}`);
  const csv = [['exam', 'group', 'quantity', 'n', 'median', 'drl', 'unit', 'median_over_drl', 'status']]
    .concat(all.map((r) => [r.exam, r.band_label, r.quantity, r.groups[0].n, r.groups[0].median, r.reference, r.unit, r.groups[0].ratio, r.groups[0].status]));
  const summary = all.map((r) => ({ label: `${r.exam} · ${r.band_label || 'all'} · ${r.quantity}`, n: r.groups[0].n, median: r.groups[0].median, p75: r.groups[0].p75, mean: r.groups[0].mean, drl: r.reference }));
  const option = (th) => {
    const base = baseOption(th);
    const tone = { below: th.ok, above: th.warn, well_above: th.alert, insufficient: th.muted };
    return { ...base, grid: { left: 8, right: 24, top: 20, bottom: 40, containLabel: true },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => { const r = rows[p.dataIndex]; const g = r.groups[0];
        return `${labels[p.dataIndex]}<br>median ${smart(g.median)} / DRL ${smart(r.reference)} ${r.unit} = ${num(g.ratio * 100)}% · n ${g.n}${g.status === 'insufficient' ? ' (n small)' : ''}`; } },
      xAxis: { type: 'value', min: 0, max: (v) => Math.max(200, Math.ceil(v.max / 50) * 50), name: '% of DRL (median)', nameLocation: 'middle', nameGap: 24, ...base._axis },
      yAxis: { type: 'category', data: labels, inverse: true, ...base._axis, axisLabel: { ...base._axis.axisLabel, fontFamily: th.sans, color: th.text, width: 260, overflow: 'truncate' } },
      series: [{ type: 'bar', barMaxWidth: 10, data: rows.map((r) => ({ value: r.groups[0].ratio * 100, itemStyle: { color: tone[r.groups[0].status] || th.muted } })),
        markLine: { silent: true, symbol: 'none', data: [{ xAxis: 100 }, { xAxis: 150 }], lineStyle: { color: th.ref, type: 'dashed' }, label: { formatter: (p) => `${p.value}%`, color: th.ref, position: 'end' } } }] };
  };
  return { title: `Median vs ${DRL_SETS[cfg.set || 'ro-ms838-2025']} · ${MODALITIES[cfg.modality || 'CT']} · ${keep ? `top ${rows.length} of ${all.length}` : rows.length} exam groups`, option, csv, summary,
    height: String(Math.max(280, 26 * rows.length + 80)) };
}

export function serverSummary(list, unit) {
  if (!list || !list.length) return null;
  return h('details', { class: 'panel-summary' },
    h('summary', { class: 'muted', style: { cursor: 'pointer', 'font-size': '12px' } }, `Summary table (${list.length} rows)`),
    h('div', { class: 'panel-scroll' }, h('table', { class: 'stats' },
      h('thead', {}, h('tr', {}, h('th', { class: 'l' }, 'Group'), ['n', 'Median', 'P75', 'Mean', ...(list[0].drl !== undefined ? ['DRL'] : [])].map((x) => h('th', {}, x)))),
      h('tbody', {}, list.map((s) => h('tr', {}, h('td', { class: 'l' }, s.label), h('td', {}, num(s.n)), h('td', {}, smart(s.median)),
        h('td', {}, smart(s.p75)), h('td', {}, smart(s.mean)), s.drl !== undefined ? h('td', {}, smart(s.drl)) : null))))),
    unit ? h('p', { class: 'muted small' }, `Unit: ${unit}`) : null);
}
