// Incidents (Quality; the admin edits): the investigation of an alert or a study: cause, actions, outcome, and the
// decision whether it is reportable as an unintended or accidental exposure (BSS art. 63). Every
// change and note is logged with who and when; closing needs the outcome and that decision.
// Open one from an alert (the Investigate button on the alerts page) or here by study number.

import '../components/dx-shell.js';
import { registerTabs } from '../components/admin-tabs.js';
import { h, clear, section, toast, pageHead } from '../core/dom.js';
import { promptDialog } from '../core/confirm.js';
import { isAdmin, incidents, incident, openIncident, updateIncident } from '../core/api.js';
import { dataTable, kpiRow, tag, when, field } from '../core/tables.js';

const page = document.getElementById('page');
const list = h('div', { style: { display: 'grid', gap: 'var(--sp-3)' } });
const detail = h('div', {});
const admin = await isAdmin();  // opening and editing incidents is for the admin
const params = new URLSearchParams(location.search);
let status = params.get('status') || 'open';

async function create(body) {
  try { const r = await openIncident(body); toast('Incident opened'); show(r.id); } catch (e) { toast(e.message); }
}

function newForm() {
  const study = h('input', { class: 'input num-input', type: 'number', min: 1, placeholder: 'study #' });
  const title = h('input', { class: 'input', type: 'text', maxlength: 255, placeholder: 'what happened', style: { 'min-width': '20em' } });
  return h('div', { class: 'row', style: { 'align-items': 'flex-end', gap: 'var(--sp-2)' } }, field('Study', study), field('Title', title),
    h('button', { class: 'btn', type: 'button', onclick: () => { if (study.value && title.value) create({ study_id: Number(study.value), title: title.value }); } }, 'Open incident'));
}

async function loadList() {
  clear(list).append(h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try { r = await incidents(status); } catch (e) { clear(list).append(h('p', { class: 'error' }, e.message)); return; }
  clear(list).append(
    kpiRow([['Open', r.counts.open || 0], ['Reportability not decided', r.unassessed], ['Closed', r.counts.closed || 0]]),
    dataTable([{ get: (x) => h('a', { href: `?id=${x.id}`, onclick: (e) => { e.preventDefault(); show(x.id); } }, `#${x.id}`), label: 'No.' }, { key: 'title', label: 'Title' },
      { get: (x) => tag(x.status, x.status === 'open' ? 'warn' : 'ok'), label: 'Status' },
      { get: (x) => (x.reportable === null ? tag('not decided', 'warn') : x.reportable ? tag(x.reported ? 'reportable · reported' : 'reportable', 'alert') : tag('not reportable', '')), label: 'Reportable' },
      { get: (x) => (x.study_id ? h('a', { href: `study.html?id=${x.study_id}` }, `study ${x.study_id}`) : null), label: 'Study' }, { key: 'opened_by', label: 'Opened by' }, { get: (x) => when(x.opened_at), label: 'Opened' }], r.rows, 'No incidents.'));
}

async function show(id) {
  const u = new URL(location.href);
  u.search = `?id=${id}`;
  history.replaceState(null, '', u);
  let i;
  try { i = await incident(id); } catch (e) { clear(detail).append(h('p', { class: 'error' }, e.message)); return; }
  const closed = i.status === 'closed';
  const text = (key, label, rows = 3) => field(label, h('textarea', { class: 'input', rows, disabled: closed, style: { width: '100%' }, 'data-k': key }, i[key] || ''));
  const reportable = h('select', { class: 'input', disabled: closed || !admin, 'data-k': 'reportable' },
    [['', 'not decided'], ['true', 'yes: reportable'], ['false', 'no']].map(([v, l]) => h('option', { value: v, selected: v === (i.reportable === null ? '' : String(i.reportable)) }, l)));
  const root = h('div', { class: 'panel', style: { padding: 'var(--sp-3)', display: 'grid', gap: 'var(--sp-2)' } },
    h('div', { class: 'row' }, h('strong', {}, `Incident #${i.id}`), tag(i.status, closed ? 'ok' : 'warn'),
      i.study_id ? h('a', { href: `study.html?id=${i.study_id}` }, `study ${i.study_id}`) : null),
    field('Title', h('input', { class: 'input', type: 'text', maxlength: 255, value: i.title, disabled: closed, 'data-k': 'title' })),
    text('cause', 'Cause'), text('actions', 'Actions taken'), text('outcome', 'Outcome'),
    h('div', { class: 'row', style: { gap: 'var(--sp-3)', 'flex-wrap': 'wrap' } },
      field('Reportable (BSS art. 63)', reportable),
      field('Basis of the decision', h('input', { class: 'input', type: 'text', maxlength: 1024, value: i.reportable_basis || '', disabled: closed, 'data-k': 'reportable_basis', style: { 'min-width': '24em' } })),
      field('Reported on', h('input', { class: 'input', type: 'datetime-local', value: (i.reported_at || '').slice(0, 16), disabled: closed, 'data-k': 'reported_at' })),
      field('Report reference', h('input', { class: 'input', type: 'text', maxlength: 128, value: i.report_reference || '', disabled: closed, 'data-k': 'report_reference' }))),
    !admin ? null : h('div', { class: 'row', style: { gap: 'var(--sp-2)' } },
      closed ? h('button', { class: 'btn', type: 'button', onclick: async () => { const why = await promptDialog('Why is it reopened?', { title: 'Reopen' }); if (why) save({ action: 'reopen', note: why }); } }, 'Reopen')
        : [h('button', { class: 'btn primary', type: 'button', onclick: () => save({}) }, 'Save'),
          h('button', { class: 'btn', type: 'button', onclick: () => save({ action: 'close' }) }, 'Save and close')],
      h('button', { class: 'btn ghost', type: 'button', onclick: async () => { const n = await promptDialog('Note for the log', { title: 'Add a note' }); if (n) save({ note: n }, true); } }, 'Add a note')));
  async function save(extra, noteOnly = false) {
    const body = { ...extra };
    if (!closed && !noteOnly) {
      for (const el of root.querySelectorAll('[data-k]')) {
        const k = el.dataset.k;
        let v = el.value.trim();
        if (k === 'reportable') v = v === '' ? null : v === 'true';
        else if (k === 'reported_at') v = v ? `${v}:00` : null;
        else if (v === '' && k !== 'title') v = null;
        body[k] = v;  // the server logs only what changed
      }
    }
    try { await updateIncident(i.id, body); toast('Saved'); await show(i.id); loadList(); } catch (e) { toast(e.message); }
  }
  clear(detail).append(root, section('Log', { level: 3 },
    dataTable([{ get: (e) => e.at.replace('T', ' '), label: 'When' }, { key: 'user', label: 'Who' }, { key: 'kind', label: 'Kind' }, { key: 'text', label: 'What' }], [...i.log].reverse())));
  detail.scrollIntoView?.({ behavior: 'smooth' });
}

clear(page).append(registerTabs('incidents.html'),
  pageHead('Incidents', { sub: 'Investigation of alerts: cause, actions, outcome and the decision on whether it is reportable. An audit trail for the medical physicist and for an inspection.' },
    h('select', { class: 'input', 'aria-label': 'Show', onchange: (e) => { status = e.target.value; loadList(); } },
      [['open', 'Open'], ['closed', 'Closed'], ['', 'All']].map(([v, l]) => h('option', { value: v, selected: v === status }, l)))),
  admin ? newForm() : null, list, detail);
loadList();
const first = Number(params.get('id'));
if (first) show(first);
