// Study detail for CT, radiography (DX/CR), mammography, nuclear medicine and PET/CT.
// Fluoroscopy and interventional studies keep the dedicated view in study.js (Ka,r curve).
// Shows: facts, dose readouts, exam codes, this study's alerts and data-quality findings,
// and the series / exposures with the DRL that applies to each (exam + age group).

import '../components/dx-data-grid.js';
import { indicationRow } from '../components/indication.js';
import { h, toast } from '../core/dom.js';
import { hisPanel, weightNote } from '../components/his-panel.js';
import { t } from '../core/i18n.js';
import { num, smart, dap, dapUnit, clock } from '../core/format.js';
import { studyHref, setGlandularity, isSample } from '../core/api.js';

const SYSTEMS = { 'ro-nom-ct': 'Nomenclator CT', 'ro-nom-rx': 'Nomenclator radiologie', 'ro-drl-ms838-2025': 'NRD OMS 838/2025',
  'ec-rp185': 'EU RP 185', 'ro-insp-1255-ct': 'INSP 1255 CT', 'ro-insp-1255-rx': 'INSP 1255 radiologie',
  'ro-insp-1255-xa': 'INSP 1255 intervențional', 'ro-insp-1255-dental': 'INSP 1255 dentar',
  'ro-insp-1255-nm': 'INSP 1255 MN diagnostic', 'ro-insp-1255-nm-hybrid': 'INSP 1255 MN hibride' };

function ratioTag(value, ref) {
  if (value == null || !ref) return '';
  const r = value / ref;
  const cls = r > 1.5 ? 'alert' : r > 1 ? 'warn' : 'ok';
  return h('span', { class: `tag ${cls}`, title: `${smart(value)} / DRL ${smart(ref)}` }, `${num(r, 2)}×`);
}

function readout(label, value, unit, sub) {
  return h('div', { class: 'readout' }, h('span', { class: 'l' }, label),
    h('span', { class: 'v' }, value, unit ? h('span', { class: 'unit' }, ` ${unit}`) : null), sub ? h('span', { class: 'sub' }, sub) : null);
}

function ctColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'protocol', label: 'Series / protocol', render: (e) => h('span', { class: 'sans' }, e.protocol || '—', e.localizer ? h('span', { class: 'tag', style: { 'margin-left': '6px' } }, 'localizer') : null) },
    { key: 'type', label: 'Type', width: '80px' },
    { key: 'ctdivol', label: 'CTDIvol', unit: 'mGy', align: 'right', render: (e) => (e.ctdivol == null ? '' : num(e.ctdivol, 2)) },
    { key: 'phantom_cm', label: 'Ph.', unit: 'cm', align: 'right' },
    { key: 'dlp', label: 'DLP', unit: 'mGy·cm', align: 'right', render: (e) => (e.dlp == null ? '' : num(e.dlp, 1)) },
    { key: 'ssde_mgy', label: 'SSDE', unit: 'mGy', align: 'right', render: (e) => (e.ssde_mgy == null ? '' : h('span', { title: `${e.ssde_source === 'reported' ? 'reported by the scanner' : `calculated (${e.ssde_source})`}${e.wed_cm ? ` · Dw ${num(e.wed_cm, 1)} cm` : ''}` }, num(e.ssde_mgy, 2), e.ssde_source === 'reported' ? '' : '*')) },
    { key: 'scan_length_mm', label: 'Length', unit: 'cm', align: 'right', render: (e) => (e.scan_length_mm == null ? '' : num(e.scan_length_mm / 10, 1)) },
    { key: 'pitch', label: 'Pitch', align: 'right' },
    { key: 'kvp', label: 'kV', align: 'right' },
    { key: 'mas', label: 'mAs', align: 'right' },
    { key: 'exam', label: 'DRL exam', render: (e) => e.exam || '' },
    { key: 'drl_dlp', label: 'DLP / DRL', align: 'right', render: (e) => (e.drl ? ratioTag(e.dlp, e.drl.dlp) : '') },
    { key: 'drl_ctdi', label: 'CTDIvol / DRL', align: 'right', render: (e) => (e.drl ? ratioTag(e.ctdivol, e.drl.ctdivol) : '') },
  ];
}

function rxColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'time', label: 'Time', width: '80px', render: (e) => (e.time ? clock(e.time) : '') },
    { key: 'protocol', label: 'Protocol', render: (e) => h('span', { class: 'sans' }, e.protocol || '—') },
    { key: 'anatomy', label: 'Anatomy' },
    { key: 'projection', label: 'Proj.', render: (e) => h('span', { title: e.view || 'derived from the protocol' }, e.projection || '') },
    { key: 'kvp', label: 'kV', align: 'right' },
    { key: 'mas', label: 'mAs', align: 'right' },
    { key: 'dap_gycm2', label: 'DAP', unit: dapUnit(), align: 'right', render: (e) => (e.dap_gycm2 == null ? '' : dap(e.dap_gycm2, 3)) },
    { key: 'exposure_index', label: 'EI', align: 'right' },
    { key: 'deviation_index', label: 'DI', align: 'right', render: diCell },
    { key: 'sid_cm', label: 'SID', unit: 'cm', align: 'right' },
    { key: 'exam', label: 'Exam', render: (e) => e.exam || '' },
    { key: 'drl', label: 'DAP / DRL', align: 'right', render: (e) => (e.drl?.dap ? h('span', { title: `${e.drl.code}: DRL ${e.drl.dap} Gy·cm² per projection (retakes add up)` }, ratioTag(e.dap_gycm2, e.drl.dap)) : '') },
  ];
}

// |DI| > 3: the detector dose is outside the target (IEC 62494-1); flagged, not alarmed
function diCell(e) {
  if (e.deviation_index == null) return '';
  return Math.abs(e.deviation_index) > 3 ? h('span', { class: 'tag warn', title: '|DI| > 3: exposure outside the target' }, num(e.deviation_index, 1)) : num(e.deviation_index, 1);
}

function mgColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'time', label: 'Time', width: '80px', render: (e) => (e.time ? clock(e.time) : '') },
    { key: 'laterality', label: 'Side', width: '50px' },
    { key: 'view', label: 'View', render: (e) => e.projection || e.view || '' },
    { key: 'kvp', label: 'kV', align: 'right' },
    { key: 'mas', label: 'mAs', align: 'right' },
    { key: 'target', label: 'Target / filter', render: (e) => [e.target, e.filter].filter(Boolean).join(' / ') },
    { key: 'thickness_mm', label: 'Thickness', unit: 'mm', align: 'right' },
    { key: 'force_n', label: 'Force', unit: 'N', align: 'right' },
    { key: 'esd_mgy', label: 'ESD', unit: 'mGy', align: 'right', render: (e) => (e.esd_mgy == null ? '' : num(e.esd_mgy, 2)) },
    { key: 'agd_mgy', label: 'AGD', unit: 'mGy', align: 'right', render: (e) => (e.agd_mgy == null ? '' : h('strong', {}, num(e.agd_mgy, 2))) },
  ];
}

function nmColumns() {
  return [
    { key: 'no', label: '#', align: 'right', width: '40px' },
    { key: 'time', label: 'Injection', width: '80px', render: (e) => (e.time ? clock(e.time) : '') },
    { key: 'agent', label: 'Radiopharmaceutical', render: (e) => h('span', { class: 'sans' }, e.agent || '—') },
    { key: 'nuclide', label: 'Nuclide' },
    { key: 'route', label: 'Route' },
    { key: 'activity_mbq', label: 'Activity', unit: 'MBq', align: 'right', render: (e) => (e.activity_mbq == null ? '—' : h('strong', {}, num(e.activity_mbq, 0))) },
    { key: 'per_kg', label: 'MBq/kg', align: 'right', sortable: false, render: (e) => (e.activity_mbq != null && e._kg ? num(e.activity_mbq / e._kg, 2) : '') },
    { key: 'same_administration_as', label: 'Counted', render: (e) => (e.same_administration_as ? h('a', { href: studyHref({ id: e.same_administration_as }) }, 'on an earlier study') : 'here') },
  ];
}

// Breast glandularity (Order 1255/2016 art. 3): not in DICOM, so typed in; the form's own steps.
function glandularityRow(s) {
  if (s.modality !== 'MG') return [];
  const steps = ['', '0.1', '25', '50', '75', '100'];
  const sel = h('select', { class: 'input no-print', 'aria-label': 'Glandularity', disabled: isSample, onchange: async (e) => {
    try { await setGlandularity(s.id, e.target.value === '' ? null : Number(e.target.value)); toast('Saved'); } catch (err) { toast(err.message); }
  } }, steps.map((v) => h('option', { value: v, selected: String(s.glandularity_pct ?? '') === v }, v === '' ? '—' : `${v} %`)));
  return [h('dt', {}, 'Glandularity'), h('dd', {}, sel)];
}

export function renderStudy(page, s, { revealButton, ackAlert }) {
  const p = s.patient;
  const T = s.totals;
  const hybrid = (s.modality_label || s.modality) !== s.modality; // PET/CT, SPECT/CT
  const ct = s.modality === 'CT' || hybrid;
  const ev = s.events;
  const identity = h('span', { class: 'mono' }, p.pseudo_id || '—');
  const series = ev.filter((e) => !e.localizer);
  const views = [...new Set(ev.map((e) => e.projection).filter(Boolean))];
  const placeholder = !s.description || /^[-\s.0/]*$/.test(s.description);
  const examName = (s.exams || []).find((x) => x.system.startsWith('ro-nom'))?.meaning;
  const title = placeholder ? (examName || `${s.modality_label || s.modality} study`) : s.description;
  const ro = ct ? [
    ...(T.activity_mbq != null ? [readout('Administered activity', num(T.activity_mbq, 0), 'MBq')] : []),
    readout('Total DLP', num(T.dlp_mgycm, 1), 'mGy·cm', `${series.length} series + ${ev.length - series.length} localizer`),
    readout('Highest CTDIvol', num(T.ctdivol_max, 2), 'mGy', [...new Set(series.map((e) => e.phantom_cm).filter(Boolean))].map((x) => `${x} cm phantom`).join(' · ')),
    readout('Contrast', { N: 'native', C: 'contrast', NC: 'native + contrast', MF: 'multiphase' }[s.contrast] || '—'),
  ] : s.modality === 'MG' ? [
    readout('AGD left', T.agd_left_mgy != null ? num(T.agd_left_mgy, 2) : '—', 'mGy', 'per breast: the sum of its views'),
    readout('AGD right', T.agd_right_mgy != null ? num(T.agd_right_mgy, 2) : '—', 'mGy'),
    readout('Views', views.length ? views.join(', ') : `${ev.length}`, '', `${ev.length} exposures`),
    readout('Thickness', (() => { const x = ev.map((e) => e.thickness_mm).filter((v) => v != null); return x.length ? `${num(Math.min(...x))}–${num(Math.max(...x))}` : '—'; })(), 'mm'),
  ] : ['PT', 'NM'].includes(s.modality) ? [
    readout('Administered activity', T.activity_mbq != null ? num(T.activity_mbq, 0) : '—', 'MBq', T.activity_counted_on ? h('a', { href: studyHref({ id: T.activity_counted_on }) }, 'same administration: counted on the earlier study') : `${ev.length} administration${ev.length === 1 ? '' : 's'}`),
  ] : [
    readout('Total DAP', dap(T.dap_gycm2, 3), dapUnit(), `${ev.length} exposures`),
    readout('Projections', views.length ? views.join(', ') : '—'),
    readout('Highest kV', num(Math.max(0, ...ev.map((e) => e.kvp || 0)), 0), 'kV'),
  ];
  // which events this page lists: CT series; NM/PET administrations; MG views; DX/CR exposures
  const kind = ct ? 'ct' : ['PT', 'NM'].includes(s.modality) ? 'nm' : s.modality === 'MG' ? 'mg' : 'rx';
  const KIND = { ct: ['CT series', 'series', 'grid.ct-series', ctColumns], nm: ['Administrations', 'administrations', 'grid.nm-admin', nmColumns],
    mg: ['Views', 'views', 'grid.mg-views', mgColumns], rx: ['Exposures', 'exposures', 'grid.rx-exposures', rxColumns] }[kind];
  if (kind === 'nm') ev.forEach((e) => { e._kg = p.weight_kg; });
  const grid = h('dx-data-grid', { class: 'study-events', label: KIND[0], noun: KIND[1], 'storage-key': KIND[2] },
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportRows(s, kind) }, 'CSV'));
  grid.columns = KIND[3]();
  grid.rowId = (e) => e.no;
  grid.setState({ sort: 'no', dir: 'asc', page: 1, size: 500 });
  grid.fetcher = async ({ sort, dir }) => {
    const rows = [...ev].sort((a, b) => (a[sort] < b[sort] ? -1 : a[sort] > b[sort] ? 1 : 0) * (dir === 'asc' ? 1 : -1));
    return { total: rows.length, rows };
  };

  const alerts = (s.alerts || []).map((a) => {
    const btn = a.status === 'open' ? h('button', { class: 'btn xs', type: 'button' }, 'Acknowledge') : h('span', { class: 'muted small' }, `acknowledged by ${a.ack_by}`);
    if (a.status === 'open') {
      btn.addEventListener('click', async () => {
        try { await ackAlert(a.id, null); btn.replaceWith(h('span', { class: 'muted small' }, 'acknowledged')); } catch (err) { toast(err.message); }
      });
    }
    return h('div', { class: 'readout' }, h('span', { class: 'l' }, h('span', { class: `tag ${a.level}` }, a.level), ' ', a.name),
      h('span', { class: 'sub' }, a.text, ' ', btn));
  });
  const findings = (s.quality || []).map((f) => h('div', { class: 'readout' },
    h('span', { class: 'l' }, h('span', { class: `tag ${f.severity === 'error' ? 'alert' : 'warn'}` }, f.rule.replaceAll('_', ' '))),
    h('span', { class: 'sub' }, `${f.title}${f.detail ? ` · ${f.detail}` : ''}`)));

  page.replaceChildren(
    h('div', { class: 'row' }, h('h1', {}, title),
      (s.alerts || []).filter((a) => a.status === 'open').length ? h('span', { class: 'tag alert' }, `${s.alerts.filter((a) => a.status === 'open').length} alerts`) : null,
      h('span', { class: 'spacer' }), h('button', { class: 'btn no-print', type: 'button', onclick: () => print() }, t('study.print'))),
    h('div', { class: 'detail' },
      h('div', { class: 'side' },
        h('section', { class: 'panel', 'aria-label': 'Patient and exam' },
          h('dl', { class: 'kv' },
            h('dt', {}, 'Accession'), h('dd', { class: 'mono' }, s.accession || '—'),
            h('dt', {}, 'Patient'), h('dd', {}, identity, ' ', revealButton(s, identity)),
            h('dt', {}, 'Age · sex'), h('dd', { class: 'mono' }, `${p.age_display || (p.age != null ? `${p.age} y` : '—')} · ${p.sex || '—'}`),
            h('dt', {}, 'Height · weight'), h('dd', { class: 'mono' }, `${p.height_cm ?? '—'} cm · ${p.weight_kg ?? '—'} kg`, weightNote(p)),
            h('dt', {}, 'Date'), h('dd', { class: 'mono' }, (s.start || '').replace('T', ' ')),
            h('dt', {}, 'Device'), h('dd', {}, s.device.name || '—'),
            h('dt', {}, 'Modality'), h('dd', {}, s.modality_label || s.modality),
            ...glandularityRow(s), ...indicationRow(s))),
        h('section', { class: 'panel', 'aria-labelledby': 'h-ro' }, h('div', { class: 'panel-head' }, h('h2', { id: 'h-ro' }, t('study.readouts'))), ...ro),
        h('section', { class: 'panel', 'aria-labelledby': 'h-ex' }, h('div', { class: 'panel-head' }, h('h2', { id: 'h-ex' }, 'Exam codes')),
          (s.exams || []).length ? s.exams.map((x) => h('div', { class: 'readout' }, h('span', { class: 'l' }, SYSTEMS[x.system] || x.system),
            h('span', { class: 'sub' }, h('span', { class: 'mono' }, x.code), ` · ${x.meaning}`)))
            : h('p', { class: 'muted small', style: { padding: '0 12px' } }, 'No exam code: map this study in Admin › Protocol mappings.')),
        hisPanel(s),
        alerts.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', {}, 'Alerts')), ...alerts) : null,
        findings.length ? h('section', { class: 'panel' }, h('div', { class: 'panel-head' }, h('h2', {}, 'Data quality')), ...findings) : null),
      h('div', { class: 'main' }, grid,
        h('p', { class: 'muted small' }, ct
          ? 'DRL per series: national OMS 838/2025 DLP for the exam and age group, else RP 185; CTDIvol from RP 185 (head by age, body by weight). Localizers excluded.'
          : kind === 'nm' ? 'Administered activity per injection; a delayed scan of the same injection counts it once, on the first study.'
            : kind === 'mg' ? 'AGD per view as reported by the unit; the per-breast AGD sums the views of that breast (the MGD DRL applies per breast).'
              : 'DRL per projection (OMS 838/2025, by age group). Retakes of the same projection add up to the projection dose.'))));
  grid.reload();  // the grid fetches once it is in the document
  document.title = `${s.accession || s.id} · Dosimetrix`;
}

function exportRows(s, kind) {
  const cols = {
    ct: ['no', 'protocol', 'type', 'ctdivol', 'phantom_cm', 'dlp', 'ssde_mgy', 'scan_length_mm', 'pitch', 'kvp', 'mas', 'exam'],
    nm: ['no', 'time', 'agent', 'nuclide', 'route', 'activity_mbq', 'same_administration_as'],
    mg: ['no', 'time', 'laterality', 'view', 'kvp', 'mas', 'target', 'filter', 'thickness_mm', 'force_n', 'esd_mgy', 'agd_mgy'],
    rx: ['no', 'time', 'protocol', 'anatomy', 'view', 'kvp', 'mas', 'dap_gycm2', 'exposure_index', 'deviation_index', 'sid_cm', 'exam'],
  }[kind];
  const ct = kind === 'ct';
  const esc = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v).replace(/^([=+\-@])/, "'$1"));
  const text = [cols.join(',')].concat(s.events.map((e) => cols.map((c) => esc(e[c])).join(','))).join('\r\n');
  h('a', { href: URL.createObjectURL(new Blob(['﻿', text], { type: 'text/csv;charset=utf-8' })), download: `${s.accession || s.id}-${ct ? 'series' : kind === 'nm' ? 'administrations' : 'exposures'}.csv` }).click();
}

