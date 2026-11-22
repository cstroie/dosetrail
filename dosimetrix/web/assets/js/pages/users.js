// Users: accounts, rename, roles, enable/disable, password reset, removal. Admin only; changes
// are audit-logged (never the password). You cannot disable yourself or drop your own admin
// role; only users without the admin role can be removed.

import '../components/dt-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, append, toast } from '../core/dom.js';
import { users, createUser, updateUser, deleteUser } from '../core/api.js';

const page = document.getElementById('page');

function rolePicker(all, selected) {
  return h('span', { class: 'roles', style: { display: 'inline-flex', gap: '8px', 'flex-wrap': 'wrap' } },
    Object.entries(all).map(([r, help]) => h('label', { class: 'chk', title: help },
      h('input', { type: 'checkbox', value: r, checked: selected.includes(r) }), ` ${r}`)));
}
const picked = (el) => [...el.querySelectorAll('input:checked')].map((i) => i.value);

async function render() {
  let data;
  try { data = await users(); } catch (e) { clear(page).append(adminTabs('users.html'), h('p', { class: 'error' }, e.message)); return; }
  const body = h('tbody');
  for (const u of data.users) {
    const roles = rolePicker(data.roles, u.roles);
    const uname = h('input', { class: 'input mono', value: u.username, 'aria-label': 'Username', style: { width: '140px' } });
    const name = h('input', { class: 'input', value: u.name || '', style: { width: '160px' } });
    const active = h('input', { type: 'checkbox', checked: u.active, 'aria-label': 'Active' });
    const save = h('button', { class: 'btn xs', type: 'button' }, 'Save');
    save.addEventListener('click', async () => {
      try {
        const r = await updateUser(u.username, { username: uname.value, name: name.value, roles: picked(roles), active: active.checked });
        toast(Object.keys(r.changes).length ? `${r.username}: ${Object.keys(r.changes).join(', ')} saved` : 'No change');
        if (r.changes.username) render();
      } catch (err) { toast(err.message); }
    });
    const reset = h('button', { class: 'btn xs ghost', type: 'button' }, 'Set password…');
    reset.addEventListener('click', async () => {
      const pw = window.prompt(`New password for ${u.username} (at least 10 characters):`);
      if (!pw) return;
      try { await updateUser(u.username, { password: pw }); toast(`Password of ${u.username} changed`); } catch (err) { toast(err.message); }
    });
    const remove = h('button', { class: 'btn xs ghost', type: 'button', disabled: u.roles.includes('admin'),
      title: u.roles.includes('admin') ? 'Take away the admin role first' : 'Remove this account' }, 'Remove…');
    remove.addEventListener('click', async () => {
      if (!window.confirm(`Remove ${u.username}? Their audit history stays. Disabling keeps the account instead.`)) return;
      try { await deleteUser(u.username); toast(`Removed ${u.username}`); render(); } catch (err) { toast(err.message); }
    });
    body.append(h('tr', { class: u.active ? '' : 'muted' },
      h('td', {}, uname), h('td', {}, name), h('td', {}, roles), h('td', {}, active),
      h('td', { class: 'mono small' }, u.last_login ? u.last_login.replace('T', ' ') : 'never'),
      h('td', {}, save, ' ', reset, ' ', remove)));
  }
  const nu = { username: h('input', { class: 'input mono', placeholder: 'username', style: { width: '140px' } }),
    name: h('input', { class: 'input', placeholder: 'full name', style: { width: '160px' } }),
    password: h('input', { class: 'input', type: 'password', placeholder: 'password (≥ 10)', autocomplete: 'new-password', style: { width: '160px' } }),
    roles: rolePicker(data.roles, ['viewer']) };
  const add = h('button', { class: 'btn primary', type: 'button' }, 'Add user');
  add.addEventListener('click', async () => {
    try {
      await createUser({ username: nu.username.value, name: nu.name.value, password: nu.password.value, roles: picked(nu.roles) });
      toast(`Added ${nu.username.value}`);
      render();
    } catch (err) { toast(err.message); }
  });
  append(clear(page), [adminTabs('users.html'),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['User', 'Name', 'Roles', 'Active', 'Last login', ''].map((c) => h('th', {}, c)))), body))),
    h('h2', { class: 'section-title' }, 'New user'),
    h('div', { class: 'toolbar' }, nu.username, nu.name, nu.password, nu.roles, add),
    h('p', { class: 'muted small' }, Object.entries(data.roles).map(([r, help]) => `${r}: ${help}`).join(' · '))]);
}

render();
