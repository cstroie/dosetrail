// DICOM nodes: this DoseTrail's AE and receiver status, the remote nodes (PACS to query,
// modalities allowed to send), C-ECHO, and PACS sync coverage. Changes need the admin role
// and are audit-logged; the receiver picks up node changes within a minute.

import '../components/dt-shell.js';
import { h, clear, append, toast } from '../core/dom.js';
import { num } from '../core/format.js';
import { dicomStatus, createNode, updateNode, deleteNode, echoNode } from '../core/api.js';

const page = document.getElementById('page');
const EMPTY = { name: '', aet: '', host: '', port: 104, enabled: true, query_retrieve: false, sends: true,
  retrieve: 'get', move_destination: '', modalities: '', max_results: null, timeout: 60, sync_days: null, notes: '' };

function tabs() {
  return h('nav', { class: 'tabs', 'aria-label': 'Administration', style: { 'margin-bottom': 'var(--sp-3)' } },
    h('a', { href: 'devices.html' }, 'Devices'), h('a', { href: 'dicom.html', 'aria-current': 'page' }, 'DICOM nodes'),
    h('a', { href: 'review.html' }, 'Protocol mappings'), h('a', { href: 'drl.html' }, 'DRLs'),
    h('a', { href: 'quality.html' }, 'Data quality'), h('a', { href: 'rules.html' }, 'Alert rules'));
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
    h('dialog', { id: 'node-dlg', class: 'panel', style: { padding: 'var(--sp-3)', 'min-width': '520px' } })]);
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
