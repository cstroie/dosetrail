// Admin · devices. Equipment records grouped by physical device. Display names are edited
// inline. Suggested merges (same manufacturer, case-insensitive, plus model and serial) open in
// the merge inspector, where the surviving display name is chosen. In sample mode, edits and
// merges change the page only; the API will persist them and write the audit log.
// Any devices can also be selected with the checkboxes and merged by hand (firmware update,
// renamed station…), and the selection can be marked local or external in one step.

import '../components/dt-shell.js';
import { h, clear, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, ago, dateTime } from '../core/format.js';
import { devices, now, isSample, updateDevice, mergeDevices, splitDevice, dismissMerge } from '../core/api.js';

const page = document.getElementById('page');
const ADMIN_TABS = ['Devices', 'DICOM nodes', 'Protocol mappings', 'DRLs', 'Data quality', 'Alert rules', 'Users', 'Audit log'];
const ADMIN_LINKS = { 'Protocol mappings': 'review.html', DRLs: 'drl.html', 'Data quality': 'quality.html', 'Alert rules': 'rules.html' };

let records = [];
let suggestions = [];
let current = null;  // suggestion under review
let survivor = null; // record id whose display name survives
let filterText = '';
let originFilter = 'all';    // all | local | external
let selected = new Set();    // physical device ids ticked in the table
let mergeName = '';          // display name after merge (editable in the inspector)
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

/** API mode: persist, then reload from the server. Sample mode: page-only change. */
async function save(promise, message) {
  try {
    await promise;
    toast(message);
    await init();
  } catch (e) {
    toast(`Not saved: ${e.message}`);
  }
}

function suggestionFor(physicalId) {
  return suggestions.find((s) => s.record_ids.includes(physicalId) && !s.done);
}

function visibleGroups() {
  const q = filterText.toLowerCase();
  return groups().filter((recs) => {
    const main = recs[0];
    if (originFilter !== 'all' && main.origin !== originFilter) return false;
    const hay = recs.map((r) => `${r.display_name} ${r.manufacturer} ${r.model} ${r.serial} ${r.station} ${r.institution}`).join(' ').toLowerCase();
    return !q || hay.includes(q);
  });
}

function renderTable(tbody) {
  clear(tbody);
  for (const recs of visibleGroups()) {
    const main = recs[0];
    const sugg = suggestionFor(main.physical_id);
    const active = current && current === sugg;
    const total = recs.reduce((a, r) => a + r.studies, 0);
    const last = recs.reduce((a, r) => (r.last_study > a.last_study ? r : a), recs[0]);
    tbody.append(h('tr', { class: `group ${active ? 'active' : ''}` },
      h('td', {}, h('input', { type: 'checkbox', checked: selected.has(main.id), 'aria-label': `Select ${main.display_name}`,
        onchange: (e) => { if (e.target.checked) selected.add(main.id); else selected.delete(main.id); render(); } })),
      h('td', {}, h('input', { class: 'inline-edit', value: main.display_name, 'aria-label': `Display name for ${main.station}`,
        onchange: (e) => {
          const name = e.target.value.trim();
          if (!name) { e.target.value = main.display_name; return; }
          if (!isSample) { save(updateDevice(main.id, { display_name: name }), `Display name saved: ${name}`); return; }
          main.display_name = name;
          toast(`Display name saved: ${main.display_name} (sample mode, this page only)`);
        } }),
      sugg ? h('button', { class: 'btn xs', type: 'button', style: { 'margin-top': '4px' }, onclick: () => review(sugg) }, `Review merge (${sugg.record_ids.length})`) : null),
      h('td', {}, `${main.manufacturer} ${main.model}`, h('br'), h('span', { class: 'muted', style: { 'font-weight': '400', 'font-size': '11px' } }, main.institution)),
      h('td', { class: 'mono' }, main.serial || '—'),
      h('td', { class: 'mono' }, main.station),
      h('td', {}, h('select', { class: 'input', 'aria-label': `Modality override for ${main.display_name}`, style: { height: '26px' },
        onchange: (e) => (isSample ? toast(`Modality override: ${e.target.value}`)
          : save(updateDevice(main.id, { modality_override: e.target.value }), `Modality override: ${e.target.value}`)) },
      ['auto', 'CT', 'DX', 'CR', 'RF', 'XA', 'MG', 'NM'].map((m) => h('option', { value: m, selected: m === (main.modality_override || 'auto') }, m === 'auto' ? `auto (${main.modality})` : m)))),
      h('td', {}, h('select', { class: 'input', 'aria-label': `Origin of ${main.display_name}`, style: { height: '26px' },
        onchange: (e) => {
          if (!isSample) { save(updateDevice(main.id, { origin: e.target.value }), `${main.display_name} is now ${e.target.value}`); return; }
          recs.forEach((r) => { r.origin = e.target.value; });
          toast(`${main.display_name} is now ${e.target.value}`);
        } },
      ['local', 'external'].map((o) => h('option', { value: o, selected: o === main.origin }, o)))),
      h('td', { class: 'r num' }, num(total)),
      h('td', {}, lastTag(last))));
    if (recs.length > 1) {
      for (const r of recs) {
        tbody.append(h('tr', { class: `child ${active ? 'active' : ''}` },
          h('td', {}),
          h('td', {}, `↳ ${r.station}`),
          h('td', { class: 'muted' }, `${r.manufacturer} ${r.model}`),
          h('td', { class: 'mono' }, r.serial || '—'),
          h('td', { class: 'mono muted' }, r.software ? `sw ${r.software}` : ''),
          h('td', {}),
          h('td', {}, !isSample && r.id !== main.id
            ? h('button', { class: 'btn xs', type: 'button', title: 'Make this record a separate device again',
              onclick: () => save(splitDevice(r.id), `${r.station} split off`) }, 'Split')
            : null),
          h('td', { class: 'r num' }, num(r.studies)),
          h('td', { class: 'mono muted' }, dateTime(r.last_study).slice(0, 10))));
      }
    }
  }
}

function review(sugg) {
  current = sugg;
  survivor = sugg.record_ids[0];
  mergeName = records.find((r) => r.id === survivor)?.display_name || '';
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
  const sameModel = distinct('model').length === 1 && distinct('manufacturer').length === 1;
  box.append(h('div', { class: 'panel-body', style: { display: 'flex', 'flex-direction': 'column', gap: '12px' } },
    sameModel
      ? h('div', {}, h('strong', {}, `${recs[0].manufacturer} ${recs[0].model}`), ' ', h('span', { class: 'mono muted' }, `SN ${distinct('serial').join(' · ')}`))
      : h('div', {}, h('strong', {}, 'Different models'), h('br'), h('span', { class: 'muted', style: { 'font-size': '12px' } }, 'Merge only if this is the same physical unit (e.g. after a hardware or software upgrade).')),
    h('div', { class: 'diff', 'aria-label': 'Differences between the records' },
      diffRow('station', 'station'), diffRow('manufacturer', 'manufacturer'), diffRow('model', 'model'),
      diffRow('serial', 'serial'), diffRow('software', 'software'), diffRow('origin', 'origin'),
      h('span', { class: 'muted' }, 'rule'), h('span', {}, current.rule)),
    h('fieldset', { style: { border: '0', margin: '0', padding: '0', display: 'flex', 'flex-direction': 'column', gap: '6px' } },
      h('legend', { class: 'muted', style: { 'font-size': '11px', 'text-transform': 'uppercase', 'letter-spacing': '.06em', 'margin-bottom': '6px' } }, 'Surviving display name'),
      recs.map((r) => h('label', { class: 'opt' },
        h('input', { type: 'radio', name: 'survivor', value: r.id, checked: r.id === survivor, onchange: () => { survivor = r.id; mergeName = r.display_name; render(); } }),
        h('span', {}, h('strong', {}, r.display_name), h('br'), h('span', { class: 'mono muted', style: { 'font-size': '11px' } }, `${r.station} · ${num(r.studies)} studies`))))),
    h('label', { class: 'muted', style: { display: 'grid', gap: '4px', 'font-size': '11px' } }, 'Display name after merge',
      h('input', { class: 'input', value: mergeName || survivorRec.display_name, maxlength: 128,
        oninput: (e) => { mergeName = e.target.value; } })),
    h('dl', { class: 'kv', style: { padding: '0' } },
      h('dt', {}, 'Survivor'), h('dd', {}, h('strong', {}, survivorRec.display_name)),
      h('dt', {}, 'Studies'), h('dd', { class: 'mono' }, num(total)),
      h('dt', {}, 'Stations routed here'), h('dd', { class: 'mono' }, recs.map((r) => r.station).join(', '))),
    h('div', { class: 'row' },
      h('button', { class: 'btn primary', type: 'button', onclick: () => merge(recs, survivorRec, (mergeName || survivorRec.display_name).trim()) }, `${t('admin.merge')} ${recs.length} devices`),
      current.manual
        ? h('button', { class: 'btn', type: 'button', onclick: () => { current = null; render(); } }, 'Cancel')
        : h('button', { class: 'btn', type: 'button', onclick: () => {
          if (!isSample) { save(dismissMerge(current.record_ids), 'Suggestion dismissed'); return; }
          current.done = true; current = null; toast('Suggestion dismissed'); render();
        } }, t('admin.reject'))),
    h('p', { class: 'muted', style: { 'font-size': '11px' } }, 'Reference levels and alert rules come from the surviving record. Merges are written to the audit log and can be split again.')));
  return box;
}

function merge(recs, keep, name = keep.display_name) {
  if (!isSample) {
    selected.clear();
    current = null;
    save(mergeDevices(recs.map((r) => r.id), keep.id, name), `Merged ${recs.length} devices into “${name}”`);
    return;
  }
  const physicalId = recs[0].physical_id;
  const main = records.find((r) => r.id === physicalId);
  main.display_name = name;
  main.studies = recs.reduce((a, r) => a + r.studies, 0);
  main.last_study = recs.reduce((a, r) => (r.last_study > a ? r.last_study : a), main.last_study);
  main.station = recs.map((r) => r.station).filter((v, i, a) => a.indexOf(v) === i).join(' / ');
  records = records.filter((r) => r.physical_id !== physicalId || r.id === physicalId);
  selected.clear();
  current.done = true;
  current = null;
  toast(`Merged ${recs.length} records into “${name}” (sample mode, not saved)`);
  render();
}

function startManualMerge() {
  const ids = [...selected];
  const recs = ids.map((id) => records.find((r) => r.id === id)).filter(Boolean)
    .sort((a, b) => b.studies - a.studies);
  current = { record_ids: recs.map((r) => r.id), rule: 'manual selection', manual: true };
  survivor = recs[0].id;
  mergeName = recs[0].display_name;
  render();
  document.getElementById('inspector')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function setOrigin(origin) {
  const ids = [...selected];
  if (!isSample) {
    selected.clear();
    save(Promise.all(ids.map((id) => updateDevice(id, { origin }))), `${ids.length} device(s) marked ${origin}`);
    return;
  }
  for (const r of records) if (ids.includes(r.physical_id)) r.origin = origin;
  selected.clear();
  toast(`${ids.length} device(s) marked ${origin} (sample mode, this page only)`);
  render();
}

function selectionBar() {
  if (!selected.size) return null;
  return h('div', { class: 'row', role: 'toolbar', 'aria-label': 'Selected devices', style: { gap: '6px', 'align-items': 'center', padding: '8px 12px', 'border-bottom': '1px solid var(--line, #333)' } },
    h('strong', {}, `${selected.size} selected`),
    h('button', { class: 'btn primary xs', type: 'button', disabled: selected.size < 2, title: 'Merge the selected devices into one', onclick: startManualMerge }, 'Merge…'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => setOrigin('local') }, 'Mark local'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => setOrigin('external') }, 'Mark external'),
    h('button', { class: 'btn xs', type: 'button', onclick: () => { selected.clear(); render(); } }, 'Clear'));
}

function render() {
  const tbody = h('tbody');
  const left = suggestions.filter((s) => !s.done).length;
  const physical = new Set(records.map((r) => r.physical_id)).size;
  page.replaceChildren(
    h('nav', { class: 'tabs', 'aria-label': 'Administration' },
      ADMIN_TABS.map((x, i) => (ADMIN_LINKS[x] ? h('a', { href: ADMIN_LINKS[x] }, x)
        : h('button', { type: 'button', 'aria-current': i === 0 ? 'page' : false, onclick: () => { if (i) toast(`${x}: not built yet`); } }, x)))),
    h('div', { class: 'detail' },
      h('section', { class: 'panel', style: { flex: '999 1 760px', 'min-width': '0' }, 'aria-labelledby': 'h-dev' },
        h('div', { class: 'panel-head' }, h('h1', { id: 'h-dev', style: { 'font-size': '15px' } }, t('admin.devices')),
          h('span', { class: 'muted mono' }, `${physical} devices · ${records.length} records`),
          left ? h('span', { class: 'tag warn' }, `${left} suggested merges`) : null,
          h('span', { class: 'spacer' }),
          h('select', { class: 'input', 'aria-label': 'Show devices by origin', style: { width: 'auto' },
            onchange: (e) => { originFilter = e.target.value; render(); } },
          [['all', 'all origins'], ['local', 'local'], ['external', 'external']].map(([v, l]) => h('option', { value: v, selected: v === originFilter }, l))),
          h('input', { class: 'input', type: 'search', 'aria-label': 'Filter devices', placeholder: 'filter: model, serial, station…', value: filterText,
            oninput: (e) => { filterText = e.target.value; renderTable(tbody); } })),
        selectionBar(),
        h('div', { class: 'grid-scroll' }, h('table', { class: 'grid tree', 'aria-label': 'Devices and their equipment records' },
          h('thead', {}, h('tr', {},
            h('th', { scope: 'col' }, h('input', { type: 'checkbox', 'aria-label': 'Select all shown devices',
              checked: visibleGroups().length > 0 && visibleGroups().every((g) => selected.has(g[0].id)),
              onchange: (e) => { for (const g of visibleGroups()) { if (e.target.checked) selected.add(g[0].id); else selected.delete(g[0].id); } render(); } })),
            ['Display name', 'Manufacturer · model', 'Serial', 'Station', 'Modality', 'Origin', 'Studies', 'Last study']
              .map((x, i) => h('th', { scope: 'col', class: i === 6 ? 'r' : '' }, x)))),
          tbody))),
      inspector()));
  renderTable(tbody);
}

async function init() {
  const [data, n] = await Promise.all([devices(), now()]);
  records = structuredClone(data.records);
  suggestions = structuredClone(data.merge_suggestions);
  selected = new Set([...selected].filter((id) => records.some((r) => r.id === id && r.physical_id === id)));
  nowIso = n;
  current = suggestions[0] || null;
  survivor = current?.record_ids[0] ?? null;
  mergeName = records.find((r) => r.id === survivor)?.display_name || '';
  render();
}

init().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
