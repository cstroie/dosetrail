// National paediatric dose survey, central side: upload the sites' monthly survey exports,
// see what was accepted or rejected, and compute the national statistics (P75 of the
// institution medians, national median, Q3/Q1) against the RP 185 EDRLs and national DRLs.
// Server: dosimetrix/aggregate.py.

import '../components/dt-shell.js';
import { h, clear, toast } from '../core/dom.js';
import { num, smart } from '../core/format.js';
import { uploadSurvey, surveyUploads, deleteSurveyUpload, nameInstitution, surveyNational, surveyNationalUrl } from '../core/api.js';

const page = document.getElementById('page');
const TONE = { below: 'ok', above: 'warn', well_above: 'alert' };
const IQ = { ok: ['ok', '≤ 1.5'], wide: ['warn', '1.5–2.5'], very_wide: ['alert', '2.5–3'], extreme: ['alert', '> 3'] };
const QTY = { dlp: 'DLP', ctdivol: 'CTDIvol', dap: 'DAP' };
const UNIT = { 'mGy.cm': 'mGy·cm', 'Gy.cm2': 'Gy·cm²', mGy: 'mGy' };
const ym = (d) => d.toISOString().slice(0, 7);
const now = new Date();
const state = { start: ym(new Date(now.getFullYear(), now.getMonth() - 12, 1)), end: ym(new Date(now.getFullYear(), now.getMonth() - 1, 1)), grid: 'rp185', estimated: false };

function render() {
  const files = h('input', { type: 'file', multiple: true, accept: '.csv,.json' });
  const out = h('div');
  clear(page).append(
    h('h1', { class: 'section-title' }, 'Central collection — national paediatric dose survey'),
    h('p', { class: 'muted small' }, 'Each site exports one month from Reports → "Studiul național" (anonymised: pseudonymous codes, month only) and sends the file. Upload the files here (CSV or JSON). One file holds one institution and one month; a new file for the same institution and month replaces the older one. Rows that fail the checks are listed and left out.'),
    h('div', { class: 'toolbar' }, files, h('button', { class: 'btn primary', type: 'button', onclick: async () => {
      if (!files.files.length) { toast('Choose the files first.'); return; }
      const lines = [];
      for (const f of files.files) {
        try { const r = await uploadSurvey(f); lines.push(h('div', {}, h('span', { class: 'tag ok' }, 'accepted'), ` ${f.name}: ${r.institution} ${r.period}, ${num(r.rows)} rows${r.rejected ? `, ${num(r.rejected)} rejected` : ''}${r.report.replaced.length ? ' (replaces an earlier upload)' : ''}`)); }
        catch (e) { lines.push(h('div', {}, h('span', { class: 'tag alert' }, 'rejected'), ` ${f.name}: ${e.message}`)); }
      }
      out.replaceChildren(...lines); loadUploads();
    } }, 'Upload')),
    out,
    h('h2', { class: 'section-title' }, 'Uploads'), h('div', { id: 'uploads' }),
    h('h2', { class: 'section-title', style: { 'margin-top': 'var(--sp-4)' } }, 'National statistics'),
    statsBar(), h('div', { id: 'national' }));
  loadUploads();
}

async function loadUploads() {
  const host = document.getElementById('uploads');
  let ups;
  try { ups = await surveyUploads(); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  if (!ups.length) { clear(host).append(h('p', { class: 'muted' }, 'Nothing uploaded yet.')); return; }
  clear(host).append(h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, ['Month', 'Institution', 'Name', 'Rows', 'Rejected', 'Status', 'Uploaded', ''].map((c) => h('th', {}, c)))),
    h('tbody', {}, ups.map((u) => {
      const name = h('input', { class: 'input', value: u.institution_name || '', placeholder: 'name (central only)', style: { width: '180px' },
        onchange: async (e) => { try { await nameInstitution(u.institution, e.target.value); toast('Saved'); } catch (err) { toast(err.message); } } });
      const issues = (u.report?.issues || []).map((i) => `row ${i.row}: ${i.reason}`).join('\n');
      return h('tr', { class: u.status === 'replaced' ? 'muted' : '' },
        h('td', { class: 'mono' }, u.period), h('td', { class: 'mono' }, u.institution), h('td', {}, name),
        h('td', { class: 'r' }, num(u.rows)), h('td', { class: 'r', title: issues }, u.rejected ? h('span', { class: 'tag warn' }, num(u.rejected)) : '0'),
        h('td', {}, u.status), h('td', { class: 'small' }, `${(u.uploaded_at || '').replace('T', ' ')} ${u.uploaded_by || ''}`),
        h('td', {}, h('button', { class: 'btn xs ghost', type: 'button', onclick: async () => {
          if (!confirm(`Delete the upload ${u.institution} ${u.period}?`)) return;
          try { await deleteSurveyUpload(u.id); loadUploads(); } catch (e) { toast(e.message); }
        } }, 'Delete')));
    }))))));
}

function statsBar() {
  const inp = (k) => h('input', { class: 'input', type: 'month', value: state[k], onchange: (e) => { state[k] = e.target.value; } });
  return h('div', { class: 'toolbar' },
    h('span', { class: 'muted small' }, 'From'), inp('start'), h('span', { class: 'muted small' }, 'to'), inp('end'),
    h('select', { class: 'input', onchange: (e) => { state.grid = e.target.value; } },
      h('option', { value: 'rp185' }, 'RP 185 groups (head by age, body by weight)'), h('option', { value: 'national' }, 'National age groups (OMS 838/2025)')),
    h('label', { class: 'row' }, h('input', { type: 'checkbox', checked: state.estimated, onchange: (e) => { state.estimated = e.target.checked; } }), 'Include estimated weights'),
    h('button', { class: 'btn primary', type: 'button', onclick: loadNational }, 'Compute'),
    h('a', { class: 'btn', href: '#', onclick: (e) => { e.preventDefault(); const u = surveyNationalUrl(state); if (u) h('a', { href: u, download: '' }).click(); } }, 'Download XLSX'));
}

async function loadNational() {
  const host = document.getElementById('national');
  clear(host).append(h('p', { class: 'muted' }, 'Computing…'));
  let r;
  try { r = await surveyNational(state); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const tag = (s) => (s ? h('span', { class: `tag ${TONE[s]}` }, s.replace('_', ' ')) : '');
  clear(host).append(
    h('p', { class: 'muted small' }, `${r.start} – ${r.end} · ${r.institutions.length} institutions · ${Object.entries(r.counts).map(([k, v]) => `${k.replaceAll('_', ' ')}: ${num(v)}`).join(' · ')}. Proposed NDRL = P75 of the medians of institutions with enough values (10; fluoroscopy 20). Hover the institutions column for each one.`),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['Exam', 'Group', 'Qty', 'n', 'Institutions', 'National median', 'Proposed NDRL', 'Q3/Q1', 'EDRL', 'vs EDRL', 'NDRL', 'vs NDRL'].map((c) => h('th', {}, c)))),
      h('tbody', {}, r.rows.map((x) => h('tr', {},
        h('td', { title: x.exam }, x.exam_meaning), h('td', {}, x.group), h('td', {}, QTY[x.quantity] || x.quantity),
        h('td', { class: 'r' }, num(x.n)),
        h('td', { class: 'r', title: x.institutions.map((i) => `${i.name || i.institution}: median ${smart(i.median)} (n ${i.n})${i.counts ? '' : ' — too few'}`).join('\n') }, `${x.n_institutions_counted}/${x.n_institutions}`),
        h('td', { class: 'r' }, `${smart(x.national_median)} ${UNIT[x.unit] || x.unit}`,
          x.n_phantom32 ? h('span', { class: 'tag warn', title: 'head CT series reported on the 32 cm (body) phantom (about half the 16 cm value): left out' }, ` 32 cm left out: ${x.n_phantom32}`) : null),
        h('td', { class: 'r' }, x.ndrl_proposal == null ? '—' : h('b', {}, smart(x.ndrl_proposal))),
        h('td', {}, x.iq_flag ? h('span', { class: `tag ${IQ[x.iq_flag][0]}`, title: IQ[x.iq_flag][1] }, num(x.iq_factor, 2)) : ''),
        h('td', { class: 'r' }, x.edrl == null ? '—' : smart(x.edrl)), h('td', {}, tag(x.status_edrl)),
        h('td', { class: 'r' }, x.ndrl == null ? '—' : smart(x.ndrl)), h('td', {}, tag(x.status_ndrl)))))))));
}

render();
