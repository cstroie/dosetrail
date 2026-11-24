// Local DRLs (admin): propose from local data (ICRP 135: P75 of the room medians; one room:
// its median), choose and adopt as a reference set, review an adopted set against a new
// period (yearly), delete. Server logic: dozimetrix/localdrl.py.

import { confirmDialog } from '../core/confirm.js';
import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, toast } from '../core/dom.js';
import { num, smart, shown, shownUnit } from '../core/format.js';
import { localDrlPropose, adoptLocalDrl, deleteDrlSet, localDrlReview, localDrlReviewUrl, drlSets } from '../core/api.js';

const page = document.getElementById('page');
const QTY = { dlp: 'DLP', dlp_study: 'DLP (study)', ctdivol: 'CTDIvol', dap: 'DAP', mgd: 'MGD' };
const TONE = { below: 'ok', above: 'warn', well_above: 'alert' };
const iso = (d) => d.toISOString().slice(0, 10);
const today = new Date();
const yearAgo = new Date(today.getFullYear() - 1, today.getMonth(), today.getDate() + 1);
const state = { start: iso(yearAgo), end: iso(today), grid: 'national', min_n: 10, p75_n: 30, rule: 'pooled', scope: 'all', q: 'origin:local', estimated: false };

function field(label, input) { return h('label', { class: 'row', style: { gap: '6px' } }, h('span', { class: 'muted small' }, label), input); }

function periodBar(onRun, label) {
  const inp = (k, attrs) => h('input', { class: 'input', value: state[k], ...attrs, onchange: (e) => { state[k] = attrs.type === 'number' ? Number(e.target.value) : e.target.value; } });
  return h('div', { class: 'toolbar' },
    field('From', inp('start', { type: 'date' })), field('to', inp('end', { type: 'date' })),
    field('Selection', inp('q', { class: 'w-160', type: 'text' })),
    field('Min. values (median)', inp('min_n', { class: 'w-70', type: 'number', min: 1 })),
    field('Values for P75', inp('p75_n', { class: 'w-70', type: 'number', min: 1 })),
    h('button', { class: 'btn primary', type: 'button', onclick: onRun }, label));
}

async function render() {
  clear(page).append(adminTabs('local-drl.html'),
    h('h1', { class: 'section-title' }, 'Propose local DRLs'),
    h('p', { class: 'muted small' }, 'Per examination and patient group, all rooms pooled: the local DRL is the P75 when there are enough values, otherwise the median (P50); fewer than the minimum: no value. The ICRP 135 room rule (P75 of the room medians) can be chosen below. CT per series; radiography, fluoroscopy and mammography per study. Studies excluded from DRL statistics (weight outside P5–P95, prematurity) are left out.'),
    periodBar(propose, 'Propose'),
    h('div', { class: 'toolbar' },
      field('Rule', h('select', { class: 'input', onchange: (e) => { state.rule = e.target.value; } },
        h('option', { value: 'pooled', selected: state.rule === 'pooled' }, 'Pooled: P75 if enough values, else median'),
        h('option', { value: 'rooms', selected: state.rule === 'rooms' }, 'ICRP 135: P75 of the room medians'))),
      field('Exams', h('select', { class: 'input', onchange: (e) => { state.scope = e.target.value; } },
        h('option', { value: 'all', selected: state.scope === 'all' }, 'All, including those without a national DRL'),
        h('option', { value: 'national', selected: state.scope === 'national' }, 'Only those with a national DRL'))),
      field('Groups', h('select', { class: 'input', onchange: (e) => { state.grid = e.target.value; } },
        h('option', { value: 'national', selected: state.grid === 'national' }, 'National age groups (OMS 838/2025)'),
        h('option', { value: 'icrp', selected: state.grid === 'icrp' }, 'ICRP 135 / RP 185: head by age, body by weight'))),
      h('label', { class: 'row', title: 'Body groups by weight: include weights estimated from the growth charts (median for age and sex)' },
        h('input', { type: 'checkbox', checked: state.estimated, onchange: (e) => { state.estimated = e.target.checked; } }), 'Include estimated weights')),
    h('div', { id: 'proposal' }),
    h('h1', { class: 'section-title mt-4' }, 'Adopted local sets'),
    h('div', { id: 'sets' }));
  loadSets();
}

async function propose() {
  const host = document.getElementById('proposal');
  clear(host).append(h('p', { class: 'muted' }, 'Computing…'));
  let r;
  try { r = await localDrlPropose(state); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const rows = r.rows;
  const picks = new Map();
  const body = h('tbody');
  let last = null;
  let lastProj = null;
  for (const x of rows) {
    const { f } = shownUnit(x.unit);  // edited in the shown unit, saved in the DRL's own unit
    const value = h('input', { class: 'input w-90', type: 'number', step: 'any', value: x.value == null ? '' : +(x.value * f).toPrecision(4), disabled: x.value == null });
    const box = h('input', { type: 'checkbox', checked: x.value != null, disabled: x.value == null });
    picks.set(x, { box, value });
    const rooms = x.rooms.map((d) => `${d.device_name}: median ${shown(d.median, x.unit)} (n ${d.n})${d.counts ? '' : ' — too few'}`).join('\n');
    body.append(h('tr', {},
      h('td', {}, box),
      h('td', { title: x.exam }, x.exam === last && x.projection === lastProj ? '' : x.exam_meaning + (x.projection ? ` (${x.projection})` : ''), x.local_only && !(x.exam === last && x.projection === lastProj) ? h('span', { class: 'tag', title: 'no national or European DRL for this exam: local only' }, ' local only') : null),
      h('td', {}, x.band_label), h('td', { title: x.series_factor ? `the whole examination: about ${x.series_factor} times one series (several phases or regions); no published DRL is per examination` : '' }, (QTY[x.quantity] || x.quantity) + (x.series_factor ? ` ≈ ${x.series_factor}× series` : '')),
      h('td', { class: 'r' }, num(x.n)), h('td', { class: 'r', title: rooms }, `${x.n_rooms}/${x.rooms.length}`),
      h('td', { class: 'r' }, value), h('td', {}, shownUnit(x.unit).unit),
      h('td', { class: 'muted small' }, x.value == null && x.indicative ? `indicative (n ${x.indicative.n}): P50 ${shown(x.indicative.p50, x.unit)} · P75 ${shown(x.indicative.p75, x.unit)}` : x.method === 'p75' ? 'P75' : x.method === 'median' ? 'median (P50)' : x.method === 'p75_rooms' ? 'P75 rooms' : x.method === 'median_room' ? 'median (1 room)' : 'too few'),
      h('td', { class: 'r' }, x.reference == null ? '—' : shown(x.reference, x.unit)),
      h('td', {}, x.status ? h('span', { class: `tag ${TONE[x.status]}` }, `${num(x.ratio, 2)}×`) : '',
        x.outlier_rooms.length ? h('span', { class: 'tag warn', title: `room median over 3× or under ⅓ of the pooled median — check the classification and units before adopting: ${x.outlier_rooms.join(', ')}` }, ' room outlier') : null,
        x.n_phantom32 ? h('span', { class: 'tag warn', title: 'head CT series reported on the 32 cm (body) phantom (about half the 16 cm value): left out of the proposal' }, ` 32 cm left out: ${x.n_phantom32}`) : null)));
    last = x.exam; lastProj = x.projection;
  }
  const name = h('input', { class: 'input', value: `Local DRLs ${r.start} – ${r.end}`, style: { width: '320px' } });
  const adopt = async () => {
    const chosen = rows.filter((x) => picks.get(x).box.checked).map((x) => ({ ...x, value: Number(picks.get(x).value.value) / shownUnit(x.unit).f }));
    if (!chosen.length) { toast('Choose at least one value.'); return; }
    if (!(await confirmDialog(`Adopt ${chosen.length} values as "${name.value}"?`, { title: 'Adopt values', label: 'Adopt' }))) return;
    try {
      const res = await adoptLocalDrl({ name: name.value, grid: r.grid, start: r.start, end: r.end, min_n: r.min_n, rule: r.rule, p75_n: r.p75_n, q: r.q, estimated: r.estimated_weights, rows: chosen });
      toast(`Adopted: ${res.name} (${res.values} values)`); loadSets();
    } catch (e) { toast(e.message); }
  };
  const all = (on) => { for (const [x, p] of picks) if (x.value != null) p.box.checked = on; };
  clear(host).append(
    h('p', { class: 'muted small' }, `${r.start} – ${r.end} · ${rows.length} groups with data, ${rows.filter((x) => x.value != null).length} with a proposal · reference: ${r.reference_kind}. Hover the rooms column for each room's median. Values can be edited before adopting.`),
    h('div', { class: 'toolbar' }, h('button', { class: 'btn xs', type: 'button', onclick: () => all(true) }, 'All'), h('button', { class: 'btn xs', type: 'button', onclick: () => all(false) }, 'None'),
      name, h('button', { class: 'btn primary', type: 'button', onclick: adopt }, 'Adopt selected')),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['', 'Exam', 'Group', 'Qty', 'n', 'Rooms', 'Local DRL', 'Unit', 'Method', 'Reference', 'vs ref.'].map((c, i) => h('th', { class: [4, 5, 6, 9].includes(i) ? 'r' : '' }, c)))),
      body))));
}

async function loadSets() {
  const host = document.getElementById('sets');
  let sets;
  try { sets = (await drlSets()).filter((s) => s.key.startsWith('local-')); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  if (!sets.length) { clear(host).append(h('p', { class: 'muted' }, 'None yet. Propose, choose, adopt.')); return; }
  clear(host).append(h('div', { class: 'panel' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, ['Name', 'Adopted', 'Values', ''].map((c) => h('th', {}, c)))),
    h('tbody', {}, sets.map((s) => h('tr', {},
      h('td', { title: `${s.source || ''}\n${s.notes || ''}` }, s.name), h('td', {}, s.valid_from || ''), h('td', { class: 'r' }, num(s.values)),
      h('td', { class: 'row' },
        h('button', { class: 'btn xs', type: 'button', onclick: () => review(s) }, 'Review…'),
        h('a', { class: 'btn xs ghost', href: `drl.html?set=${encodeURIComponent(s.key)}` }, 'Compare'),
        h('button', { class: 'btn xs ghost danger', type: 'button', onclick: async () => {
          if (!(await confirmDialog(`Delete "${s.name}"? Comparisons and reports can no longer use it.`, { title: 'Delete set', label: 'Delete', danger: true }))) return;
          try { await deleteDrlSet(s.key); loadSets(); } catch (e) { toast(e.message); }
        } }, 'Delete'))))))),
  h('div', { id: 'review' }));
}

async function review(set) {
  const host = document.getElementById('review');
  const run = async () => {
    out.replaceChildren(h('p', { class: 'muted' }, 'Computing…'));
    const params = { set: set.key, q: state.q, start: state.start, end: state.end, min_n: state.min_n };
    let r;
    try { r = await localDrlReview(params); } catch (e) { out.replaceChildren(h('p', { class: 'error' }, e.message)); return; }
    const change = (p) => (p == null ? '—' : h('span', { class: `tag ${p > 10 ? 'warn' : p < -10 ? 'ok' : ''}` }, `${p > 0 ? '+' : ''}${num(p, 0)} %`));
    out.replaceChildren(
      h('div', { class: 'toolbar' }, h('a', { class: 'btn', href: localDrlReviewUrl(params), download: '' }, 'Download XLSX'),
        h('span', { class: 'muted small' }, `${r.start} – ${r.end}. "Rooms above": rooms whose median exceeds the local DRL — look at their protocols first.`)),
      h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
        h('thead', {}, h('tr', {}, ['Exam', 'Group', 'Qty', 'Local DRL', 'Now', 'Change', 'n', 'Rooms above', 'Reference', 'Now vs ref.'].map((c) => h('th', {}, c)))),
        h('tbody', {}, r.rows.map((x) => h('tr', { class: x.rooms_above.length ? 'hot' : '' },
          h('td', { title: x.exam }, x.exam_meaning), h('td', {}, x.band_label || 'all'), h('td', {}, QTY[x.quantity] || x.quantity),
          h('td', { class: 'r' }, `${shown(x.local_drl, x.unit)} ${shownUnit(x.unit).unit}`), h('td', { class: 'r' }, x.new_value == null ? '—' : shown(x.new_value, x.unit)),
          h('td', { class: 'r' }, change(x.change_pct)), h('td', { class: 'r' }, num(x.n)),
          h('td', { class: 'small' }, x.rooms_above.map((d) => `${d.device_name} ${shown(d.median, x.unit)}`).join(', ')),
          h('td', { class: 'r' }, x.reference == null ? '—' : shown(x.reference, x.unit)),
          h('td', {}, x.status ? h('span', { class: `tag ${TONE[x.status]}` }, x.status.replace('_', ' ')) : ''))))))));
  };
  const out = h('div');
  clear(host).append(h('h2', { class: 'section-title mt-3' }, `Review: ${set.name}`), periodBar(run, 'Review this period'), out);
  run();
}

render();
