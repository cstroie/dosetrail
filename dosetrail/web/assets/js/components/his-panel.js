// The HIS request card of the study pages (request found in the hospital information
// system: ward, requester, indication, guideline decision) and the weight-source tag.
// Admins can queue the study for another lookup.
import { h, toast } from '../core/dom.js';
import { requeryHis, isSample } from '../core/api.js';

const STATUS = {
  pending: ['info', 'waiting'], matched: ['ok', 'request found'], patient_only: ['warn', 'patient only'],
  no_patient: ['warn', 'patient not found'], skipped: ['', 'not looked up'], error: ['alert', 'error'],
};
const SOURCES = {
  measured: ['measured', 'measured before the exam'],
  chart_same_day: ['request', 'written on the HIS request the day of the exam'],
  chart_72h: ['request ≤ 72 h', 'written on a HIS request up to 72 h before the exam'],
  his_chart_undated: ['HIS record', 'from the patient record in the HIS; date of measurement unknown'],
  dicom_unknown: ['DICOM', 'from the image header; provenance unknown'],
  imported: ['imported', 'imported from the previous system'],
};

/** Tag after the weight: estimate (median for age and sex) or where a real value came from. */
export function weightNote(p) {
  if (p.weight_estimated) {
    const why = p.weight_reported != null
      ? `reported ${p.weight_reported} kg is implausible for age and sex (outside P1–P99); `
      : 'no weight reported; ';
    return h('span', { class: 'tag', style: { 'margin-left': '6px' }, title: `${why}estimate = median weight for age and sex (WHO to 10 y, CDC 2000 to 20 y)` },
      p.weight_reported != null ? `est. (reported ${p.weight_reported})` : 'est.');
  }
  const src = SOURCES[p.weight_source];
  if (!src || p.weight_kg == null) return null;
  return h('span', { class: 'tag', style: { 'margin-left': '6px' }, title: src[1] }, src[0]);
}

function row(label, value) {
  return value == null || value === '' ? null : [h('dt', {}, label), h('dd', {}, value)];
}

/** The HIS panel, or null when the study was never queued. */
export function hisPanel(s, { admin = true } = {}) {
  const x = s.his;
  if (!x) return null;
  const [cls, label] = STATUS[x.status] || ['', x.status];
  const r = x.request;
  const body = h('dl', { class: 'kv' },
    row('Status', h('span', {}, h('span', { class: `tag ${cls}` }, label),
      x.score != null ? h('span', { class: 'muted small' }, ` match ${Math.round(x.score * 100)} %`) : null,
      x.next_try ? h('span', { class: 'muted small' }, ` · next try ${x.next_try.replace('T', ' ')}`) : null)),
    x.other_person ? row('Note', 'the HIS record found by this ID seems to be another person’s (birth date far from the header’s): not used') : null,
    r ? [row('Ordered', `${(r.ordered || '').replace('T', ' ')}${r.priority === 'urgent' ? ' · urgent' : ''}`),
      row('Ward', r.ward), row('Requested by', r.requester), row('Exam', [r.procedure, r.region].filter(Boolean).join(' · ')),
      row('Indication', r.indication), row('Guideline', r.guideline), row('Prior imaging', r.prior_imaging),
      row('On the form', [r.weight_kg != null ? `${r.weight_kg} kg` : null, r.height_m != null ? `${Math.round(r.height_m * 100)} cm` : null,
        r.pregnant ? 'pregnant' : null].filter(Boolean).join(' · ') || null)] : null,
    x.rejected.length ? row('Not used', x.rejected.map((j) => `${j.weight_kg != null ? `${j.weight_kg} kg` : `${Math.round(j.height_m * 100)} cm`} from ${j.from}: ${j.why}`).join('; ')) : null,
    x.error ? row('Error', x.error) : null);
  const again = admin && !isSample ? h('button', { class: 'btn xs no-print', type: 'button', title: 'Look this study up in the HIS again' }, 'Look up again') : null;
  again?.addEventListener('click', async () => {
    again.disabled = true;
    try { await requeryHis(s.id); toast('Queued: the HIS worker looks it up within a minute'); } catch (e) { toast(e.message); again.disabled = false; }
  });
  return h('section', { class: 'panel', 'aria-labelledby': 'h-his' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-his' }, 'HIS request'), h('span', { class: 'spacer' }), again), body);
}
