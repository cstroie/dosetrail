// Alert rules: enable, threshold, severity and notification per rule (admin role; every
// change is audit-logged). Seeding never overwrites a rule edited here.

import '../components/dt-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, toast } from '../core/dom.js';
import { alertRules, updateAlertRule } from '../core/api.js';

const page = document.getElementById('page');
const KIND = { threshold: 'Threshold', drl_ratio: '× national DRL', cumulative: 'Cumulative / patient', count: 'Studies / patient' };

async function render() {
  clear(page).append(adminTabs('rules.html'));
  let rules;
  try { rules = await alertRules(); } catch (e) { page.append(h('p', { class: 'error' }, e.message)); return; }
  const body = h('tbody');
  for (const r of rules) {
    const enabled = h('input', { type: 'checkbox', checked: r.enabled, 'aria-label': 'Enabled' });
    const threshold = h('input', { class: 'input num-input', type: 'number', step: 'any', value: r.threshold, 'aria-label': 'Threshold', style: { width: '90px' } });
    const severity = h('select', { class: 'input', 'aria-label': 'Severity' }, ['warn', 'alert'].map((s) => h('option', { value: s, selected: s === r.severity }, s)));
    const notifyBox = h('input', { type: 'checkbox', checked: r.notify, 'aria-label': 'Notify' });
    const save = h('button', { class: 'btn xs', type: 'button' }, 'Save');
    save.addEventListener('click', async () => {
      try {
        const res = await updateAlertRule(r.id, { enabled: enabled.checked, threshold: Number(threshold.value), severity: severity.value, notify: notifyBox.checked });
        toast(Object.keys(res.changes).length ? `Saved: ${Object.keys(res.changes).join(', ')}` : 'No change');
      } catch (e) { toast(e.message); }
    });
    const scope = [r.modality, r.max_age_days ? `age < ${Math.round(r.max_age_days / 365.25)} y` : null, r.window_days ? `${r.window_days} d` : null].filter(Boolean).join(' · ');
    body.append(h('tr', {},
      h('td', {}, enabled), h('td', { title: r.note || '' }, h('div', {}, r.name), h('div', { class: 'muted small' }, r.note || '')),
      h('td', {}, KIND[r.kind] || r.kind), h('td', {}, scope || 'all'),
      h('td', {}, threshold, ' ', h('span', { class: 'muted small' }, r.unit || (r.kind === 'drl_ratio' ? '× DRL' : r.kind === 'count' ? 'studies' : ''))),
      h('td', {}, severity), h('td', {}, notifyBox), h('td', {}, save)));
  }
  page.append(h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, ['On', 'Rule', 'Kind', 'Scope', 'Threshold', 'Severity', 'Notify', ''].map((c) => h('th', {}, c)))), body))),
  h('p', { class: 'muted small' }, 'Notifications go to the channels set in [notify] of dosimetrix.toml (e-mail, ntfy), for studies of the last 48 h, without patient identifiers.'));
}

render();
