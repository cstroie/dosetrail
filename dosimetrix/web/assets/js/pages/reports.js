// Regulatory reports. First: the quarterly centralised report of Order MS 1255/2016 (INSP),
// radiology and CT sheets. The XLSX keeps the official column order so the numeric block
// can be pasted into the INSP workbook.

import '../components/dt-shell.js';
import { confirmDialog } from '../core/confirm.js';
import { h, clear, append, toast } from '../core/dom.js';
import { num, smart } from '../core/format.js';
import { t } from '../core/i18n.js';
import { prefs } from '../core/prefs.js';
import { exportList, exportRun, specUrl, actionList, addAction, editAction, deleteAction, art12Report, art12Url, inspReport, inspReportUrl, surveyUrl, drlTrend, drlTrendUrl, gridReport, gridReportUrl, operatorReport, operatorReportUrl } from '../core/api.js';

const page = document.getElementById('page');
const today = new Date();
const lastQuarter = Math.floor(today.getMonth() / 3) || 4;
const state = { year: lastQuarter === 4 && today.getMonth() < 3 ? today.getFullYear() - 1 : today.getFullYear(), quarter: lastQuarter, estimated: false };

// The API (and the XLSX) keep the official Romanian form wording; the page translates it.
const sheetTitle = (s) => t(`rep.sheet.${s.key}`) === `rep.sheet.${s.key}` ? s.title : t(`rep.sheet.${s.key}`);
const Q_KEYS = { 'DAP mediu': 'dap', 'DLP mediu': 'dlp', 'Doza medie în punctul de referință': 'kar', 'ESAK sau MGD': 'esak', 'Kerma în aer': 'kerma', DAP: 'dapd' };
const qLabel = (q) => (Q_KEYS[q.label] ? t(`rep.q.${Q_KEYS[q.label]}`) : q.label);

// One report at a time, chosen by whom it goes to (the choice is kept between visits). The list
// is the server's: every spec file it knows is a target, so a dropped file appears with no code.
// The reports that have a richer page of their own keep it, keyed by their spec id; any other
// spec gets the generic parameters, preview and download.
const BESPOKE = { 'ro-insp-1255-quarterly': 'insp', 'ro-insp-1255-art12': 'art12',
  'ro-ms1245-drl-trend': 'trend', 'dosimetrix-operator-doses': 'operators',
  'dosimetrix-icrp135-grid': 'grid', 'ro-sdnp-paediatric-survey': 'survey' };
const LEGACY = Object.fromEntries(Object.entries(BESPOKE).map(([id, k]) => [k, id]));  // old ?r=insp links
let targets = [];  // [{ id, label }]

function chooseTarget() {
  const wanted = new URLSearchParams(location.search).get('r');
  const ids = targets.map((x) => x.id);
  const pick = [LEGACY[wanted] || wanted, prefs.get('reports.target'), LEGACY[prefs.get('reports.target')]].find((x) => ids.includes(x));
  state.target = pick || ids[0];
}

function render() {
  const years = [];
  for (let y = today.getFullYear(); y >= today.getFullYear() - 8; y -= 1) years.push(y);
  const section = BESPOKE[state.target] || 'specs';
  const reload = () => ({ specs: loadSpecs, insp: load, art12: loadArt12, trend: loadTrend, grid: loadGrid, operators: loadOperators })[section]?.();
  const targetSel = h('select', { class: 'input', 'aria-label': t('rep.target'), onchange: (e) => {
    state.target = e.target.value;
    prefs.set('reports.target', state.target);
    render();
  } }, targets.map((x) => h('option', { value: x.id, selected: x.id === state.target }, x.label)));
  const yearSel = h('select', { class: 'input', 'aria-label': 'Year', onchange: (e) => { state.year = Number(e.target.value); reload(); } },
    years.map((y) => h('option', { value: y, selected: y === state.year }, String(y))));
  const qSel = h('select', { class: 'input', 'aria-label': 'Quarter', onchange: (e) => { state.quarter = Number(e.target.value); reload(); } },
    [1, 2, 3, 4].map((q) => h('option', { value: q, selected: q === state.quarter }, `T${q}`)));
  const xlsx = (url) => h('button', { class: 'btn', type: 'button', onclick: () => {
    const href = url();
    if (!href) { toast(t('rep.needserver')); return; }
    h('a', { href, download: '' }).click();
    toast(t('rep.started'));
  } }, t('rep.xlsx'));
  const sections = {
    insp: () => [
      h('div', { class: 'toolbar' }, yearSel, qSel,
        h('button', { class: 'btn primary', type: 'button', onclick: load }, t('rep.preview')),
        xlsx(() => inspReportUrl({ year: state.year, quarter: state.quarter })),
        h('span', { class: 'muted small' }, t('rep.insp.hint'))),
      h('div', { id: 'rep' }), h('div', { id: 'plan' })],
    art12: () => [
      h('div', { class: 'toolbar' }, yearSel,
        h('button', { class: 'btn primary', type: 'button', onclick: loadArt12 }, t('rep.preview')),
        h('button', { class: 'btn', type: 'button', onclick: () => {
          const href = art12Url({ year: state.year });
          if (!href) { toast(t('rep.needserver')); return; }
          h('a', { href, download: '' }).click();
          toast(t('rep.started'));
        } }, t('rep.zip')),
        h('span', { class: 'muted small' }, t('rep.art12.hint'))),
      h('div', { id: 'art12' })],
    trend: () => [
      h('div', { class: 'toolbar' }, yearSel, qSel,
        h('button', { class: 'btn primary', type: 'button', onclick: loadTrend }, t('rep.trend.btn')),
        xlsx(() => drlTrendUrl({ year: state.year, quarter: state.quarter })),
        h('span', { class: 'muted small' }, t('rep.trend.hint'))),
      h('div', { id: 'trend' })],
    operators: () => [
      h('div', { class: 'toolbar' }, yearSel, qSel,
        h('button', { class: 'btn primary', type: 'button', onclick: loadOperators }, t('rep.preview')),
        xlsx(() => operatorReportUrl({ year: state.year, quarter: state.quarter })),
        h('span', { class: 'muted small' }, t('rep.operators.hint'))),
      h('div', { id: 'operators' })],
    grid: () => [
      h('div', { class: 'toolbar' }, yearSel, qSel,
        h('button', { class: 'btn primary', type: 'button', onclick: loadGrid }, t('rep.grid.btn')),
        xlsx(() => gridReportUrl({ year: state.year, quarter: state.quarter, estimated: state.estimated })),
        h('label', { class: 'row', title: 'Missing or implausible weights replaced by the median weight for age and sex (WHO to 10 y, CDC 2000 to 20 y). Off: those body exams count as "Weight unknown".' },
          h('input', { type: 'checkbox', checked: state.estimated, onchange: (e) => { state.estimated = e.target.checked; loadGrid(); } }),
          t('rep.grid.est')),
        h('span', { class: 'muted small' }, t('rep.grid.hint'))),
      h('div', { id: 'grid' })],
    specs: () => [h('div', { id: 'specs' })],
    survey: () => [
      surveyBar(),
      h('p', { class: 'muted small' }, t('rep.central'), h('a', { href: 'survey-central.html' }, t('rep.central.link')))],
  };
  const title = targets.find((x) => x.id === state.target)?.label || '';
  clear(page).append(h('div', { class: 'toolbar' }, h('span', { class: 'muted' }, t('rep.target')), targetSel),
    h('h1', { class: 'section-title' }, title), ...sections[section]());
  reload();
}

async function boot() {
  const lang = document.documentElement.lang === 'ro' ? 'ro' : 'en';
  try {
    const list = await exportList();
    targets = list.specs.filter((s) => s.allowed).map((s) => ({ id: s.id, label: `${s.title[lang] || s.title.en || s.id}${s.country ? ` · ${s.country}` : ''}` }));
  } catch { /* sample mode, or no server: the fixed list below */ }
  if (!targets.length) targets = Object.entries(LEGACY).map(([k, id]) => ({ id, label: t(`rep.to.${k}`) }));
  chooseTarget();
  render();
}

// Drop-in report specs (docs/specs.md): the list, the parameters and the preview all come from
// the spec files, so a new file on the server adds an export here with no code.
const specState = { id: null, params: {} };

async function loadSpecs() {
  const host = document.getElementById('specs');
  clear(host).append(h('p', { class: 'muted' }, t('rep.loading')));
  let list;
  try { list = await exportList(); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const lang = document.documentElement.lang === 'ro' ? 'ro' : 'en';
  const specs = list.specs.filter((s) => s.allowed);
  if (!specs.length) { clear(host).append(h('p', { class: 'empty' }, t('rep.none')), ...problemsNote(list)); return; }
  const spec = specs.find((s) => s.id === state.target) || specs[0];
  if (specState.id !== spec.id) { specState.id = spec.id; specState.params = {}; }
  const p = specState.params;
  const input = (name, type, extra = {}) => {
    const el = h('input', { class: 'input', type, 'aria-label': name, value: p[name] ?? extra.value ?? '', ...extra.attrs,
      onchange: (e) => { p[name] = e.target.value; } });
    if (p[name] === undefined && extra.value !== undefined) p[name] = extra.value;
    return el;
  };
  const controls = [];
  const period = (spec.params.period || {}).type || 'none';
  const yr = today.getFullYear();
  const years = Array.from({ length: 9 }, (_, i) => yr - i);
  const sel = (name, options, value) => {
    if (p[name] === undefined) p[name] = String(value);
    return h('select', { class: 'input', 'aria-label': name, onchange: (e) => { p[name] = e.target.value; } },
      options.map(([v, l]) => h('option', { value: v, selected: String(p[name]) === String(v) }, l)));
  };
  if (['year', 'quarter'].includes(period)) controls.push(sel('year', years.map((y) => [y, String(y)]), yr));
  if (period === 'quarter') controls.push(sel('quarter', [1, 2, 3, 4].map((q) => [q, `T${q}`]), Math.ceil((today.getMonth() + 1) / 3)));
  if (period === 'month') controls.push(input('month', 'month', { value: `${yr}-${String(today.getMonth() + 1).padStart(2, '0')}` }));
  if (period === 'range') {
    const first = new Date(yr, today.getMonth() - 1, 1);
    const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    controls.push(input('from', 'date', { value: iso(first) }), input('to', 'date', { value: iso(new Date(yr, today.getMonth(), 0)) }));
  }
  for (const [name, def] of Object.entries(spec.params)) {
    if (name === 'period') continue;
    controls.push(input(name, def.type === 'int' || def.type === 'float' ? 'number' : 'text', { value: def.default, attrs: { placeholder: name, class: name === 'q' ? 'input w-260' : 'input' } }));
  }
  const run = (format) => () => {
    if (format === 'json') { preview(); return; }
    const href = specUrl(spec.id, p, format);
    if (!href) { toast(t('rep.needserver')); return; }
    h('a', { href, download: '' }).click();
    toast(t('rep.started'));
  };
  const out = h('div', { id: 'spec-out' });
  async function preview() {
    clear(out).append(h('p', { class: 'muted' }, t('rep.loading')));
    try { drawSpec(out, await exportRun(spec.id, p), lang); } catch (e) { clear(out).append(h('p', { class: 'error' }, e.message)); }
  }
  clear(host).append(
    h('div', { class: 'toolbar' }, ...controls, h('button', { class: 'btn primary', type: 'button', onclick: run('json') }, t('rep.preview')),
      spec.formats.filter((f) => f !== 'json').map((f) => h('button', { class: 'btn', type: 'button', onclick: run(f) }, f.toUpperCase()))),
    h('p', { class: 'muted small' }, [spec.authority, spec.legal, `v${spec.version}`].filter(Boolean).join(' · '),
      spec.needs_pid ? h('span', { class: 'tag warn ml-1', title: t('rep.spec.pid') }, 'pid') : null),
    out, ...problemsNote(list));
}

function problemsNote(list) {
  return (list.problems || []).length ? [h('details', { class: 'mt-4' }, h('summary', { class: 'muted small' }, t('rep.spec.problems').replace('{n}', list.problems.length)),
    list.problems.map((x) => h('p', { class: 'error small mono' }, `${x.file}: ${x.error}`)))] : [];
}

function drawSpec(host, r, lang) {
  clear(host);
  const MAX = 200;
  for (const b of r.books) {
    for (const s of b.sheets) {
      const rows = s.rows.slice(0, MAX);
      append(host, [h('h2', { class: 'section-title mt-4' }, r.books.length > 1 ? `${b.title || b.filename} · ${s.name}` : s.name,
        h('span', { class: 'muted small ml-1' }, `${num(s.rows.length)} ${t('rep.spec.rows')}`)),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll panel-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, s.columns.map((c) => h('th', {}, c)))),
        h('tbody', {}, rows.length ? rows.map((row) => h('tr', {}, row.map((v) => h('td', { class: typeof v === 'number' ? 'r mono' : '' }, v == null ? '' : typeof v === 'number' ? smart(v) : String(v)))))
          : h('tr', {}, h('td', { colspan: s.columns.length, class: 'empty' }, t('rep.none')))))),
      s.rows.length > MAX ? h('p', { class: 'muted small' }, t('rep.spec.cut').replace('{n}', MAX)) : null)]);
    }
  }
}

// Art. 12: 20 patients per examination and device, February and March, due 15 April.
async function loadArt12() {
  const host = document.getElementById('art12');
  clear(host).append(h('p', { class: 'muted' }, t('rep.loading')));
  let r;
  try { r = await art12Report({ year: state.year }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  clear(host).append(h('p', { class: 'muted small' }, t('rep.art12.window').replace('{from}', r.window[0]).replace('{to}', r.window[1]).replace('{n}', r.per_sheet)));
  if (!r.workbooks.length) { host.append(h('p', { class: 'empty' }, t('rep.none'))); return; }
  for (const w of r.workbooks) {
    const rows = w.sheets.map((s) => h('tr', {}, h('td', {}, s.name), h('td', { class: 'r mono' }, `${s.patients} / ${r.per_sheet}`),
      h('td', { class: 'r mono' }, num(s.available)), h('td', {}, s.missing ? h('span', { class: 'tag warn' }, t('rep.art12.missing').replace('{n}', s.missing)) : h('span', { class: 'tag ok' }, 'OK'))));
    append(host, [h('h2', { class: 'section-title mt-4' }, `${w.title} · ${w.device}`, h('span', { class: 'muted small mono ml-1' }, w.file)),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll panel-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, h('th', {}, t('rep.examination')), h('th', { class: 'r' }, t('rep.art12.patients')), h('th', { class: 'r' }, t('rep.art12.available')), h('th', {}, ''))),
        h('tbody', {}, rows))))]);
  }
}

async function loadTrend() {
  const host = document.getElementById('trend');
  clear(host).append(h('p', { class: 'muted' }, t('rep.loading')));
  let r;
  try { r = await drlTrend({ year: state.year, quarter: state.quarter }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const cell = (x) => {
    if (!x) return h('td', { class: 'r muted' }, '—');
    const cls = x.mean_over_drl > 1.5 ? 'alert' : x.mean_over_drl > 1 ? 'warn' : 'ok';
    return h('td', { class: 'r', title: `n ${x.n} · mean ${smart(x.mean)} · median ${smart(x.median)}` },
      h('span', { class: `tag ${cls}` }, `${num(x.mean_over_drl, 2)}×`), h('br'), h('span', { class: 'muted small' }, `med ${smart(x.median)} · n ${x.n}`));
  };
  clear(host).append(h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, h('th', {}, t('rep.exam')), h('th', {}, t('rep.group')), h('th', { class: 'r' }, t('rep.drl')),
      r.quarters.map((q) => h('th', { class: 'r' }, q)), h('th', { class: 'r' }, t('rep.above')), h('th', { class: 'r' }, t('rep.trendcol')))),
    h('tbody', {}, r.rows.map((x) => h('tr', { class: x.last_above ? 'hot' : '' },
      h('td', { title: x.exam }, x.exam_meaning), h('td', {}, x.band_label || t('rep.all')),
      h('td', { class: 'r' }, `${smart(x.reference)} ${x.unit}`), x.quarters.map(cell),
      h('td', { class: 'r' }, `${x.quarters_above}/${x.quarters_with_data}`),
      h('td', { class: 'r' }, x.trend_pct_per_quarter == null ? '' : `${x.trend_pct_per_quarter > 0 ? '+' : ''}${num(x.trend_pct_per_quarter, 0)} %/q`)))))))); 
}

async function loadOperators() {
  const host = document.getElementById('operators');
  clear(host).append(h('p', { class: 'muted' }, t('rep.loading')));
  let r;
  try { r = await operatorReport({ year: state.year, quarter: state.quarter }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const pct = (x) => `${num(x * 100, 0)} %`;
  const share = r.studies ? r.studies_with_operator / r.studies : 0;
  const tone = (o) => (o.flag ? 'alert' : o.median_ratio >= 1.15 ? 'warn' : 'ok');
  clear(host).append(
    h('p', { class: 'muted small' }, t('rep.operators.about')
      .replace('{start}', r.start).replace('{end}', r.end).replace('{n}', r.min_n)
      .replace('{ratio}', num(r.ratio_flag, 1)).replace('{p95}', pct(r.p95_flag))),
    h('div', { class: 'kpis drl-kpis' },
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, t('rep.operators.k.cov')), h('span', { class: 'v' }, pct(share)),
        h('span', { class: 'm' }, `${num(r.studies_with_operator)} / ${num(r.studies)}`)),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, t('rep.operators.k.ops')), h('span', { class: 'v' }, num(r.operators.filter((o) => o.enough).length)),
        h('span', { class: 'm' }, t('rep.operators.k.ops.m').replace('{n}', r.min_n))),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, t('rep.operators.k.flag')), h('span', { class: 'v' }, num(r.operators.filter((o) => o.flag).length)),
        h('span', { class: 'm' }, t('rep.operators.k.flag.m')))));
  const body = h('tbody', {}, r.operators.map((o) => h('tr', { class: o.enough ? '' : 'muted' },
    h('td', {}, o.operator), h('td', { class: 'r' }, num(o.n)), h('td', { class: 'r' }, num(o.studies)),
    h('td', { class: 'r' }, o.enough ? h('span', { class: `tag ${tone(o)}` }, `${num(o.median_ratio, 2)}×`) : `${num(o.median_ratio, 2)}×`),
    h('td', { class: 'r' }, pct(o.share_p95)),
    h('td', { class: 'small' }, o.exams.slice(0, 3).map((x) => `${x.exam} ${x.band} ${num(x.median_ratio, 2)}× (${x.n})`).join(' · ')),
    h('td', { class: 'small muted' }, o.devices.join(', ')))));
  if (!r.operators.length) body.append(h('tr', {}, h('td', { colspan: 7, class: 'empty' }, t('rep.operators.none'))));
  append(host, [h('h2', { class: 'section-title mt-5' }, t('rep.operators.table')),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, t('rep.operators.cols').split('|').map((c, i) => h('th', { class: i >= 1 && i <= 4 ? 'r' : '' }, c)))), body))),
    h('h2', { class: 'section-title mt-5' }, t('rep.operators.coverage')),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, t('rep.operators.covcols').split('|').map((c, i) => h('th', { class: i ? 'r' : '' }, c)))),
      h('tbody', {}, r.coverage.map((c) => h('tr', {}, h('td', {}, c.device), h('td', { class: 'r' }, num(c.studies)),
        h('td', { class: 'r' }, num(c.with_operator)), h('td', { class: 'r' }, pct(c.share))))))))]);
}

async function loadGrid() {
  const host = document.getElementById('grid');
  clear(host).append(h('p', { class: 'muted' }, t('rep.loading')));
  let r;
  try { r = await gridReport({ year: state.year, quarter: state.quarter, estimated: state.estimated }); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const tone = { below: 'ok', above: 'warn', well_above: 'alert', insufficient: '' };
  const label = { below: '≤ EDRL', above: '≤ 1.5×', well_above: '> 1.5×', insufficient: `n < ${r.min_n}` };
  const n = r.notes;
  clear(host).append(h('p', { class: 'muted small' }, `${r.start} – ${r.end} · body values: ${num(n.body_values)}, of which weight estimated (median for age and sex, WHO / CDC) ${num(n.body_weight_estimated || 0)}, no weight ${num(n.body_weight_unknown)}`
    + (n.body_values && n.body_weight_unknown / n.body_values > 0.5 ? ' — the weight grid needs weights (RIS or entered at the modality).' : '')
    + (r.estimated_weights ? '. Estimated weights fill the weight groups; they are never measured values.' : '. Estimated weights left out: only reported weights place body exams in a group.')));
  for (const [param, title] of [['age', t('rep.head')], ['weight', t('rep.body')]]) {
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
    if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 11, class: 'empty' }, t('rep.none'))));
    append(host, [h('h2', { class: 'section-title mt-5' }, title),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, t('rep.cols').split('|').map((c, i) => h('th', { class: i >= 3 ? 'r' : '' }, c)))), body)))]);
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
    } }, t('rep.survey.dl')),
    h('span', { class: 'muted small' }, t('rep.survey.hint')));
}

// Art. 7(3): a mean needs 30 patients per device and examination type.
function lowTitle(x, r) {
  const dev = x.devices.filter((d) => x.low_n.includes(d.id)).map((d) => `${d.name || '—'}: ${d.n}`).join(', ');
  return t('rep.low.title').replace('{n}', r.min_patients).replace('{dev}', dev);
}

// Plan of optimisation measures of the quarter on screen (art. 7(2)).
async function loadPlan() {
  const host = document.getElementById('plan');
  if (!host) return;
  let r;
  try { r = await actionList(state.year, state.quarter); } catch (e) { clear(host); return; }
  const status = (a) => h('select', { class: 'input', 'aria-label': t('plan.status'), onchange: async (e) => {
    try { await editAction(a.id, { status: e.target.value }); } catch (err) { toast(err.message); loadPlan(); }
  } }, ['open', 'doing', 'done'].map((v) => h('option', { value: v, selected: a.status === v }, t(`plan.${v}`))));
  const rows = r.rows.map((a) => h('tr', {}, h('td', {}, a.finding), h('td', {}, a.action), h('td', {}, a.owner || ''), h('td', {}, status(a)),
    h('td', {}, h('button', { class: 'btn xs ghost danger', type: 'button', onclick: async () => {
      if (!(await confirmDialog(a.action, { title: t('plan.delete'), label: t('plan.delete'), danger: true }))) return;
      try { await deleteAction(a.id); loadPlan(); } catch (err) { toast(err.message); }
    } }, t('plan.delete')))));
  const f = h('input', { class: 'input', placeholder: t('plan.finding'), 'aria-label': t('plan.finding') });
  const m = h('input', { class: 'input', placeholder: t('plan.action'), 'aria-label': t('plan.action') });
  const o = h('input', { class: 'input w-140', placeholder: t('plan.owner'), 'aria-label': t('plan.owner') });
  const add = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
    try { await addAction({ year: state.year, quarter: state.quarter, finding: f.value, action: m.value, owner: o.value }); loadPlan(); } catch (err) { toast(err.message); }
  } }, t('plan.add'));
  clear(host).append(h('h2', { class: 'section-title mt-5' }, `${t('plan.title')} · T${state.quarter} ${state.year}`),
    h('p', { class: 'muted small' }, t('plan.hint')),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll panel-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['finding', 'action', 'owner', 'status'].map((k) => h('th', {}, t(`plan.${k}`))), h('th', {}, ''))),
      h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', { colspan: 5, class: 'empty' }, t('plan.none')))))),
    h('div', { class: 'row panel-body' }, f, m, o, add)));
}

async function load() {
  loadPlan();
  const host = document.getElementById('rep');
  clear(host).append(h('p', { class: 'muted' }, t('rep.loading')));
  let r;
  try {
    r = await inspReport({ year: state.year, quarter: state.quarter, q: 'origin:local' });
  } catch (e) {
    clear(host).append(h('p', { class: 'error' }, e.message));
    return;
  }
  const kpis = h('div', { class: 'kpis drl-kpis' },
    r.sheets.filter((s) => s.rows.some((x) => x.total)).map((s) => h('div', { class: 'kpi' }, h('span', { class: 'code' }, sheetTitle(s)),
      h('span', { class: 'v' }, num(s.rows.reduce((a, x) => a + x.total, 0))),
      h('span', { class: 'm' }, t('rep.k.exams').replace('{n}', s.rows.filter((x) => x.total).length)))),
    h('div', { class: 'kpi' }, h('span', { class: 'code' }, t('rep.k.unrep')), h('span', { class: 'v' }, num(r.notes.unclassified)),
      h('span', { class: 'm' }, t('rep.k.unrep.m'))),
    h('div', { class: 'kpi' }, h('span', { class: 'code' }, t('rep.k.sex')), h('span', { class: 'v' }, num(r.notes.other_sex)),
      h('span', { class: 'm' }, t('rep.k.sex.m'))));
  clear(host).append(h('p', { class: 'muted small' }, `${r.period.from} – ${r.period.to}`), kpis);
  for (const s of r.sheets) {
    const rows = s.rows.filter((x) => x.total);
    const qs = s.quantities.filter((q) => q.key);
    const head = h('thead', {}, h('tr', {},
      h('th', {}, t('rep.code')), h('th', {}, t('rep.examination')), h('th', { class: 'r' }, t('rep.total')),
      s.count_bands.map((b) => h('th', { class: 'r' }, `${b.label} M/F`)),
      qs.map((q) => s.mean_bands.map((b) => h('th', { class: 'r' }, `${qLabel(q)} ${b.label}`)))));
    const body = h('tbody', {}, rows.map((x) => h('tr', {},
      h('td', { class: 'mono' }, x.code), h('td', {}, [x.exam, x.type].filter(Boolean).join(' — ')),
      h('td', { class: 'r' }, num(x.total), x.low_n.length ? h('span', { class: 'tag warn ml-1', title: lowTitle(x, r) }, t('rep.low')) : null),
      s.count_bands.map((b) => h('td', { class: 'r' }, `${x.counts[b.key].M || '·'} / ${x.counts[b.key].F || '·'}`)),
      qs.map((q) => s.mean_bands.map((b) => h('td', { class: 'r' }, x.means[q.key][b.key] == null ? '' : smart(x.means[q.key][b.key])))))));
    if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 40, class: 'empty' }, t('rep.none'))));
    append(host, [h('h2', { class: 'section-title mt-5' }, `${sheetTitle(s)} · ${qs.map((q) => `${qLabel(q)} (${q.unit})`).join(' · ')}`),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' }, head, body)))]);
  }
}

boot();
