// QA and calibration register (Quality; the admin edits): per device the DAP-meter calibrations (with the correction
// factor: true DAP = displayed × factor, from that date until the next calibration), constancy and
// acceptance tests and annual checks, each with the date the next is due. What is overdue or due
// within 30 days is listed first and raises the "QA due" alert. Stored doses are never changed:
// the corrected DAP is shown beside the stored one on the study page and in the dose statement, and is the
// one used by statistics, DRL comparisons, exports, DAP alerts and follow-ups.

import '../components/dx-shell.js';
import { registerTabs } from '../components/admin-tabs.js';
import { h, clear, section, toast, pageHead } from '../core/dom.js';
import { confirmDialog } from '../core/confirm.js';
import { isAdmin, qaDue, deviceQA, addDeviceQA, deleteQA, physicalDevices } from '../core/api.js';
import { dataTable, kpiRow, tag, field } from '../core/tables.js';

const KINDS = { dap_calibration: 'DAP meter calibration', constancy: 'Constancy test', acceptance: 'Acceptance test', annual: 'Annual check', other: 'Other QA check' };
const page = document.getElementById('page');
const dueHost = h('div', {});
const regHost = h('div', {});
const admin = await isAdmin();  // adding and deleting entries is for the admin
let devs = [];
let deviceId = Number(new URLSearchParams(location.search).get('device')) || null;

async function loadDue() {
  let r;
  try { r = await qaDue(30); } catch (e) { clear(dueHost).append(h('p', { class: 'error' }, e.message)); return; }
  clear(dueHost).append(
    kpiRow([['Overdue', r.filter((x) => x.overdue).length], ['Due within 30 days', r.filter((x) => !x.overdue).length]]),
    dataTable([{ get: (x) => h('a', { href: `?device=${x.device_id}`, onclick: (e) => { e.preventDefault(); deviceId = x.device_id; draw(); } }, x.device || `Device ${x.device_id}`), label: 'Device' },
      { get: (x) => KINDS[x.kind] || x.kind, label: 'What' }, { key: 'done_on', label: 'Last done' }, { key: 'next_due', label: 'Due' },
      { get: (x) => tag(x.overdue ? `${-x.days} days overdue` : `in ${x.days} days`, x.overdue ? 'alert' : 'warn'), label: '' }], r, 'Nothing is due.'));
}

async function loadRegister() {
  if (!deviceId) { clear(regHost).append(h('p', { class: 'muted' }, 'Choose a device.')); return; }
  let rows;
  try { rows = await deviceQA(deviceId); } catch (e) { clear(regHost).append(h('p', { class: 'error' }, e.message)); return; }
  const kind = h('select', { class: 'input' }, Object.entries(KINDS).map(([k, l]) => h('option', { value: k }, l)));
  const done = h('input', { class: 'input', type: 'date', value: new Date().toISOString().slice(0, 10) });
  const next = h('input', { class: 'input', type: 'date' });
  const factor = h('input', { class: 'input num-input', type: 'number', step: 'any', min: 0.5, max: 2, placeholder: '1.00' });
  const result = h('input', { class: 'input', type: 'text', maxlength: 255, placeholder: 'result' });
  const note = h('input', { class: 'input', type: 'text', maxlength: 1024, placeholder: 'note' });
  const add = async () => {
    const body = { kind: kind.value, done_on: done.value, next_due: next.value || null, factor: factor.value ? Number(factor.value) : null, result: result.value || null, note: note.value || null };
    try { await addDeviceQA(deviceId, body); toast('Added'); loadRegister(); loadDue(); } catch (e) { toast(e.message); }
  };
  const del = async (x) => { if (await confirmDialog('Delete this entry? A wrong calibration factor is corrected by deleting it.', { title: 'Delete entry', label: 'Delete', danger: true })) { try { await deleteQA(x.id); loadRegister(); loadDue(); } catch (e) { toast(e.message); } } };
  clear(regHost).append(
    !admin ? null : h('div', { class: 'row qa-add' },
      field('Kind', kind), field('Done on', done), field('Next due', next), field('Factor (DAP meter only)', factor), field('Result', result), field('Note', note),
      h('button', { class: 'btn primary', type: 'button', onclick: add }, 'Add')),
    dataTable([{ get: (x) => KINDS[x.kind] || x.kind, label: 'Kind' }, { key: 'done_on', label: 'Done' }, { key: 'next_due', label: 'Next due' }, { key: 'factor', label: 'Factor', right: true }, { key: 'result', label: 'Result' }, { key: 'note', label: 'Note' }, { key: 'entered_by', label: 'By' },
      { get: (x) => (admin ? h('button', { class: 'btn xs ghost danger', type: 'button', onclick: () => del(x) }, 'Delete') : null), label: '' }], rows, 'Nothing recorded for this device.'));
}

function draw() {
  const sel = h('select', { class: 'input', 'aria-label': 'Device', onchange: (e) => { deviceId = Number(e.target.value) || null; loadRegister(); } },
    h('option', { value: '' }, '— choose —'),
    devs.map((d) => h('option', { value: d.id, selected: d.id === deviceId }, d.display_name)));
  clear(page).append(registerTabs('qa.html'),
    pageHead('QA and calibration register', { sub: 'Per device: DAP-meter calibrations with their correction factor (applied to the DAP in statistics, DRL comparisons, exports and alerts from the date done until the next calibration), constancy, acceptance and annual tests, and when the next is due.' }),
    section('Due', { level: 2 }, dueHost), section('Register', { level: 2, actions: sel }, regHost));
  loadDue();
  loadRegister();
}

try { devs = (await physicalDevices()).filter((d) => d.origin === 'local'); } catch { devs = []; }
draw();
