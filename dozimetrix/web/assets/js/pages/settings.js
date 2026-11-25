// Settings (admin): what is the same for the whole site. The first card is editable and kept in
// the database, so a change applies without a restart (and is audit-logged): the site name and
// code, the web address people open Dozimetrix at (links in notifications), how long the DICOM
// spool keeps failed and archived objects. Below, read-only, what is set in dozimetrix.toml and
// needs a restart to change.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, toast } from '../core/dom.js';
import { siteSettings, saveSiteSettings } from '../core/api.js';

const page = document.getElementById('page');
const field = (label, input, hint, cls = '') => h('label', { class: `field ${cls}` }, label, input,
  hint ? h('span', { class: 'muted small' }, hint) : null);
const from = (f, what) => (f.source === 'toml' ? `from dozimetrix.toml${what ? `: ${what}` : ''}` : f.source === 'ui' ? 'set here' : '');

function editable(s) {
  const name = h('input', { class: 'input', type: 'text', maxlength: 255, value: s.site.name || '' });
  const code = h('input', { class: 'input mono', type: 'text', maxlength: 32, value: s.site.code || '' });
  const url = h('input', { class: 'input', type: 'text', value: s.base_url.value || '', placeholder: 'the address people use to open Dozimetrix' });
  const days = h('input', { class: 'input num-input', type: 'number', min: 0, max: 3650, step: 1, value: s.spool_retention_days.value ?? 0 });
  const save = h('button', { class: 'btn primary', type: 'button' }, 'Save');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const body = { name: name.value, code: code.value, base_url: url.value, spool_retention_days: days.value === '' ? null : Number(days.value) };
      const r = await saveSiteSettings(body);
      toast(Object.keys(r.changes).length ? `Saved: ${Object.keys(r.changes).join(', ')}` : 'No change');
      render();
    } catch (e) { toast(e.message); save.disabled = false; }
  });
  return h('div', { class: 'panel form-card', style: { 'max-width': '720px' } },
    h('div', { class: 'fields' },
      field('Site name', name, 'the institution this installation belongs to', 'wide'),
      field('Site code', code, 'up to 32 letters or digits, no spaces'),
      field('Web address', url, `${from(s.base_url) || 'not set'}: where people open Dozimetrix; notifications link to a study here`, 'wide'),
      field('Keep failed and archived DICOM objects (days)', days,
        `0 keeps everything · ${from(s.spool_retention_days, `${s.spool_retention_days.toml} days`)} · the receiver prunes hourly`, 'wide')),
    h('div', { class: 'row' }, save));
}

function readOnly(d) {
  const list = (v) => (Array.isArray(v) ? (v.length ? v.join(', ') : 'any') : String(v ?? '—'));
  const groups = [
    ['Program', [['Version', d.version], ['Configuration file', d.config_file || 'none found: built-in defaults']]],
    ['Database', [['Server', `${d.database.driver}${d.database.host ? ` on ${d.database.host}` : ''}`], ['Name', d.database.name]]],
    ['DICOM receiver', [['AE title', d.dicom.aet], ['Port', d.dicom.port], ['Listens on', d.dicom.bind], ['Allowed callers (toml)', list(d.dicom.allowed_aets)]]],
    ['Web server', [['Listens on', d.web.listen], ['Trusted proxies', list(d.web.trusted_proxies)], ['Allowed hosts', list(d.web.allowed_hosts)],
      ['Secure cookies (HTTPS only)', d.web.secure_cookies ? 'yes' : 'no'], ['Session length', `${d.web.session_hours} h`], ['Upload limit', `${d.web.max_upload_mb} MB`]]],
    ['Folders', [['Data', d.paths.data_dir], ['DICOM spool', d.paths.spool_dir], ['Site secret', d.paths.secret_file], ['Report specs', d.paths.specs_dir], ['Plugins', d.paths.plugins_dir]]],
  ];
  const body = h('tbody');
  for (const [title, rows] of groups) {
    body.append(h('tr', {}, h('th', { colspan: 2, class: 'muted small', style: { 'text-align': 'left', 'padding-top': 'var(--sp-3)' } }, title)));
    for (const [k, v] of rows) body.append(h('tr', {}, h('td', {}, k), h('td', { class: 'mono' }, String(v ?? '—'))));
  }
  return h('div', { class: 'panel', style: { 'max-width': '720px', padding: 'var(--sp-3)' } }, h('table', { class: 'grid' }, body));
}

async function render() {
  clear(page).append(adminTabs('settings.html'));
  let s;
  try { s = await siteSettings(); } catch (e) { page.append(h('p', { class: 'error' }, e.message)); return; }
  page.append(
    h('h2', { class: 'section-title' }, 'Site'), editable(s),
    h('h2', { class: 'section-title' }, 'Deployment'),
    h('p', { class: 'muted small' }, 'Set in dozimetrix.toml on the server; a change needs a restart of the service. Shown here so nobody has to open the file to check.'),
    readOnly(s.deployment));
}

render();
