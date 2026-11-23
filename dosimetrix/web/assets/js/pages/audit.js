// Audit log: logins, identifier reveals, exports, reports and every administrative change.
// Filter by action, user, text and date; newest first, paged by "Older…".

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, append, toast } from '../core/dom.js';
import { auditLog } from '../core/api.js';

const page = document.getElementById('page');
const f = { action: '', user_name: '', text: '', since: '' };
let rows = [];
let more = false;

function detail(d) {
  if (!d) return '';
  const s = JSON.stringify(d);
  return h('span', { class: 'mono small', title: s }, s.length > 140 ? `${s.slice(0, 140)}…` : s);
}

async function load(append_ = false) {
  try {
    const params = { ...f, limit: 200 };
    if (append_ && rows.length) params.before_id = rows[rows.length - 1].id;
    const r = await auditLog(Object.fromEntries(Object.entries(params).filter(([, v]) => v !== '')));
    rows = append_ ? rows.concat(r.rows) : r.rows;
    more = r.more;
    draw(r.actions);
  } catch (e) { toast(e.message); }
}

let actionsCache = [];
function draw(actions) {
  if (actions) actionsCache = actions;
  const action = h('select', { class: 'input', onchange: (e) => { f.action = e.target.value; load(); } },
    h('option', { value: '' }, 'all actions'), actionsCache.map((a) => h('option', { value: a, selected: a === f.action }, a)));
  const user = h('input', { class: 'input', placeholder: 'user', value: f.user_name, onchange: (e) => { f.user_name = e.target.value.trim(); load(); }, style: { width: '120px' } });
  const text = h('input', { class: 'input', placeholder: 'target contains…', value: f.text, onchange: (e) => { f.text = e.target.value.trim(); load(); } });
  const since = h('input', { class: 'input', type: 'date', value: f.since, onchange: (e) => { f.since = e.target.value; load(); } });
  const body = h('tbody', {}, rows.map((r) => h('tr', {},
    h('td', { class: 'mono small' }, (r.at || '').replace('T', ' ')), h('td', { class: 'mono' }, r.user || '—'),
    h('td', {}, h('span', { class: `tag ${/identify|export|report/.test(r.action) ? 'warn' : r.action.startsWith('login_failed') ? 'alert' : 'info'}` }, r.action)),
    h('td', { class: 'mono small' }, r.target || ''), h('td', {}, detail(r.detail)), h('td', { class: 'mono small' }, r.from || ''))));
  if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 6, class: 'empty' }, 'No entry matches.')));
  append(clear(page), [adminTabs('audit.html'),
    h('div', { class: 'toolbar' }, action, user, text, since, h('span', { class: 'muted small' }, `${rows.length} entries shown`)),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['When', 'User', 'Action', 'Target', 'Detail', 'From'].map((c) => h('th', {}, c)))), body))),
    more ? h('button', { class: 'btn mt-2', type: 'button', onclick: () => load(true) }, 'Older…') : null]);
}

load();
