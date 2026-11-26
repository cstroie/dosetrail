// High-dose follow-up (admin): patients of fluoroscopy and interventional studies that passed a
// substantial dose level (peak skin dose 3 Gy, Ka,r 5 Gy, KAP 500 Gy·cm²): tell the patient, check
// the skin 2–4 weeks later, record the outcome. Created automatically (followup.py); here the
// physicist moves each one along. The patient letter names the patient (pid role, audit-logged).

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, toast, pageHead } from '../core/dom.js';
import { promptDialog } from '../core/confirm.js';
import { followups, updateFollowup, followupLetterHref } from '../core/api.js';
import { dataTable, kpiRow, tag, when } from '../core/tables.js';

const page = document.getElementById('page');
const out = h('div', { style: { display: 'grid', gap: 'var(--sp-3)' } });
let status = new URLSearchParams(location.search).get('status') || 'pending';

async function act(id, patch) {
  try { await updateFollowup(id, patch); toast('Saved'); load(); } catch (e) { toast(e.message); }
}

async function check(r) {
  const outcome = await promptDialog('Outcome of the skin check (e.g. no skin effect, erythema, desquamation)', { title: 'Skin check', value: r.outcome || '' });
  if (outcome) act(r.id, { status: 'checked', outcome });
}
async function dismiss(r) {
  const note = await promptDialog('Why is no follow-up needed? (e.g. phantom, estimate wrong, limited field)', { title: 'No follow-up' });
  if (note) act(r.id, { status: 'dismissed', note });
}

const STATUS = { open: ['to inform', 'alert'], informed: ['informed', 'warn'], checked: ['checked', 'ok'], dismissed: ['dismissed', ''] };

function actions(r) {
  const b = (label, fn, cls = '') => h('button', { class: `btn xs ${cls}`.trim(), type: 'button', onclick: fn }, label);
  return h('span', { class: 'row', style: { gap: '4px', 'flex-wrap': 'wrap' } },
    r.status === 'open' ? b('Informed', () => act(r.id, { status: 'informed' })) : null,
    r.status === 'open' || r.status === 'informed' ? [b('Skin checked…', () => check(r)), b('No follow-up…', () => dismiss(r), 'ghost')] : null,
    h('a', { class: 'btn xs ghost', href: followupLetterHref(r.id, 'ro'), target: '_blank', rel: 'noopener' }, 'Letter RO'),
    h('a', { class: 'btn xs ghost', href: followupLetterHref(r.id, 'en'), target: '_blank', rel: 'noopener' }, 'EN'));
}

async function load() {
  const u = new URL(location.href);
  u.search = status === 'pending' ? '' : `?status=${status}`;
  history.replaceState(null, '', u);
  clear(out).append(h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try { r = await followups(status); } catch (e) { clear(out).append(h('p', { class: 'error' }, e.message)); return; }
  clear(out).append(
    kpiRow([['To inform', r.counts.open || 0], ['Informed, skin not checked', r.counts.informed || 0], ['Overdue (> 4 weeks)', r.overdue], ['Checked', r.counts.checked || 0]]),
    dataTable([
      { get: (x) => h('a', { href: `study.html?id=${x.study_id}` }, when(x.time)), label: 'Study' }, { key: 'modality', label: 'Mod.' },
      { key: 'device', label: 'Room' }, { key: 'exam', label: 'Procedure' }, { key: 'levels', label: 'Level passed' },
      { get: (x) => h('span', {}, tag(STATUS[x.status][0], STATUS[x.status][1]), x.overdue ? [' ', tag('overdue', 'alert')] : null), label: 'Status' },
      { key: 'check_due', label: 'Check from' }, { key: 'outcome', label: 'Outcome / note', get: (x) => x.outcome || x.note },
      { get: actions, label: '' }], r.rows, 'No follow-ups in this view.'));
}

clear(page).append(adminTabs('followups.html'),
  pageHead('High-dose follow-up', { sub: 'Fluoroscopy and interventional patients above a substantial radiation dose level: inform, check the skin after 2–4 weeks, record the outcome.' },
    h('select', { class: 'input', 'aria-label': 'Show', onchange: (e) => { status = e.target.value; load(); } },
      [['pending', 'To act on'], ['open', 'To inform'], ['informed', 'Informed'], ['checked', 'Checked'], ['dismissed', 'Dismissed'], ['', 'All']].map(([v, l]) => h('option', { value: v, selected: v === status }, l)))),
  out);
load();
