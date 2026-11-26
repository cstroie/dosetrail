// Report digests (admin): a monthly summary mailed on the first morning of the month to a
// department head: studies, exams above the national DRL, the month's alerts and rooms gone quiet.
// A digest is a name, recipients and a study query (the department). It names no patient. Sent once
// per month through the e-mail settings of the alert notifications (Alerts page).

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, section, toast, pageHead } from '../core/dom.js';
import { confirmDialog } from '../core/confirm.js';
import { digests, saveDigests, digestPreview } from '../core/api.js';
import { field } from '../core/tables.js';

const page = document.getElementById('page');
const host = h('div', { style: { display: 'grid', gap: 'var(--sp-3)' } });
const preview = h('div', {});
let items = [];

function card(d, i) {
  const name = h('input', { class: 'input', type: 'text', maxlength: 80, value: d.name });
  const to = h('input', { class: 'input', type: 'text', value: d.to.join(', '), style: { 'min-width': '22em' } });
  const q = h('input', { class: 'input', type: 'text', value: d.q, placeholder: 'e.g. origin:local modality:CT', style: { 'min-width': '22em' } });
  const on = h('input', { type: 'checkbox', checked: d.enabled });
  const ntfy = h('input', { type: 'checkbox', checked: d.ntfy });
  const read = () => ({ id: d.id, name: name.value, to: to.value.split(/[,;\s]+/).filter(Boolean), q: q.value, enabled: on.checked, ntfy: ntfy.checked });
  d._read = read;
  return h('div', { class: 'panel', style: { padding: 'var(--sp-3)', display: 'grid', gap: 'var(--sp-2)' } },
    h('div', { class: 'row', style: { 'align-items': 'flex-end', gap: 'var(--sp-3)', 'flex-wrap': 'wrap' } },
      field('Name', name), field('Recipients', to, 'comma-separated e-mail addresses'), field('Studies', q, 'a study query: the department'),
      h('label', { class: 'chk' }, on, ' enabled'), h('label', { class: 'chk' }, ntfy, ' also ntfy')),
    h('div', { class: 'row', style: { gap: 'var(--sp-2)' } },
      h('span', { class: 'muted small' }, d.last_period ? `last sent: ${d.last_period}` : 'not sent yet'),
      h('span', { class: 'spacer' }),
      d.id ? h('button', { class: 'btn xs', type: 'button', onclick: () => show(d.id) }, 'Preview last month') : null,
      h('button', { class: 'btn xs ghost danger', type: 'button', onclick: () => { items.splice(i, 1); draw(); } }, 'Remove')));
}

async function show(id) {
  clear(preview).append(h('p', { class: 'muted' }, 'Building…'));
  try {
    const p = await digestPreview(id);
    clear(preview).append(section(p.subject, { level: 3, sub: 'plain-text part of the mail; nothing is sent' }, h('pre', { class: 'mono small panel', style: { padding: 'var(--sp-3)', 'white-space': 'pre-wrap' } }, p.text)));
  } catch (e) { clear(preview).append(h('p', { class: 'error' }, e.message)); }
}

async function save() {
  const body = items.map((d) => d._read());
  if (!(await confirmDialog(`Save ${body.length} digest${body.length === 1 ? '' : 's'}?`, { title: 'Save digests', label: 'Save' }))) return;
  try { items = await saveDigests(body); toast('Saved'); draw(); } catch (e) { toast(e.message); }
}

function draw() {
  clear(host).append(items.length ? items.map(card) : h('p', { class: 'muted' }, 'No digests. Add one to mail a monthly summary to a department head.'),
    h('div', { class: 'row', style: { gap: 'var(--sp-2)' } },
      h('button', { class: 'btn', type: 'button', onclick: () => { items.push({ id: '', name: '', to: [], q: 'origin:local', enabled: true, ntfy: false, last_period: null }); draw(); } }, 'Add a digest'),
      h('button', { class: 'btn primary', type: 'button', onclick: save }, 'Save')));
}

clear(page).append(adminTabs('digests.html'),
  pageHead('Report digests', { sub: 'A monthly summary by e-mail on the first morning of the month: studies, exams above the DRL, alerts, rooms gone quiet. No patient data. Needs the e-mail settings on the Alerts page.' }),
  host, preview);
try { items = await digests(); draw(); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); }
