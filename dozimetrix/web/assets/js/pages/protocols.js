// Protocol comparison: the protocols of one CT region side by side, per ICRP 135 patient group
// (head by age, body by weight): distributions of dose and technique, dose rank. The ranking is
// by dose only; image quality (noise, slice thickness) is not in the dose data, so the technique
// columns are there for a radiologist to judge. Server logic: dozimetrix/protocols.py.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear } from '../core/dom.js';
import { num } from '../core/format.js';
import { codes, protocolsCompare } from '../core/api.js';

const page = document.getElementById('page');
const iso = (d) => d.toISOString().slice(0, 10);
const today = new Date();
const yearAgo = new Date(today.getFullYear() - 1, today.getMonth(), today.getDate() + 1);
const state = {
  region: 'CT-CAP', system: 'ro-drl-ms838-2025', grid: 'icrp', by: 'protocol', by_device: true,
  min_n: 10, estimated: false, q: 'origin:local', start: iso(yearAgo), end: iso(today),
};
const d1 = (v) => (v == null ? '—' : num(v, v >= 100 ? 0 : v >= 10 ? 1 : 2));
const field = (label, input) => h('label', { class: 'row', style: { gap: '6px' } }, h('span', { class: 'muted small' }, label), input);
const select = (key, options, onchange) => h('select', { class: 'input', onchange: (e) => { state[key] = e.target.value; onchange?.(); } },
  options.map(([value, label]) => h('option', { value, selected: String(state[key]) === String(value) }, label)));

/** A small CTDIvol histogram on the group's own scale, so bars are comparable across rows. */
function hist(hs) {
  if (!hs?.counts?.length) return '';
  const max = Math.max(...hs.counts, 1);
  const w = 6, H = 22, gap = 1;
  const svg = h('svg', { width: hs.counts.length * (w + gap), height: H, viewBox: `0 0 ${hs.counts.length * (w + gap)} ${H}`, role: 'img', 'aria-label': 'CTDIvol distribution' },
    hs.counts.map((c, i) => h('rect', { x: i * (w + gap), y: H - Math.max(c ? 1 : 0, (c / max) * H), width: w, height: Math.max(c ? 1 : 0, (c / max) * H), fill: 'currentColor', opacity: 0.55 })));
  svg.setAttribute('title', `${num(hs.edges[0], 1)} – ${num(hs.edges.at(-1), 1)} mGy`);
  return h('span', { class: 'muted', title: `CTDIvol ${num(hs.edges[0], 1)} – ${num(hs.edges.at(-1), 1)} mGy` }, svg);
}

function table(rows, byDevice) {
  const med = (r, k) => d1(r.stats[k]?.median);
  const head = ['#', 'Protocol', ...(byDevice ? ['Room'] : []), 'n', 'CTDIvol P25 · median · P75', '× lowest', 'DLP', 'kV', 'eff. mAs', 'Pitch', 'Coll. mm', 'Length mm', 'CTDIvol'];
  return h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, head.map((c, i) => h('th', { class: i === 0 || (i >= (byDevice ? 3 : 2) && i < head.length - 1) ? 'r' : '' }, c)))),
    h('tbody', {}, rows.map((r) => {
      const c = r.stats.ctdivol;
      const names = r.names.length > 1 || r.names[0] !== r.protocol ? `Spelled: ${r.names.join(', ')}` : '';
      const notes = [r.n_from_name ? `${r.n_from_name} series: group read from the protocol name` : '', r.n_name_mismatch ? `${r.n_name_mismatch} series: patient's group differs from the one in the name` : ''].filter(Boolean);
      return h('tr', { class: r.enough ? '' : 'muted' },
        h('td', { class: 'r' }, r.rank ?? ''),
        h('td', { title: [names, ...notes].filter(Boolean).join('\n') }, r.protocol, r.rank === 1 ? h('span', { class: 'tag ok', title: 'lowest median CTDIvol in this group — dose only, not image quality' }, ' lowest') : '',
          r.n_from_name ? h('span', { class: 'tag', title: notes[0] }, ' name') : '', r.n_name_mismatch ? h('span', { class: 'tag warn', title: notes.at(-1) }, ' mismatch') : ''),
        ...(byDevice ? [h('td', {}, r.device_name || '')] : []),
        h('td', { class: 'r' }, num(r.n)),
        h('td', { class: 'r' }, `${d1(c.p25)} · ${d1(c.median)} · ${d1(c.p75)}`),
        h('td', { class: 'r' }, r.ratio_to_lowest == null ? '' : `${num(r.ratio_to_lowest, 2)}×`),
        h('td', { class: 'r' }, med(r, 'dlp')), h('td', { class: 'r' }, med(r, 'kvp')), h('td', { class: 'r' }, med(r, 'emas')),
        h('td', { class: 'r' }, r.stats.pitch ? num(r.stats.pitch.median, 2) : '—'), h('td', { class: 'r' }, med(r, 'collimation_mm')), h('td', { class: 'r' }, med(r, 'scan_length_mm')),
        h('td', {}, hist(r.hist)));
    })))));
}

async function run() {
  const host = document.getElementById('result');
  clear(host).append(h('p', { class: 'muted' }, 'Computing…'));
  let r;
  try { r = await protocolsCompare(state); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const groups = new Map();
  for (const row of r.rows) {
    const key = `${row.band}|${row.phantom_cm}`;
    if (!groups.has(key)) groups.set(key, { label: row.band_label, phantom: row.phantom_cm, rows: [] });
    groups.get(key).rows.push(row);
  }
  clear(host).append(
    h('p', { class: 'muted small' }, `${r.meaning} (${r.region}) · ${r.start} – ${r.end} · ${r.rows.length} protocol rows in ${groups.size} groups · groups by ${r.parameter}. Rows with fewer than ${r.min_n} series are greyed and not ranked.`),
    h('p', { class: 'muted small' }, r.note),
    ...[...groups.values()].map((g) => h('section', { class: 'mt-3' },
      h('h2', { class: 'section-title' }, `${g.label}${g.phantom ? ` · ${g.phantom} cm phantom` : ''}`), table(g.rows, r.by_device))));
  if (!r.rows.length) host.append(h('p', { class: 'muted' }, 'No series for this region in the period.'));
}

async function render() {
  clear(page).append(adminTabs('protocols.html'),
    h('h1', { class: 'section-title' }, 'Compare protocols'),
    h('p', { class: 'muted small' }, 'All the protocols that scanned one region, whatever they are called, side by side: dose, technique and distribution, per patient group (ICRP 135: head by age, body by weight). A protocol name that states a group (CT_CAP_N_1_6_ANI) places a patient whose age or weight is missing. Head series on the 32 cm phantom are kept apart from the 16 cm ones. Bolus-tracking monitoring and aborted scans are left out.'),
    h('div', { class: 'toolbar', id: 'bar' }),
    h('div', { id: 'result' }));
  let list = [];
  try {
    const [nat, loc] = await Promise.all([codes('ro-drl-ms838-2025'), codes('ro-nom-ct')]);
    list = [...nat.filter((c) => c.modality === 'CT').map((c) => ['ro-drl-ms838-2025', c]), ...loc.map((c) => ['ro-nom-ct', c])];
  } catch (e) { clear(document.getElementById('result')).append(h('p', { class: 'error' }, e.message)); return; }
  const regionSel = h('select', { class: 'input', onchange: (e) => { [state.system, state.region] = e.target.value.split('|'); } },
    list.map(([sys, c]) => h('option', { value: `${sys}|${c.code}`, selected: c.code === state.region && sys === state.system }, `${c.meaning} (${c.code}${sys === 'ro-nom-ct' ? ', local' : ''})`)));
  const inp = (k, attrs) => h('input', { class: 'input', value: state[k], ...attrs, onchange: (e) => { state[k] = attrs.type === 'number' ? Number(e.target.value) : e.target.value; } });
  document.getElementById('bar').append(
    field('Region', regionSel),
    field('From', inp('start', { type: 'date' })), field('to', inp('end', { type: 'date' })),
    field('Selection', inp('q', { class: 'w-160', type: 'text' })),
    field('Groups', select('grid', [['icrp', 'ICRP 135 (head age, body weight)'], ['national', 'National age groups']])),
    field('Protocols', select('by', [['protocol', 'Each name'], ['family', 'Merge contrast / phase / group variants']])),
    h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: state.by_device, onchange: (e) => { state.by_device = e.target.checked; } }), 'Per room'),
    field('Min. series', inp('min_n', { class: 'w-70', type: 'number', min: 1 })),
    h('label', { class: 'row', title: 'Body groups by weight: include weights estimated from the growth charts' }, h('input', { type: 'checkbox', checked: state.estimated, onchange: (e) => { state.estimated = e.target.checked; } }), 'Estimated weights'),
    h('button', { class: 'btn primary', type: 'button', onclick: run }, 'Compare'));
  run();
}

render();
