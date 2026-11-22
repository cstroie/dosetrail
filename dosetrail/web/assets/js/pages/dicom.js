// Integration: DICOM (this DoseTrail's AE and receiver status, the remote nodes – PACS to query,
// modalities allowed to send –, C-ECHO, PACS sync coverage) and the HIS / RIS servers that
// studies are enriched from (patient, weight, height, request, indication). Changes need the
// admin role and are audit-logged; the receiver picks up node changes within a minute. The HIS
// password is write-only: the server only says whether one is set.

import '../components/dt-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, append, toast } from '../core/dom.js';
import { num } from '../core/format.js';
import { dicomStatus, createNode, updateNode, deleteNode, echoNode,
  hisStatus, createHisServer, updateHisServer, deleteHisServer, checkHisServer } from '../core/api.js';

const page = document.getElementById('page');
const HIS_EMPTY = { name: '', kind: 'hippobridge', base_url: '', username: '', enabled: true, timeout: 120,
  modalities: '', window_before_h: 72, window_after_h: 2, min_interval_s: 2, backfill_hours: '22-06', notes: '' };
const EMPTY = { name: '', aet: '', host: '', port: 104, enabled: true, query_retrieve: false, sends: true,
  retrieve: 'get', move_destination: '', modalities: '', max_results: null, timeout: 60, sync_days: null, notes: '' };

function tabs() {
  return adminTabs('dicom.html');
}

function ago(s) {
  if (s == null) return '';
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function serviceTag(st, what) {
  if (!st) return h('span', { class: 'tag' }, `${what}: never seen`);
  return st.running ? h('span', { class: 'tag ok' }, `${what}: running`)
    : h('span', { class: 'tag warn' }, `${what}: stopped · last ${ago(st.age_s)}`);
}

async function render() {
  let st;
  try { st = await dicomStatus(); } catch (e) { clear(page).append(tabs(), h('p', { class: 'error' }, e.message)); return; }
  let his;
  try { his = await hisStatus(); } catch (e) { his = { error: e.message }; }
  const rx = st.receiver;
  const senders = Object.entries(rx?.by_aet || {}).sort((a, b) => b[1].n - a[1].n);
  append(clear(page), [tabs(),
    h('div', { class: 'kpis drl-kpis' },
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'This DoseTrail'), h('span', { class: 'v mono' }, st.local.aet),
        h('span', { class: 'm' }, `port ${st.local.port} · ${st.local.bind}`)),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Receiver'), serviceTag(rx, 'receiver'),
        h('span', { class: 'm' }, rx ? `${num(rx.received)} objects since start · ${ago(rx.age_s)}` : 'start it with dosetrail receive')),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Accepted callers'),
        h('span', { class: 'v', style: { 'font-size': '14px' } }, st.local.allowed.length ? st.local.allowed.join(', ') : 'any'),
        h('span', { class: 'm' }, 'nodes marked “sends”, plus allowed_aets in dosetrail.toml')),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'PACS sync'), serviceTag(st.qr, 'qr'),
        h('span', { class: 'm' }, st.qr ? st.qr.message : 'no query/retrieve has run yet'))),
    senders.length ? h('p', { class: 'muted small' }, 'Received since the receiver started: ',
      senders.map(([aet, x]) => `${aet} ${num(x.n)} (last ${x.last?.slice(11, 16) || '—'})`).join(' · ')) : null,
    h('div', { class: 'toolbar', style: { 'margin-top': 'var(--sp-3)' } },
      h('h2', { class: 'section-title', style: { margin: 0 } }, 'Remote nodes'), h('span', { class: 'spacer' }),
      h('button', { class: 'btn primary', type: 'button', onclick: () => edit(null) }, 'Add node')),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, nodeTable(st.nodes))),
    h('h2', { class: 'section-title' }, 'PACS sync coverage · last 120 days'),
    coverage(st.coverage),
    hisSection(his),
    h('dialog', { id: 'node-dlg', class: 'panel', style: { padding: 'var(--sp-3)', 'min-width': '520px' } }),
    h('dialog', { id: 'his-dlg', class: 'panel', style: { padding: 'var(--sp-3)', 'min-width': '560px' } })]);
}

function nodeTable(list) {
  const body = h('tbody');
  for (const n of list) {
    const e = n.last_echo;
    const echoTag = !e ? h('span', { class: 'muted' }, 'not tested')
      : h('span', { class: `tag ${e.ok ? 'ok' : 'alert'}`, title: e.error || '' }, e.ok ? `OK ${e.ms} ms` : 'FAILED');
    const echoBtn = h('button', { class: 'btn xs', type: 'button', disabled: !n.host }, 'Echo');
    echoBtn.addEventListener('click', async () => {
      echoBtn.disabled = true;
      try {
        const r = await echoNode(n.id);
        toast(r.ok ? `C-ECHO ${n.aet}: OK in ${r.ms} ms` : `C-ECHO ${n.aet} failed: ${r.error}`);
        render();
      } catch (err) { toast(err.message); echoBtn.disabled = false; }
    });
    const del = h('button', { class: 'btn xs ghost', type: 'button' }, 'Delete');
    del.addEventListener('click', async () => {
      if (!window.confirm(`Delete DICOM node “${n.name}”?`)) return;
      try { await deleteNode(n.id); toast(`Deleted ${n.name}`); render(); } catch (err) { toast(err.message); }
    });
    body.append(h('tr', { class: n.enabled ? '' : 'muted' },
      h('td', {}, n.name, n.notes ? h('div', { class: 'muted small' }, n.notes) : null),
      h('td', { class: 'mono' }, n.aet), h('td', { class: 'mono' }, n.host ? `${n.host}:${n.port}` : '—'),
      h('td', {}, [n.query_retrieve ? h('span', { class: 'tag info' }, `query/retrieve · ${n.retrieve.toUpperCase()}`) : null, ' ',
        n.sends ? h('span', { class: 'tag info' }, 'sends to us') : null, n.enabled ? null : h('span', { class: 'tag' }, 'disabled')]),
      h('td', {}, n.query_retrieve ? [n.modalities || 'default', n.max_results ? ` · cap ${n.max_results}` : '', n.sync_days ? ` · sync ${n.sync_days} d` : ''] : ''),
      h('td', {}, echoTag, e ? h('div', { class: 'muted small' }, e.at.replace('T', ' ')) : null),
      h('td', {}, echoBtn, ' ', h('button', { class: 'btn xs', type: 'button', onclick: () => edit(n) }, 'Edit'), ' ', del)));
  }
  if (!list.length) body.append(h('tr', {}, h('td', { colspan: 7, class: 'empty' }, 'No remote node yet. Add your PACS (query/retrieve) and the modalities that send dose reports.')));
  return h('table', { class: 'grid' }, h('thead', {}, h('tr', {},
    ['Name', 'AE title', 'Address', 'Role', 'Query', 'Last C-ECHO', ''].map((c) => h('th', {}, c)))), body);
}

/* ---------------- HIS / RIS ---------------- */

const QUEUE_LABELS = { pending: 'waiting', matched: 'request found', patient_only: 'patient only',
  no_patient: 'patient not found', skipped: 'skipped', error: 'error' };

function hisSection(his) {
  const head = h('div', { class: 'toolbar', style: { 'margin-top': 'var(--sp-4)' } },
    h('h2', { class: 'section-title', style: { margin: 0 } }, 'HIS / RIS servers'), h('span', { class: 'spacer' }),
    his.error ? null : h('button', { class: 'btn primary', type: 'button', onclick: () => editHis(null) }, 'Add server'));
  if (his.error) return [head, h('p', { class: 'muted' }, his.error)];
  const w = his.worker;
  const queue = Object.entries(his.queue || {});
  return [head,
    h('p', { class: 'muted small' },
      w ? [h('span', { class: `tag ${w.stale ? 'warn' : 'ok'}` }, w.stale ? 'worker: not running' : 'worker: running'),
        ` last seen ${w.at.replace('T', ' ')}${w.message ? ` · ${w.message}` : ''}`]
        : 'Worker never seen: run dosetrail his run (or its service).',
      queue.length ? ` · Studies: ${queue.map(([k, n]) => `${QUEUE_LABELS[k] || k} ${num(n)}`).join(' · ')}` : ''),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, hisTable(his.servers)))];
}

function hisTable(list) {
  const body = h('tbody');
  for (const s of list) {
    const c = s.last_check;
    const tag = !c ? h('span', { class: 'muted' }, 'not tested')
      : h('span', { class: `tag ${c.ok ? 'ok' : 'alert'}`, title: c.error || '' }, c.ok ? `OK ${c.ms} ms` : 'FAILED');
    const test = h('button', { class: 'btn xs', type: 'button' }, 'Test');
    test.addEventListener('click', async () => {
      test.disabled = true;
      try {
        const r = await checkHisServer(s.id);
        toast(r.ok ? `${s.name}: login OK in ${r.ms} ms` : `${s.name}: ${r.error}`);
        render();
      } catch (err) { toast(err.message); test.disabled = false; }
    });
    const del = h('button', { class: 'btn xs ghost', type: 'button' }, 'Delete');
    del.addEventListener('click', async () => {
      if (!window.confirm(`Delete HIS server “${s.name}”? Matched requests stay; new studies are no longer looked up.`)) return;
      try { await deleteHisServer(s.id); toast(`Deleted ${s.name}`); render(); } catch (err) { toast(err.message); }
    });
    body.append(h('tr', { class: s.enabled ? '' : 'muted' },
      h('td', {}, s.name, s.notes ? h('div', { class: 'muted small' }, s.notes) : null),
      h('td', { class: 'mono' }, s.base_url, h('div', { class: 'muted small' }, s.kind)),
      h('td', {}, s.username || '—', ' ', s.password_set ? h('span', { class: 'tag' }, 'password set')
        : h('span', { class: 'tag warn' }, 'no password')),
      h('td', {}, s.modalities || 'all', h('div', { class: 'muted small' },
        `request −${s.window_before_h} h … +${s.window_after_h} h · ${s.min_interval_s} s between calls`)),
      h('td', {}, tag, c ? h('div', { class: 'muted small' }, c.at.replace('T', ' ')) : null),
      h('td', {}, test, ' ', h('button', { class: 'btn xs', type: 'button', onclick: () => editHis(s) }, 'Edit'), ' ', del)));
  }
  if (!list.length) body.append(h('tr', {}, h('td', { colspan: 6, class: 'empty' },
    'No HIS server yet. Add one to fill weight, height, sex, age and the request indication of new studies.')));
  return h('table', { class: 'grid' }, h('thead', {}, h('tr', {},
    ['Name', 'Address', 'Account', 'Studies looked up', 'Last test', ''].map((c) => h('th', {}, c)))), body);
}

function editHis(server) {
  const dlg = document.getElementById('his-dlg');
  const v = { ...HIS_EMPTY, ...(server || {}) };
  const inp = (k, type = 'text', extra = {}) => h('input', { class: 'input', type, value: v[k] ?? '', ...extra });
  const els = {
    name: inp('name'), base_url: inp('base_url', 'url', { placeholder: 'scheme://host:port', class: 'input mono' }),
    kind: h('select', { class: 'input' }, h('option', { value: 'hippobridge', selected: true }, 'HippoBridge (FHIR)')),
    username: inp('username', 'text', { autocomplete: 'off' }),
    password: h('input', { class: 'input', type: 'password', autocomplete: 'new-password',
      placeholder: server?.password_set ? 'unchanged (set)' : 'not set' }),
    enabled: h('input', { type: 'checkbox', checked: !!v.enabled }),
    modalities: inp('modalities', 'text', { placeholder: 'all, or e.g. DX,CR,CT,RF' }),
    window_before_h: inp('window_before_h', 'number'), window_after_h: inp('window_after_h', 'number'),
    min_interval_s: inp('min_interval_s', 'number', { step: '0.5' }), timeout: inp('timeout', 'number'),
    backfill_hours: inp('backfill_hours', 'text', { placeholder: '22-06' }), notes: inp('notes'),
  };
  const save = h('button', { class: 'btn primary', type: 'button' }, server ? 'Save' : 'Add');
  save.addEventListener('click', async () => {
    const data = {};
    for (const [k, el] of Object.entries(els)) {
      if (k === 'password') { if (el.value) data.password = el.value; continue; }
      if (el.type === 'checkbox') data[k] = el.checked;
      else if (el.type === 'number') data[k] = el.value === '' ? null : Number(el.value);
      else data[k] = el.value;
    }
    try {
      if (server) await updateHisServer(server.id, data); else await createHisServer(data);
      dlg.close();
      toast(server ? `Saved ${data.name}` : `Added ${data.name}: press Test to check the login`);
      render();
    } catch (err) { toast(err.message); }
  });
  const clearPw = server?.password_set ? h('button', { class: 'btn xs ghost', type: 'button', onclick: async () => {
    if (!window.confirm('Remove the stored password?')) return;
    try { await updateHisServer(server.id, { password: '' }); dlg.close(); toast('Password removed'); render(); } catch (err) { toast(err.message); }
  } }, 'Remove password') : null;
  clear(dlg).append(
    h('h2', { class: 'section-title' }, server ? `Edit ${server.name}` : 'Add HIS / RIS server'),
    h('div', { style: { display: 'grid', 'grid-template-columns': '1fr 1fr', gap: 'var(--sp-2)' } },
      field('Name', els.name), field('Type', els.kind),
      field('Base URL', els.base_url, 'the gateway, not the HIS itself'), field('Timeout (s)', els.timeout, 'HIS pages can be slow'),
      field('Username', els.username, 'a HIS account; read access is enough'),
      field('Password', els.password, 'stored encrypted; never shown again'),
      field('Modalities', els.modalities, 'empty = every modality'), field('Pause between calls (s)', els.min_interval_s, 'be gentle with the HIS'),
      field('Request window before the exam (h)', els.window_before_h), field('… and after (h)', els.window_after_h),
      field('History backfill hours', els.backfill_hours, 'start-end, e.g. 22-06 (night)'), field('Notes', els.notes),
      h('label', { class: 'chk' }, els.enabled, ' enabled'), clearPw),
    h('p', { class: 'muted small' }, 'Per device, HIS lookups can be limited to the patient or turned off in Admin → Devices.'),
    h('div', { class: 'toolbar', style: { 'margin-top': 'var(--sp-3)', 'justify-content': 'flex-end' } },
      h('button', { class: 'btn ghost', type: 'button', onclick: () => dlg.close() }, 'Cancel'), save));
  dlg.showModal();
}

function coverage(days) {
  const done = days.filter((d) => d.done).length;
  return h('div', { class: 'panel', style: { padding: 'var(--sp-3)' } },
    h('div', { style: { display: 'flex', gap: '2px', 'flex-wrap': 'wrap' } }, days.map((d) => h('span', {
      title: `${d.date}: ${d.done ? 'retrieved' : 'not retrieved (or incomplete)'}`,
      style: { width: '9px', height: '22px', 'border-radius': '2px', background: d.done ? 'var(--ok)' : 'var(--line-2)' } }))),
    h('p', { class: 'muted small' }, `${done} of ${days.length} days fully retrieved · ${days[0].date} → ${days[days.length - 1].date}. Today and yesterday are always queried again.`));
}

function field(label, input, hint) {
  return h('label', { class: 'field' }, label, input, hint ? h('span', { class: 'muted small' }, hint) : null);
}

function edit(node) {
  const dlg = document.getElementById('node-dlg');
  const v = { ...EMPTY, ...(node || {}) };
  const inp = (k, type = 'text', extra = {}) => h('input', { class: 'input', type, value: v[k] ?? '', ...extra });
  const chk = (k) => h('input', { type: 'checkbox', checked: !!v[k] });
  const els = {
    name: inp('name'), aet: inp('aet', 'text', { maxlength: 16, class: 'input mono' }), host: inp('host'),
    port: inp('port', 'number'), enabled: chk('enabled'), sends: chk('sends'), query_retrieve: chk('query_retrieve'),
    retrieve: h('select', { class: 'input' }, ['get', 'move'].map((m) => h('option', { value: m, selected: v.retrieve === m }, m === 'get' ? 'C-GET (no PACS setup)' : 'C-MOVE (PACS must know us)'))),
    move_destination: inp('move_destination'), modalities: inp('modalities', 'text', { placeholder: 'CT,DX,CR,RF,XA,MG,SR' }),
    max_results: inp('max_results', 'number'), timeout: inp('timeout', 'number'), sync_days: inp('sync_days', 'number'),
    notes: inp('notes'),
  };
  const save = h('button', { class: 'btn primary', type: 'button' }, node ? 'Save' : 'Add');
  save.addEventListener('click', async () => {
    const data = {};
    for (const [k, el] of Object.entries(els)) {
      if (el.type === 'checkbox') data[k] = el.checked;
      else if (el.type === 'number') data[k] = el.value === '' ? null : Number(el.value);
      else data[k] = el.value;
    }
    try {
      if (node) await updateNode(node.id, data); else await createNode(data);
      dlg.close();
      toast(node ? `Saved ${data.name}` : `Added ${data.name}`);
      render();
    } catch (err) { toast(err.message); }
  });
  clear(dlg).append(
    h('h2', { class: 'section-title' }, node ? `Edit ${node.name}` : 'Add DICOM node'),
    h('div', { style: { display: 'grid', 'grid-template-columns': '1fr 1fr', gap: 'var(--sp-2)' } },
      field('Name', els.name), field('AE title', els.aet), field('Host', els.host, 'not needed for a node that only sends'),
      field('Port', els.port),
      h('label', { class: 'chk' }, els.sends, ' sends to DoseTrail (accepted caller)'),
      h('label', { class: 'chk' }, els.query_retrieve, ' query/retrieve from it (PACS)'),
      field('Retrieve', els.retrieve), field('C-MOVE destination', els.move_destination, 'default: our AE title'),
      field('Modalities to query', els.modalities, 'one C-FIND per modality'), field('C-FIND result cap', els.max_results, 'capped days are split'),
      field('Timeout (s)', els.timeout), field('Sync window (days)', els.sync_days, 'scheduled sync looks back this far'),
      h('label', { class: 'chk' }, els.enabled, ' enabled'), field('Notes', els.notes)),
    h('div', { class: 'toolbar', style: { 'margin-top': 'var(--sp-3)', 'justify-content': 'flex-end' } },
      h('button', { class: 'btn ghost', type: 'button', onclick: () => dlg.close() }, 'Cancel'), save));
  dlg.showModal();
}

render();
