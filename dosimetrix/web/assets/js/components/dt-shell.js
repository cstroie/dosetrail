// <dt-shell page="studies" crumb="Studies" sub="CT">…page content…</dt-shell>
//
// Wraps a page in the icon rail and context bar. Children with slot="ctx" are moved into
// the context bar; everything else goes into the work area. Also owns the global shortcuts
// (Ctrl K and "/" open the command palette) and the theme / language buttons.

import { h, icon } from '../core/dom.js';
import { t, lang } from '../core/i18n.js';
import { prefs } from '../core/prefs.js';
import './dt-command-palette.js';
import { me, logout } from '../core/api.js';

const NAV = [
  ['overview', 'index.html', 'overview'],
  ['studies', 'studies.html', 'studies'],
  ['charts', 'charts.html', 'charts'],
  ['drl', 'drl.html', 'drl'],
  ['reports', 'reports.html', 'reports'],
  ['alerts', 'alerts.html', 'alerts'],
  ['admin', 'devices.html', 'admin'],
];

class DtShell extends HTMLElement {
  connectedCallback() {
    if (this._built) return;
    this._built = true;
    const page = this.getAttribute('page');
    const kids = [...this.childNodes];
    const ctxExtras = kids.filter((n) => n.nodeType === 1 && n.getAttribute('slot') === 'ctx');
    const work = h('main', { class: 'work', id: 'main' });
    kids.filter((n) => !ctxExtras.includes(n)).forEach((n) => work.append(n));

    const rail = h('nav', { class: 'rail', 'aria-label': 'Main' },
      h('span', { class: 'logo', role: 'img', 'aria-label': 'Dosimetrix' }, icon('logo', 26, 2)),
      NAV.map(([key, href, ic]) => h('a', { href, 'aria-current': key === page ? 'page' : false, title: t(`nav.${key}`) },
        key === 'alerts' ? h('span', { class: 'dot', 'aria-hidden': 'true' }) : null,
        icon(ic, 20), t(`nav.${key}.short`))));

    const crumb = h('span', { class: 'crumb' }, this.getAttribute('crumb') || t(`nav.${page}`) || '',
      this.getAttribute('sub') ? h('span', { class: 'muted' }, ` · ${this.getAttribute('sub')}`) : null);

    const palette = h('dt-command-palette');
    const theme = prefs.get('theme');
    const ctx = h('header', { class: 'ctx' },
      crumb,
      h('button', { class: 'cmdk-trigger', type: 'button', onclick: () => palette.open() },
        icon('search', 14, 2), h('span', {}, t('shell.search')), h('kbd', {}, 'Ctrl K')),
      ctxExtras.map((n) => { n.removeAttribute('slot'); return n; }),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('shell.lang'), title: t('shell.lang'),
        onclick: () => { prefs.set('lang', lang() === 'en' ? 'ro' : 'en'); location.reload(); } }, lang().toUpperCase()),
      h('button', { id: 'dt-theme-btn', class: 'icon-btn', type: 'button', 'aria-label': t(`shell.theme.${theme}`), title: t(`shell.theme.${theme}`),
        onclick: () => toggleTheme() }, icon(theme === 'dark' ? 'sun' : 'moon', 15, 2)),
      userMenu());

    const skip = h('a', { class: 'sr-only', href: '#main' }, 'Skip to content');
    this.append(skip, h('div', { class: 'shell' }, rail, h('div', { class: 'content' }, ctx, work)), palette);

    document.addEventListener('keydown', (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); palette.open(); }
      else if (e.key === '/' && !typing) { e.preventDefault(); palette.open(); }
    });
  }
}

// The signed-in user: initials on the button; the menu has the name, roles and sign-out.
function userMenu() {
  const btn = h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('shell.user'), 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, '·');
  const menu = h('div', { class: 'menu', role: 'menu', hidden: true });
  const wrap = h('span', { class: 'menu-wrap' }, btn, menu);
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    btn.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (e) => { if (!wrap.contains(e.target)) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  me().then((u) => {
    if (!u) {
      btn.textContent = '?';
      btn.title = t('shell.sample');
      menu.append(h('div', { class: 'menu-item muted' }, t('shell.sample')));
      return;
    }
    const label = u.name || u.username;
    const words = label.split(/[\s._-]+/).filter(Boolean);
    btn.textContent = (words.length > 1 ? words[0][0] + words[words.length - 1][0] : label.slice(0, 2)).toUpperCase();
    btn.title = `${label} (${u.username})`;
    menu.append(
      h('div', { style: { padding: '5px 6px' } }, h('strong', {}, label), h('div', { class: 'muted small' }, u.username),
        h('div', { class: 'muted small' }, `${t('shell.roles')}: ${u.roles.join(', ')}`)),
      u.roles.includes('admin') ? h('a', { class: 'menu-item', role: 'menuitem', href: 'users.html' }, t('shell.users')) : null,
      h('button', { class: 'menu-item', type: 'button', role: 'menuitem', onclick: () => logout() }, t('shell.signout')));
  }).catch(() => { btn.textContent = '?'; });
  return wrap;
}

export function toggleTheme() {
  const next = prefs.get('theme') === 'dark' ? 'light' : 'dark';
  prefs.set('theme', next);
  document.documentElement.dataset.theme = next;
  // refresh the theme button label/icon
  const btn = document.getElementById('dt-theme-btn');
  if (btn) {
    btn.replaceChildren(icon(next === 'dark' ? 'sun' : 'moon', 15, 2));
    btn.setAttribute('aria-label', t(`shell.theme.${next}`));
    btn.title = t(`shell.theme.${next}`);
  }
  document.dispatchEvent(new CustomEvent('dt-theme-change', { detail: next }));
}

customElements.define('dt-shell', DtShell);
