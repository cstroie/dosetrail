// Alert rules: enable, threshold, severity and notification per rule (admin role; every
// change is audit-logged). Seeding never overwrites a rule edited here.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { confirmDialog } from '../core/confirm.js';
import { h, clear, toast } from '../core/dom.js';
import { alertRules, updateAlertRule, createAlertRule, deleteAlertRule, alertRuleMetrics } from '../core/api.js';
import { shownUnit } from '../core/format.js';

const page = document.getElementById('page');
const KIND = { threshold: 'Threshold', drl_ratio: '× national DRL', cumulative: 'Cumulative / patient', count: 'Studies / patient', pregnant: 'Pregnant patient', device_silent: 'Silent device', device_drift: 'Device dose drift', drl_sustained: 'Room above DRL, 2 quarters' };

// The kinds a rule can be added for here (device rules are seeded: they need extra settings)
const NEW_KINDS = ['threshold', 'cumulative', 'count', 'drl_ratio', 'pregnant'];
const field = (label, input, hint) => h('label', { class: 'field' }, label, input, hint ? h('span', { class: 'muted small' }, hint) : null);

async function addRule() {
  let metrics;
  try { metrics = await alertRuleMetrics(); } catch (e) { toast(e.message); return; }
  let dlg = document.getElementById('rule-dlg');
  if (!dlg) { dlg = h('dialog', { id: 'rule-dlg', class: 'panel', style: { padding: 'var(--sp-3)', 'min-width': '520px' } }); document.body.append(dlg); }
  const kind = h('select', { class: 'input' }, NEW_KINDS.map((k) => h('option', { value: k }, KIND[k])));
  const name = h('input', { class: 'input', type: 'text', maxlength: 255, placeholder: 'e.g. CT: CTDIvol ≥ 100 mGy' });
  const metric = h('select', { class: 'input' });
  const modality = h('input', { class: 'input', type: 'text', placeholder: 'all, or e.g. CT,RF' });
  const threshold = h('input', { class: 'input num-input', type: 'number', step: 'any', min: 0 });
  const unitTag = h('span', { class: 'muted small' });
  const windowDays = h('input', { class: 'input num-input', type: 'number', min: 1, step: 1, value: 365 });
  const ageYears = h('input', { class: 'input num-input', type: 'number', min: 0, step: 'any', placeholder: 'any age' });
  const severity = h('select', { class: 'input' }, ['warn', 'alert'].map((s) => h('option', { value: s }, s)));
  const notifyBox = h('input', { type: 'checkbox' });
  const note = h('input', { class: 'input', type: 'text', maxlength: 1024 });
  const rows = {
    metric: field('Metric', metric), threshold: field('Threshold', h('span', { class: 'row' }, threshold, ' ', unitTag)),
    window: field('Window (days)', windowDays, 'per patient; alerts once per window'),
  };
  const sync = () => {
    const k = kind.value;
    const list = metrics.filter((m) => k !== 'cumulative' || m.level === 'study');
    const keep = metric.value;
    metric.replaceChildren(...list.map((m) => h('option', { value: m.key, selected: m.key === keep }, `${m.label}${m.level === 'event' ? ' (highest value in the study)' : ''}`)));
    const m = metrics.find((x) => x.key === metric.value);
    const su = shownUnit(m?.unit);
    unitTag.textContent = k === 'threshold' || k === 'cumulative' ? (m?.unit && su.unit) || '' : k === 'count' ? 'studies' : k === 'drl_ratio' ? '× national DRL' : '';
    rows.metric.hidden = !(k === 'threshold' || k === 'cumulative');
    rows.threshold.hidden = k === 'pregnant';
    rows.window.hidden = !(k === 'cumulative' || k === 'count');
  };
  kind.addEventListener('change', sync);
  metric.addEventListener('change', sync);
  const save = h('button', { class: 'btn primary', type: 'button' }, 'Add rule');
  save.addEventListener('click', async () => {
    const k = kind.value;
    const m = metrics.find((x) => x.key === metric.value);
    const body = { kind: k, name: name.value, modality: modality.value || null, severity: severity.value, notify: notifyBox.checked,
      note: note.value || null, max_age_days: ageYears.value === '' ? null : Math.round(Number(ageYears.value) * 365.25) };
    if (k === 'threshold' || k === 'cumulative') body.metric = metric.value;
    if (k !== 'pregnant') body.threshold = Number(threshold.value) / ((k === 'threshold' || k === 'cumulative') ? shownUnit(m?.unit).f : 1);
    if (k === 'cumulative' || k === 'count') body.window_days = Number(windowDays.value);
    try {
      const r = await createAlertRule(body);
      dlg.close();
      toast(`Added “${r.name}”. It applies to studies evaluated from now on.`);
      render();
    } catch (e) { toast(e.message); }
  });
  dlg.replaceChildren(h('h2', { class: 'section-title' }, 'Add alert rule'),
    h('div', { style: { display: 'grid', 'grid-template-columns': '1fr 1fr', gap: 'var(--sp-2)' } },
      field('Kind', kind), field('Name', name), rows.metric, rows.threshold, rows.window,
      field('Modality', modality, 'comma list'), field('Younger than (years)', ageYears), field('Severity', severity),
      field('Notify', notifyBox, 'by e-mail / ntfy'), field('Note', note)),
    h('p', { class: 'muted small' }, 'Applies to studies evaluated from now on. For earlier ones run: dozimetrix alerts evaluate --since YYYY-MM-DD'),
    h('div', { class: 'row jc-end' }, h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'Cancel'), save));
  sync();
  dlg.showModal();
}

async function render() {
  clear(page).append(adminTabs('rules.html'));
  let rules;
  try { rules = await alertRules(); } catch (e) { page.append(h('p', { class: 'error' }, e.message)); return; }
  const body = h('tbody');
  page.append(h('div', { class: 'toolbar' }, h('span', { class: 'spacer' }),
    h('button', { class: 'btn primary', type: 'button', onclick: addRule }, 'Add rule')));
  for (const r of rules) {
    const enabled = h('input', { type: 'checkbox', checked: r.enabled, 'aria-label': 'Enabled' });
    const su = shownUnit(r.unit);  // entered in mGy / mGy·cm², stored in the rule's unit (Gy, Gy·cm²)
    const threshold = h('input', { class: 'input num-input w-90', type: 'number', step: 'any', value: r.threshold == null ? '' : +(r.threshold * su.f).toPrecision(6), 'aria-label': 'Threshold' });
    const severity = h('select', { class: 'input', 'aria-label': 'Severity' }, ['warn', 'alert'].map((s) => h('option', { value: s, selected: s === r.severity }, s)));
    const notifyBox = h('input', { type: 'checkbox', checked: r.notify, 'aria-label': 'Notify' });
    const save = h('button', { class: 'btn xs', type: 'button' }, 'Save');
    save.addEventListener('click', async () => {
      try {
        const res = await updateAlertRule(r.id, { enabled: enabled.checked, threshold: Number(threshold.value) / su.f, severity: severity.value, notify: notifyBox.checked });
        toast(Object.keys(res.changes).length ? `Saved: ${Object.keys(res.changes).join(', ')}` : 'No change');
      } catch (e) { toast(e.message); }
    });
    const scope = [r.modality, r.max_age_days ? `age < ${Math.round(r.max_age_days / 365.25)} y` : null, r.window_days ? `${r.window_days} d` : null].filter(Boolean).join(' · ');
    body.append(h('tr', {},
      h('td', {}, enabled), h('td', { title: r.note || '' }, h('div', {}, r.name), h('div', { class: 'muted small' }, r.note || '')),
      h('td', {}, KIND[r.kind] || r.kind), h('td', {}, scope || 'all'),
      h('td', {}, r.kind === 'pregnant' ? h('span', { class: 'muted' }, '—') : [threshold, ' ', h('span', { class: 'muted small' }, (r.unit && su.unit) || (r.kind === 'drl_ratio' ? '× DRL' : r.kind === 'count' ? 'studies' : r.kind === 'device_drift' ? '× usual' : r.kind === 'drl_sustained' ? '× DRL' : ''))]),
      h('td', {}, severity), h('td', {}, notifyBox), h('td', {}, save, r.custom ? [' ', h('button', { class: 'btn xs ghost danger', type: 'button', onclick: async () => {
        if (!(await confirmDialog(`Delete “${r.name}” and the alerts it raised? This cannot be undone. To keep the history, switch the rule off instead.`, { title: 'Delete rule', label: 'Delete', danger: true }))) return;
        try { const d = await deleteAlertRule(r.id); toast(`Deleted ${d.deleted} (${d.alerts_removed} alerts)`); render(); } catch (e) { toast(e.message); }
      } }, 'Delete')] : '')));
  }
  page.append(h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, ['On', 'Rule', 'Kind', 'Scope', 'Threshold', 'Severity', 'Notify', ''].map((c) => h('th', {}, c)))), body))),
  h('p', { class: 'muted small' }, 'Notifications go to the channels set in [notify] of dozimetrix.toml (e-mail, ntfy), for studies of the last 48 h, without patient identifiers.'));
}

render();
