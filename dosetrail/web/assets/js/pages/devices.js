// Admin · devices. Equipment records grouped by physical device. Display names are edited
// inline. Suggested merges (same manufacturer, case-insensitive, plus model and serial) open in
// the merge inspector, where the surviving display name is chosen. In sample mode, edits and
// merges change the page only; the API will persist them and write the audit log.

import '../components/dt-shell.js';
import { h, clear, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, ago, dateTime } from '../core/format.js';
import { devices, now } from '../core/api.js';

const page = document.getElementById('page');
const ADMIN_TABS = ['Devices', 'DICOM nodes', 'Protocol mappings', 'DRLs', 'Alert rules', 'Users', 'Audit log'];

let records = [];
let suggestions = [];
let current = null;  // suggestion under review
let survivor = null; // record id whose display name survives
let filterText = '';
let nowIso = '';

function groups() {
  const byPhysical = new Map();
  for (const r of records) {
    if (!byPhysical.has(r.physical_id)) byPhysical.set(r.physical_id, []);
    byPhysical.get(r.physical_id).push(r);
  }
  return [...byPhysical.values()].map((recs) => {
    recs.sort((a, b) => (a.id === a.physical_id ? -1 : b.id === b.physical_id ? 1 : b.studies - a.studies));
    return recs;
  });
}

function lastTag(r) {
  const days = (new Date(nowIso) - new Date(r.last_study)) / 86400000;
  const level = r.origin === 'external' ? 'info' : days >= 5 ? 'alert' : days >= 2 ? 'warn' : 'ok';
  return h('span', { class: `tag ${level}` }, level === 'ok' || level === 'info' ? `${ago(r.last_study, nowIso)} ago` : `stale ${ago(r.last_study, nowIso)}`);
}

function suggestionFor(physicalId) {
  return suggestions.find((s) => s.record_ids.includes(physicalId) && !s.done);
}

function renderTable(tbody) {
  clear(tbody);
  const q = filterText.toLowerCase();
  for (const recs of groups()) {
    const main = recs[0];
    const hay = recs.map((r) => `${r.display_name} ${r.manufacturer} ${r.model} ${r.serial} ${r.station}`).join(' ').toLowerCase();
    if (q && !hay.includes(q)) continue;
    const sugg = suggestionFor(main.physical_id);
    const active = current && current === sugg;
    const total = recs.reduce((a, r) => a + r.studies, 0);
    const last = recs.reduce((a, r) => (r.last_study > a.last_study ? r : a), recs[0]);
    tbody.append(h('tr', { class: `group ${active ? 'active' : ''}` },
      h('td', {}, h('input', { class: 'inline-edit', value: main.display_name, 'aria-label': `Display name for ${main.station}`,
        onchange: (e) => { main.display_name = e.target.value.trim() || main.display_name; toast(`Display name saved: ${main.display_name} (sample mode, this page only)`); } }),
      sugg ? h('button', { class: 'btn xs', type: 'button', style: { 'margin-top': '4px' }, onclick: () => review(sugg) }, `Review merge (${sugg.record_ids.length})`) : null),
      h('td', {}, `${main.manufacturer} ${main.model}`, h('br'), h('span', { class: 'muted', style: { 'font-weight': '400', 'font-size': '11px' } }, main.institution)),
      h('td', { class: 'mono' }, main.serial || '—'),
      h('td', { class: 'mono' }, main.station),
      h('td', {}, h('select', { class: 'input', 'aria-label': `Modality override for ${main.display_name}`, style: { height: '26px' },
        onchange: (e) => toast(`Modality override: ${e.target.value}`) },
      ['auto', 'CT', 'DX', 'CR', 'RF', 'XA', 'MG', 'NM'].map((m) => h('option', { value: m, selected: m === 'auto' }, m === 'auto' ? `auto (${main.modality})` : m)))),
      h('td', {}, h('select', { class: 'input', 'aria-label': `Origin of ${main.display_name}`, style: { height: '26px' },
        onchange: (e) => { recs.forEach((r) => { r.origin = e.target.value; }); toast(`${main.display_name} is now ${e.target.value}`); } },
      ['local', 'external'].map((o) => h('option', { value: o, selected: o === main.origin }, o)))),
      h('td', { class: 'r num' }, num(total)),
      h('td', {}, lastTag(last))));
    if (recs.length > 1) {
      for (const r of recs) {
        tbody.append(h('tr', { class: `child ${active ? 'active' : ''}` },
          h('td', {}, `↳ ${r.station}`),
          h('td', { class: 'muted' }, `${r.manufacturer} ${r.model}`),
          h('td', { class: 'mono' }, r.serial || '—'),
          h('td', { class: 'mono muted' }, r.software ? `sw ${r.software}` : ''),
          h('td', {}), h('td', {}),
          h('td', { class: 'r num' }, num(r.studies)),
          h('td', { class: 'mono muted' }, dateTime(r.last_study).slice(0, 10))));
      }
    }
  }
}

function review(sugg) {
  current = sugg;
  survivor = sugg.record_ids[0];
  render();
  document.getElementById('inspector')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function inspector() {
  const box = h('aside', { id: 'inspector', class: 'panel inspector', 'aria-labelledby': 'h-insp' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-insp' }, t('admin.inspector')),
      current ? h('span', { class: 'tag warn' }, `${current.record_ids.length} records`) : null));
  if (!current) {
    box.append(h('div', { class: 'panel-body muted' }, suggestions.some((s) => !s.done)
      ? 'Choose “Review merge” on a device to compare its records.'
      : 'No merge suggestions left.'));
    return box;
  }
  const recs = current.record_ids.map((id) => records.find((r) => r.id === id));
  const distinct = (k) => [...new Set(recs.map((r) => r[k] || '—'))];
  const diffRow = (label, key) => { const v = distinct(key); return [h('span', { class: 'muted' }, label), h('span', { class: v.length > 1 ? 'x' : '' }, v.join(' · '))]; };
  const total = recs.reduce((a, r) => a + r.studies, 0);
  const survivorRec = recs.find((r) => r.id === survivor) || recs[0];
  box.append(h('div', { class: 'panel-body', style: { display: 'flex', 'flex-direction': 'column', gap: '12px' } },
    h('div', {}, h('strong', {}, `${recs[0].manufacturer} ${recs[0].model}`), ' ', h('span', { class: 'mono muted' }, `SN ${recs[0].serial || '—'}`)),
    h('div', { class: 'diff', 'aria-label': 'Differences between the records' },
      diffRow('station', 'station'), diffRow('manufacturer', 'manufacturer'), diffRow('software', 'software'),
      h('span', { class: 'muted' }, 'rule'), h('span', {}, current.rule)),
    h('fieldset', { style: { border: '0', margin: '0', padding: '0', display: 'flex', 'flex-direction': 'column', gap: '6px' } },
      h('legend', { class: 'muted', style: { 'font-size': '11px', 'text-transform': 'uppercase', 'letter-spacing': '.06em', 'margin-bottom': '6px' } }, 'Surviving display name'),
      recs.map((r) => h('label', { class: 'opt' },
        h('input', { type: 'radio', name: 'survivor', value: r.id, checked: r.id === survivor, onchange: () => { survivor = r.id; render(); } }),
        h('span', {}, h('strong', {}, r.display_name), h('br'), h('span', { class: 'mono muted', style: { 'font-size': '11px' } }, `${r.station} · ${num(r.studies)} studies`))))),
    h('dl', { class: 'kv', style: { padding: '0' } },
      h('dt', {}, 'After merge'), h('dd', {}, h('strong', {}, survivorRec.display_name)),
      h('dt', {}, 'Studies'), h('dd', { class: 'mono' }, num(total)),
      h('dt', {}, 'Stations routed here'), h('dd', { class: 'mono' }, recs.map((r) => r.station).join(', '))),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', type: 'button', onclick: () => merge(recs, survivorRec) }, `${t('admin.merge')} ${recs.length} records`),
      h('button', { class: 'btn', type: 'button', onclick: () => { current.done = true; current = null; toast('Suggestion dismissed'); render(); } }, t('admin.reject'))),
    h('p', { class: 'muted', style: { 'font-size': '11px' } }, 'Reference levels and alert rules come from the surviving record. Merges are written to the audit log and can be split again.')));
  return box;
}

function merge(recs, keep) {
  const physicalId = recs[0].physical_id;
  const name = keep.display_name;
  const main = records.find((r) => r.id === physicalId);
  main.display_name = name;
  main.studies = recs.reduce((a, r) => a + r.studies, 0);
  main.last_study = recs.reduce((a, r) => (r.last_study > a ? r.last_study : a), main.last_study);
  main.station = recs.map((r) => r.station).filter((v, i, a) => a.indexOf(v) === i).join(' / ');
  records = records.filter((r) => r.physical_id !== physicalId || r.id === physicalId);
  current.done = true;
  current = null;
  toast(`Merged ${recs.length} records into “${name}” (sample mode, not saved)`);
  render();
}

function render() {
  const tbody = h('tbody');
  const left = suggestions.filter((s) => !s.done).length;
  const physical = new Set(records.map((r) => r.physical_id)).size;
  page.replaceChildren(
    h('nav', { class: 'tabs', 'aria-label': 'Administration' },
      ADMIN_TABS.map((x, i) => h('button', { type: 'button', 'aria-current': i === 0 ? 'page' : false, onclick: () => { if (i) toast(`${x}: not built yet`); } }, x))),
    h('div', { class: 'detail' },
      h('section', { class: 'panel', style: { flex: '999 1 760px', 'min-width': '0' }, 'aria-labelledby': 'h-dev' },
        h('div', { class: 'panel-head' }, h('h1', { id: 'h-dev', style: { 'font-size': '15px' } }, t('admin.devices')),
          h('span', { class: 'muted mono' }, `${physical} devices · ${records.length} records`),
          left ? h('span', { class: 'tag warn' }, `${left} suggested merges`) : null,
          h('span', { class: 'spacer' }),
          h('input', { class: 'input', type: 'search', 'aria-label': 'Filter devices', placeholder: 'filter: model, serial, station…', value: filterText,
            oninput: (e) => { filterText = e.target.value; renderTable(tbody); } })),
        h('div', { class: 'grid-scroll' }, h('table', { class: 'grid tree', 'aria-label': 'Devices and their equipment records' },
          h('thead', {}, h('tr', {}, ['Display name', 'Manufacturer · model', 'Serial', 'Station', 'Modality', 'Origin', 'Studies', 'Last study']
            .map((x, i) => h('th', { scope: 'col', class: i === 6 ? 'r' : '' }, x)))),
          tbody))),
      inspector()));
  renderTable(tbody);
}

async function init() {
  const [data, n] = await Promise.all([devices(), now()]);
  records = structuredClone(data.records);
  suggestions = structuredClone(data.merge_suggestions);
  nowIso = n;
  current = suggestions[0] || null;
  survivor = current?.record_ids[0] ?? null;
  render();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
