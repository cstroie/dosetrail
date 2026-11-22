// Alerts: every alert raised by the rules (Admin › Alert rules), filtered by status, rule,
// level, modality and study date. Select rows and acknowledge them with a note; the note
// and who acknowledged are kept on the alert and in the audit log.

import '../components/dt-shell.js';
import { h, clear, append, toast } from '../core/dom.js';
import { num } from '../core/format.js';
import { alertList, alertSummary, ackAlerts, studyHref } from '../core/api.js';
import { prefs } from '../core/prefs.js';

const page = document.getElementById('page');
const PAGE = 100;
const f = { status: 'open', rule: '', level: '', modality: '', since: '', until: '', ...(prefs.get('alerts.filters') || {}) };
let offset = 0;
const selected = new Set();

const params = () => Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ''));

async function load() {
  prefs.set('alerts.filters', f);
  selected.clear();
  let sum;
  let rows;
  try {
    [sum, rows] = await Promise.all([alertSummary(params()), alertList({ ...params(), limit: PAGE, offset })]);
  } catch (e) {
    clear(page).append(h('p', { class: 'error' }, e.message));
    return;
  }
  draw(sum, rows);
}

function select(label, key, options) {
  return h('label', { class: 'chk' }, label, ' ', h('select', { class: 'input', onchange: (e) => { f[key] = e.target.value; offset = 0; load(); } },
    options.map(([v, l]) => h('option', { value: v, selected: f[key] === v }, l))));
}

function draw(sum, rows) {
  const ackBar = h('div', { class: 'toolbar' });
  const note = h('input', { class: 'input', placeholder: 'note (e.g. reviewed with the radiologist, justified)', style: { flex: '1 1 320px' } });
  const ackBtn = h('button', { class: 'btn primary', type: 'button', disabled: true }, 'Acknowledge selected');
  const count = h('span', { class: 'muted small' });
  const refreshBar = () => {
    ackBtn.disabled = !selected.size;
    count.textContent = selected.size ? `${selected.size} selected` : '';
  };
  ackBtn.addEventListener('click', async () => {
    try {
      const r = await ackAlerts([...selected], note.value || null);
      toast(`${r.acknowledged} alerts acknowledged`);
      load();
    } catch (e) { toast(e.message); }
  });
  ackBar.append(note, ackBtn, count);

  const all = h('input', { type: 'checkbox', 'aria-label': 'Select all shown open alerts', onchange: (e) => {
    for (const r of rows) if (r.status === 'open') { if (e.target.checked) selected.add(r.id); else selected.delete(r.id); }
    body.querySelectorAll('input[type=checkbox]').forEach((c) => { c.checked = selected.has(Number(c.value)); });
    refreshBar();
  } });
  const body = h('tbody', {}, rows.map((a) => h('tr', { class: a.level === 'alert' && a.status === 'open' ? 'hot' : '' },
    h('td', {}, a.status === 'open' ? h('input', { type: 'checkbox', value: a.id, onchange: (e) => {
      if (e.target.checked) selected.add(a.id); else selected.delete(a.id);
      refreshBar();
    } }) : ''),
    h('td', { class: 'mono small' }, (a.time || '').replace('T', ' ')),
    h('td', {}, h('span', { class: `tag ${a.level}` }, a.kind)),
    h('td', {}, h('a', { href: studyHref({ id: a.study_id }) }, a.title), h('div', { class: 'muted small' }, a.rule_name)),
    h('td', { class: 'mono' }, a.modality || ''),
    h('td', { class: 'r' }, h('span', { class: 'mono' }, a.value), h('div', { class: 'muted small' }, a.limit)),
    h('td', {}, a.status === 'open' ? h('span', { class: 'tag warn' }, 'open')
      : h('span', { title: a.ack_note || '' }, h('span', { class: 'tag ok' }, 'ack'), ` ${a.ack_by || ''} ${a.ack_at ? a.ack_at.slice(0, 10) : ''}`,
        a.ack_note ? h('div', { class: 'muted small' }, a.ack_note) : null)))));
  if (!rows.length) body.append(h('tr', {}, h('td', { colspan: 7, class: 'empty' }, 'No alert matches these filters.')));

  const rules = [['', 'all rules'], ...sum.by_rule.map((r) => [r.rule, `${r.name} (${r.n})`])];
  if (f.rule && !sum.by_rule.some((r) => r.rule === f.rule)) rules.push([f.rule, f.rule]);
  const dates = ['since', 'until'].map((k) => h('label', { class: 'chk' }, k, ' ', h('input', { class: 'input', type: 'date', value: f[k],
    onchange: (e) => { f[k] = e.target.value; offset = 0; load(); } })));
  const pager = h('div', { class: 'toolbar', style: { 'margin-top': 'var(--sp-2)' } },
    h('button', { class: 'btn', type: 'button', disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - PAGE); load(); } }, '‹ Newer'),
    h('span', { class: 'muted small' }, `${num(offset + 1)}–${num(offset + rows.length)} of ${num(sum.total)}`),
    h('button', { class: 'btn', type: 'button', disabled: offset + rows.length >= sum.total, onclick: () => { offset += PAGE; load(); } }, 'Older ›'));

  append(clear(page), [
    h('div', { class: 'kpis drl-kpis' },
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, f.status === 'all' ? 'All alerts' : `${f.status[0].toUpperCase()}${f.status.slice(1)}`), h('span', { class: 'v' }, num(sum.total)), h('span', { class: 'm' }, 'matching the filters')),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Alert level'), h('span', { class: 'v' }, num(sum.by_level.alert || 0)), h('span', { class: 'm' }, 'severity “alert”')),
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Warnings'), h('span', { class: 'v' }, num(sum.by_level.warn || 0)), h('span', { class: 'm' }, 'severity “warn”')),
      sum.by_rule.slice(0, 3).map((r) => h('div', { class: 'kpi', title: r.name }, h('span', { class: 'code' }, r.rule), h('span', { class: 'v' }, num(r.n)), h('span', { class: 'm' }, r.name)))),
    h('div', { class: 'toolbar' },
      select('Status', 'status', [['open', 'open'], ['acknowledged', 'acknowledged'], ['all', 'all']]),
      select('Rule', 'rule', rules),
      select('Level', 'level', [['', 'all'], ['alert', 'alert'], ['warn', 'warning']]),
      select('Modality', 'modality', [['', 'all'], ...['CT', 'DX', 'CR', 'RF', 'XA', 'MG'].map((m) => [m, m])]),
      ...dates,
      h('a', { class: 'btn ghost', href: 'rules.html' }, 'Rules…')),
    f.status !== 'acknowledged' ? ackBar : null,
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, h('th', {}, all), ['Study date', 'Kind', 'Study · rule', 'Mod.', 'Value · limit', 'Status'].map((c, i) => h('th', { class: i === 4 ? 'r' : '' }, c)))), body))),
    pager]);
}

load();
