// DRL sets (admin): the reference sets and their values. The values come from the bundled
// seed files (dosimetrix/seeds/drl/*.csv) and are reloaded by `dosimetrix seed`, so they are
// shown read-only here. The comparison with local doses is on the "Reference levels" page.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear } from '../core/dom.js';
import { drlSets, drlSet } from '../core/api.js';
import { shownUnit } from '../core/format.js';

const page = document.getElementById('page');
const QTY = { dlp: 'DLP', ctdivol: 'CTDIvol', dap: 'DAP', kae: 'Ka,e', mgd: 'MGD', activity: 'Activity' };
const APPLIES = { series: 'per series', study: 'per study', exposure: 'per exposure' };
const fmt = (v) => (v >= 100 ? Math.round(v).toString() : Number(v.toPrecision(3)).toString());

async function render() {
  clear(page).append(adminTabs('drl-sets.html'));
  let sets;
  try { sets = await drlSets(); } catch (e) { page.append(h('p', { class: 'error' }, e.message)); return; }
  if (!sets.length) { page.append(h('p', { class: 'muted' }, 'No reference sets. Run "dosimetrix seed".')); return; }

  const wanted = new URLSearchParams(location.search).get('set');
  const pick = h('select', { class: 'input', 'aria-label': 'Reference set' },
    sets.map((s) => h('option', { value: s.key, selected: s.key === wanted }, `${s.name} (${s.values})`)));
  const filter = h('input', { class: 'input w-260', type: 'search', placeholder: 'Filter: code, exam, band…', 'aria-label': 'Filter' });
  const meta = h('div', { class: 'muted small', style: { margin: 'var(--sp-2) 0' } });
  const holder = h('div');
  page.append(h('div', { class: 'toolbar', style: { display: 'flex', gap: 'var(--sp-2)', 'flex-wrap': 'wrap', 'align-items': 'center' } },
    pick, filter, h('a', { class: 'btn xs', href: 'drl.html' }, 'Compare with local doses →')), meta, holder);

  let current = null;
  const draw = () => {
    clear(holder);
    if (!current) return;
    const f = filter.value.trim().toLowerCase();
    const rows = current.values.filter((v) => !f || [v.exam, v.exam_meaning, v.band_label, v.modality, QTY[v.quantity]]
      .some((x) => (x || '').toLowerCase().includes(f)));
    const body = h('tbody');
    let last = null;
    for (const v of rows) {
      const first = v.exam !== last;
      last = v.exam;
      body.append(h('tr', { class: first ? 'group-start' : '' },
        h('td', { class: 'mono' }, first ? v.exam : ''),
        h('td', {}, first ? v.exam_meaning : ''),
        h('td', {}, first ? (v.modality || '') : ''),
        h('td', {}, v.band_label || 'all patients'),
        h('td', {}, QTY[v.quantity] || v.quantity),
        h('td', { class: 'num mono' }, fmt(v.value * shownUnit(v.unit).f)),
        h('td', {}, shownUnit(v.unit).unit),
        h('td', { class: 'muted' }, APPLIES[v.applies_to] || v.applies_to)));
    }
    holder.append(h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['Code', 'Exam', 'Mod.', 'Group', 'Quantity', 'Value', 'Unit', 'Applies to'].map((c) => h('th', {}, c)))),
      body))),
    h('p', { class: 'muted small' }, `${rows.length} of ${current.values.length} values. They come from the bundled seed files and are reloaded by "dosimetrix seed"; to change them, edit dosimetrix/seeds/drl/${current.key}.csv.`));
  };

  const load = async () => {
    clear(meta).append('Loading…');
    try { current = await drlSet(pick.value); } catch (e) { clear(meta).append(h('span', { class: 'error' }, e.message)); return; }
    clear(meta).append(
      h('div', {}, h('strong', {}, 'Source: '), current.source || '—'),
      current.notes ? h('div', {}, h('strong', {}, 'Notes: '), current.notes) : '',
      current.valid_from ? h('div', {}, h('strong', {}, 'Valid from: '), current.valid_from) : '');
    history.replaceState(null, '', `?set=${encodeURIComponent(pick.value)}`);
    draw();
  };
  pick.addEventListener('change', load);
  filter.addEventListener('input', draw);
  load();
}

render();
