// Alerts (admin): the notification channels (e-mail, ntfy: settings, a test message, what was
// sent or failed), then the rules: enable, threshold, severity and notify per rule, add and
// delete for the ones an admin created. Admin role; every change is audit-logged. Passwords
// and tokens are write-only. Seeding never overwrites a rule edited here.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { confirmDialog } from '../core/confirm.js';
import { h, clear, section, toast } from '../core/dom.js';
import { alertRules, updateAlertRule, createAlertRule, deleteAlertRule, alertRuleMetrics, notifySettings, saveNotify, testNotify, modalities } from '../core/api.js';
import { shownUnit } from '../core/format.js';

const page = document.getElementById('page');
const KIND = { threshold: 'Threshold', drl_ratio: '× national DRL', cumulative: 'Cumulative / patient', count: 'Studies / patient', pregnant: 'Pregnant patient', protocol_band: 'Protocol band ≠ patient', device_silent: 'Silent device', device_drift: 'Device dose drift', device_di: 'Room deviation index', drl_sustained: 'Room above DRL, 2 quarters' };

// The kinds a rule can be added for here (device rules are seeded: they need extra settings)
const NEW_KINDS = ['threshold', 'cumulative', 'count', 'drl_ratio', 'pregnant'];
const field = (label, input, hint) => h('label', { class: 'field' }, label, input, hint ? h('span', { class: 'muted small' }, hint) : null);

async function addRule() {
  let metrics;
  let groups;
  try { [metrics, groups] = await Promise.all([alertRuleMetrics(), modalities()]); } catch (e) { toast(e.message); return; }
  let dlg = document.getElementById('rule-dlg');
  if (!dlg) { dlg = h('dialog', { id: 'rule-dlg', class: 'panel', style: { padding: 'var(--sp-3)', 'min-width': '520px' } }); document.body.append(dlg); }
  const kind = h('select', { class: 'input' }, NEW_KINDS.map((k) => h('option', { value: k }, KIND[k])));
  const name = h('input', { class: 'input', type: 'text', maxlength: 255, placeholder: 'e.g. CT: CTDIvol ≥ 100 mGy' });
  const metric = h('select', { class: 'input' });
  // the modality groups of the other pages (DX/CR together); none ticked: every modality
  const modBoxes = groups.map((p) => h('input', { type: 'checkbox', value: p.code }));
  const modality = h('span', { class: 'row', style: { gap: '8px', 'flex-wrap': 'wrap' } },
    modBoxes.map((b, i) => h('label', { class: 'chk' }, b, ` ${groups[i].label}`)));
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
    const body = { kind: k, name: name.value, modality: modBoxes.filter((b) => b.checked).map((b) => b.value).join(',') || null, severity: severity.value, notify: notifyBox.checked,
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
  dlg.replaceChildren(section('Add alert rule', { sub: 'Applies to studies evaluated from now on. For earlier ones run: dozimetrix alerts evaluate --since YYYY-MM-DD' },
    h('div', { style: { display: 'grid', 'grid-template-columns': '1fr 1fr', gap: 'var(--sp-2)' } },
      field('Kind', kind), field('Name', name), rows.metric, rows.threshold, rows.window,
      h('div', { class: 'field', role: 'group', 'aria-label': 'Modality' }, 'Modality', modality, h('span', { class: 'muted small' }, 'none ticked: all')), field('Younger than (years)', ageYears), field('Severity', severity),
      field('Notify', notifyBox, 'by e-mail / ntfy'), field('Note', note)),
    h('div', { class: 'row jc-end' }, h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, 'Cancel'), save)));
  sync();
  dlg.showModal();
}

// ---- notification channels
function notifyPanel(n) {
  const f = (k) => n.fields[k];
  const src = (k) => (f(k).source === 'toml' ? 'from dozimetrix.toml' : '');
  const inp = (k, extra = {}) => h('input', { class: 'input', type: 'text', value: f(k).value ?? '', ...extra });
  const chk = (k) => h('input', { type: 'checkbox', checked: !!f(k).value });
  const pw = (isSet, label) => h('input', { class: 'input', type: 'password', autocomplete: 'new-password',
    placeholder: isSet ? `${label} set: leave empty to keep` : `${label} not set` });
  const e = {
    email_to: inp('email_to', { placeholder: 'physics@example.org, rso@example.org', value: (f('email_to').value || []).join(', ') }),
    email_from: inp('email_from'), smtp_host: inp('smtp_host'), smtp_port: inp('smtp_port', { type: 'number', value: f('smtp_port').value ?? 587 }),
    smtp_starttls: chk('smtp_starttls'), smtp_ssl: chk('smtp_ssl'), smtp_user: inp('smtp_user', { autocomplete: 'off' }),
    smtp_password: pw(n.smtp_password_set, 'Password'),
  };
  const nt = { ntfy_url: inp('ntfy_url', { placeholder: 'the ntfy topic address' }), ntfy_token: pw(n.ntfy_token_set, 'Token') };
  const fld = (label, el, k, cls = '') => h('label', { class: `field ${cls}` }, label, el, k && src(k) ? h('span', { class: 'muted small' }, src(k)) : null);
  const tick = (label, el) => h('label', { class: 'field check' }, el, label);
  const tag = (on) => h('span', { class: `tag ${on ? 'ok' : 'warn'}` }, on ? 'configured' : 'not configured');

  const collect = (els) => {
    const out = {};
    for (const [k, el] of Object.entries(els)) {
      if (el.type === 'checkbox') out[k] = el.checked;
      else if (k === 'email_to') out[k] = el.value.split(/[,;\s]+/).filter(Boolean);
      else if (el.type === 'number') out[k] = el.value === '' ? null : Number(el.value);
      else if (k === 'smtp_password' || k === 'ntfy_token') { if (el.value) out[k] = el.value; } else out[k] = el.value;
    }
    return out;
  };
  const act = (label, run) => {
    const b = h('button', { class: 'btn xs', type: 'button' }, label);
    b.addEventListener('click', async () => { b.disabled = true; try { await run(); } catch (err) { toast(err.message); } b.disabled = false; });
    return b;
  };
  const save = (els, extra = {}) => act('Save', async () => {
    const r = await saveNotify({ ...collect(els), ...extra });
    toast(Object.keys(r.changes).length ? `Saved: ${Object.keys(r.changes).join(', ')}` : 'No change');
    render();
  });
  const test = (channel, on) => act('Send test', async () => {
    if (!on) { toast('Save the settings first'); return; }
    const r = await testNotify(channel);
    toast(r.ok ? `${channel}: test message sent` : `${channel} failed: ${r.error}`);
    render();
  });
  const st = n.status;
  const none = !n.channels.length;
  return section('Notifications', {
    sub: (none
      ? 'No channel is configured: alerts on rules with Notify on are kept for 48 h and sent once a channel is set up.'
      : `Rules with Notify on send to: ${n.channels.join(', ')}. Messages carry no patient identifiers, only the exam, the dose and a link (login needed; the web address is set in Admin › Settings).`)
      + ` ${st.waiting} waiting · ${st.sent_24h} sent in the last 24 h · ${st.failed} failed${st.last_error ? ` · last error: ${st.last_error.slice(0, 160)}` : ''}` },
    h('div', { class: 'notify-cards' },
      h('div', { class: 'panel form-card' },
        h('div', { class: 'row' }, h('strong', {}, 'E-mail'), h('span', { class: 'spacer' }), tag(n.channels.includes('email'))),
        h('div', { class: 'fields' },
          fld('To', e.email_to, 'email_to', 'wide'), fld('From', e.email_from, 'email_from', 'wide'),
          fld('SMTP host', e.smtp_host, 'smtp_host'), fld('Port', e.smtp_port, 'smtp_port'),
          fld('User', e.smtp_user, 'smtp_user'), fld('Password', e.smtp_password),
          h('div', { class: 'checks' }, tick('STARTTLS', e.smtp_starttls), tick('SSL (port 465)', e.smtp_ssl))),
        h('div', { class: 'row' }, save(e), test('email', n.channels.includes('email')),
          n.smtp_password_set ? act('Remove password', async () => { await saveNotify({ clear_smtp_password: true }); render(); }) : '')),
      h('div', { class: 'panel form-card' },
        h('div', { class: 'row' }, h('strong', {}, 'ntfy'), h('span', { class: 'spacer' }), tag(n.channels.includes('ntfy'))),
        h('div', { class: 'fields' },
          fld('Topic address', nt.ntfy_url, 'ntfy_url', 'wide'), fld('Access token (optional)', nt.ntfy_token, null, 'wide')),
        h('div', { class: 'row' }, save(nt), test('ntfy', n.channels.includes('ntfy')),
          n.ntfy_token_set ? act('Remove token', async () => { await saveNotify({ clear_ntfy_token: true }); render(); }) : ''))));
}

async function render() {
  clear(page).append(adminTabs('rules.html'));
  try { page.append(notifyPanel(await notifySettings())); } catch (e) { page.append(h('p', { class: 'error' }, e.message)); }
  let rules;
  try { rules = await alertRules(); } catch (e) { page.append(h('p', { class: 'error' }, e.message)); return; }
  const body = h('tbody');
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
  page.append(section('Rules', {
    sub: 'Notifications go to the channels set above, for studies of the last 48 h, without patient identifiers.',
    actions: h('button', { class: 'btn primary', type: 'button', onclick: addRule }, 'Add rule') },
  h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, ['On', 'Rule', 'Kind', 'Scope', 'Threshold', 'Severity', 'Notify', ''].map((c) => h('th', {}, c)))), body)))));
}

render();
