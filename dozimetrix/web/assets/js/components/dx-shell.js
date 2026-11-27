// <dx-shell page="studies" crumb="Studies" sub="CT">…page content…</dx-shell>
//
// Wraps a page in the icon rail and context bar. Children with slot="ctx" are moved into
// the context bar; everything else goes into the work area. Also owns the global shortcuts
// (Ctrl K and "/" open the command palette) and the theme / language buttons.

import { h, icon, section, toast } from '../core/dom.js';
import { t, lang } from '../core/i18n.js';
import { prefs } from '../core/prefs.js';
import './dx-command-palette.js';
import { me, logout, changePassword } from '../core/api.js';
import { live } from '../core/live.js';

// Overview is on the logo in the context bar. Doses, Analysis and Registers carry their own tab bars.
const NAV = [
  ['studies', 'studies.html', 'studies'],
  ['charts', 'charts.html', 'charts'],
  ['drl', 'drl.html', 'drl'],
  ['analysis', 'analysis.html', 'analysis'],
  ['quality', 'followups.html', 'quality'],
  ['alerts', 'alerts.html', 'alerts'],
];  // Admin lives in the account menu, for admins only

function switchLang() { prefs.set('lang', lang() === 'en' ? 'ro' : 'en'); location.reload(); }

class DtShell extends HTMLElement {
  /** The crumb's second part (e.g. the modality a page shows); null clears it. */
  setSub(text) {
    if (this._sub) this._sub.textContent = text ? ` · ${text}` : '';
    else if (text) this.setAttribute('sub', text);
    else this.removeAttribute('sub');
  }

  connectedCallback() {
    if (this._built) return;
    this._built = true;
    const page = this.getAttribute('page');
    const kids = [...this.childNodes];
    const ctxExtras = kids.filter((n) => n.nodeType === 1 && n.getAttribute('slot') === 'ctx');
    const work = h('main', { class: 'work', id: 'main' });
    kids.filter((n) => !ctxExtras.includes(n)).forEach((n) => work.append(n));

    const rail = h('nav', { class: 'rail', 'aria-label': 'Main' },
      h('a', { class: 'logo', href: 'index.html', 'aria-label': `Dozimetrix · ${t('nav.overview')}`, title: t('nav.overview'), 'aria-current': page === 'overview' ? 'page' : false }, icon('logo', 26, 2)),
      NAV.map(([key, href, ic]) => h('a', { href, 'aria-current': key === page ? 'page' : false, title: t(`nav.${key}`) },
        key === 'alerts' ? h('span', { class: 'dot', 'aria-hidden': 'true' }) : null,
        icon(ic, 20), t(`nav.${key}.short`))));

    // Registers (quality) are for the exporter and admin roles, as on the server
    me().then((u) => { if (u && !u.roles.some((r) => r === 'exporter' || r === 'admin')) rail.querySelector('a[href="followups.html"]')?.remove(); }).catch(() => {});

    this._sub = h('span', { class: 'muted' });
    const crumb = h('span', { class: 'crumb' }, this.getAttribute('crumb') || t(`nav.${page}`) || '', this._sub);
    this.setSub(this.getAttribute('sub'));

    const palette = h('dx-command-palette');
    const ctx = h('header', { class: 'ctx' },
      h('a', { class: 'ctx-logo', href: 'index.html', 'aria-label': `Dozimetrix · ${t('nav.overview')}`, title: t('nav.overview') }, icon('logo', 24, 2)),
      crumb,
      serviceBadge(),
      h('button', { class: 'cmdk-trigger', type: 'button', onclick: () => palette.open() },
        icon('search', 14, 2), h('span', { class: 'cmdk-label' }, t('shell.search')), h('kbd', {}, 'Ctrl K')),
      ctxExtras.map((n) => { n.removeAttribute('slot'); return n; }),
      h('button', { class: 'icon-btn ctx-wide', type: 'button', 'aria-label': t('shell.lang'), title: t('shell.lang'),
        onclick: switchLang }, lang().toUpperCase()),
      appearanceMenu(),
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

// A badge in the header while a background service is down or needs a look (live). Hidden
// when all is well; links to the overview, whose Services panel says more.
function serviceBadge() {
  const badge = h('a', { class: 'tag alert svc-badge', href: 'index.html#h-svc', style: { display: 'none' } });
  live(['services'], { services: (list) => {
    const bad = (list || []).filter((x) => x.state === 'alert' || x.state === 'warn');
    badge.style.display = bad.length ? '' : 'none';  // .tag sets its own display: no `hidden`
    if (!bad.length) return;
    badge.className = `tag ${bad.some((x) => x.state === 'alert') ? 'alert' : 'warn'} svc-badge`;
    badge.textContent = bad.length === 1 ? `${bad[0].name}: ${bad[0].state === 'alert' ? 'down' : 'check'}` : `${bad.length} services need a look`;
    badge.title = bad.map((x) => `${x.name}: ${x.detail || x.state}`).join('\n');
  } });
  return badge;
}

// The signed-in user: initials on the button; the menu has the name, roles and sign-out.
function userMenu() {
  const btn = h('button', { class: 'avatar-btn', type: 'button', 'aria-label': t('shell.user'), title: t('shell.user'), 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, '·');
  const menu = h('div', { class: 'menu account', role: 'menu', hidden: true });
  const wrap = h('span', { class: 'menu-wrap' }, btn, menu);
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    btn.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (e) => { if (!wrap.contains(e.target)) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  // On a phone the language and appearance buttons leave the bar and live here instead.
  const phoneItems = () => [
    h('div', { class: 'menu-sep ctx-narrow', role: 'separator' }),
    h('button', { class: 'menu-item ctx-narrow', type: 'button', role: 'menuitem', onclick: switchLang },
      icon('globe', 16, 1.8), `${t('shell.language')}: ${lang().toUpperCase()}`),
    h('button', { class: 'menu-item ctx-narrow', type: 'button', role: 'menuitem', onclick: (e) => {
      e.stopPropagation();
      const home = [...menu.childNodes];
      drawAppearance(menu, () => menu.replaceChildren(...home));
    } }, icon('palette', 16, 1.8), t('shell.appearance.short'))];
  menu.addEventListener('click', (e) => { if (e.target.closest('.appearance-view')) e.stopPropagation(); });
  me().then((u) => {
    if (!u) {
      btn.textContent = '?';
      btn.title = t('shell.sample');
      menu.append(h('div', { class: 'menu-item muted' }, t('shell.sample')), ...phoneItems());
      return;
    }
    const label = u.name || u.username;
    const words = label.split(/[\s._-]+/).filter(Boolean);
    btn.textContent = (words.length > 1 ? words[0][0] + words[words.length - 1][0] : label.slice(0, 2)).toUpperCase();
    btn.title = `${label} (${u.username})`;
    // as in Reporion: who (name, then the account and its roles), the account's own
    // actions with an icon each, a separator, sign out
    menu.append(
      h('div', { class: 'menu-item static account-info' }, icon('user', 18, 1.8),
        h('div', { class: 'menu-text' }, h('strong', {}, label),
          h('span', { class: 'muted small' }, `${u.username} · ${u.roles.join(', ')}`))),
      h('button', { class: 'menu-item', type: 'button', role: 'menuitem', onclick: () => { close(); passwordDialog(); } },
        icon('key', 16, 1.8), t('shell.password')),
      u.roles.includes('admin') ? h('a', { class: 'menu-item', role: 'menuitem', href: 'devices.html',
        'aria-current': document.querySelector('dx-shell')?.getAttribute('page') === 'admin' ? 'page' : false }, icon('admin', 16, 1.8), t('nav.admin')) : null,
      ...phoneItems(),
      h('div', { class: 'menu-sep', role: 'separator' }),
      h('button', { class: 'menu-item', type: 'button', role: 'menuitem', onclick: () => logout() }, icon('logout', 16, 1.8), t('shell.signout')));
  }).catch(() => { btn.textContent = '?'; });
  return wrap;
}

// Palettes (tokens in css/tokens.css); swatch = [background, accent] of the dark theme.
export const PALETTES = [
  ['film', 'Film', '#070B0E', '#9CB8D6'],
  ['iodine', 'Iodine', '#111114', '#AE99D6'],
  ['cherenkov', 'Cherenkov', '#0A0E13', '#3CC4D8'],
  ['phosphor', 'Phosphor', '#0F110E', '#C4E35A'],
];

function appearanceChanged() {
  document.dispatchEvent(new CustomEvent('dx-theme-change', { detail: prefs.get('theme') }));
}

export function setTheme(mode) {
  prefs.set('theme', mode);
  document.documentElement.dataset.theme = mode;
  appearanceChanged();
}

export function setPalette(name) {
  prefs.set('paletteV', 2);
  prefs.set('palette', name);
  document.documentElement.dataset.palette = name;
  appearanceChanged();
}

// One's own password: current, new, repeat (PUT /api/me/password; audited).
function passwordDialog() {
  const err = h('p', { class: 'error small', role: 'alert' });
  const field = (name, label, ac) => h('label', { class: 'pw-field' }, h('span', { class: 'muted small' }, label),
    h('input', { class: 'input', type: 'password', name, autocomplete: ac, required: true }));
  const form = h('form', { method: 'dialog', class: 'pw-form' },
    section(t('shell.password').replace('…', ''), {},
      field('current', t('shell.pw.current'), 'current-password'),
      field('next', t('shell.pw.new'), 'new-password'),
      field('repeat', t('shell.pw.repeat'), 'new-password'), err,
      h('div', { class: 'row jc-end' },
        h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, t('shell.pw.cancel')),
        h('button', { class: 'btn primary', type: 'submit' }, t('shell.pw.save')))));
  const dlg = h('dialog', { class: 'pw-dialog' }, form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(form));
    if (d.next !== d.repeat) { err.textContent = t('shell.pw.differ'); return; }
    try { await changePassword(d.current, d.next); dlg.close(); toast(t('shell.pw.done')); }
    catch (ex) { err.textContent = ex.message; }
  });
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
}

export function toggleTheme() {
  setTheme(prefs.get('theme') === 'dark' ? 'light' : 'dark');
}

// The appearance choices drawn into a menu element; `back` (phone: inside the account menu)
// adds a first entry that returns to what the menu showed before.
function drawAppearance(menu, back) {
  const redraw = () => drawAppearance(menu, back);
  const mode = prefs.get('theme');
  const current = prefs.get('palette') || 'film';
  menu.replaceChildren(
    back ? h('button', { class: 'menu-item appearance-view', type: 'button', onclick: back }, icon('back', 16, 1.8), t('shell.back')) : null,
    h('div', { class: 'seg appearance-mode appearance-view', role: 'group', 'aria-label': t('shell.appearance') },
      ['dark', 'light'].map((m) => h('button', { type: 'button', 'aria-pressed': String(m === mode),
        onclick: () => { setTheme(m); redraw(); } }, icon(m === 'dark' ? 'moon' : 'sun', 13, 2), ' ', t(`shell.mode.${m}`)))),
    h('div', { class: 'menu-sep', role: 'separator' }),
    h('div', { class: 'menu-label muted small' }, t('shell.palette')),
    ...PALETTES.map(([key, name, bg, accent]) => h('button', { class: 'menu-item appearance-view', type: 'button', role: 'menuitemradio',
      'aria-checked': String(key === current), onclick: () => { setPalette(key); redraw(); } },
      h('span', { class: 'swatch', 'aria-hidden': 'true', style: { background: `linear-gradient(135deg, ${bg} 0 50%, ${accent} 50% 100%)` } }),
      h('span', { class: 'menu-text' }, h('span', {}, name), h('span', { class: 'muted small' }, t(`palette.${key}`))),
      key === current ? h('span', { class: 'menu-end' }, icon('check', 14, 2.2)) : null)));
}

// Appearance (as in Reporion): dark / light, then the palettes with a swatch; a choice
// applies at once and the menu stays open, so another can be tried.
function appearanceMenu() {
  const btn = h('button', { id: 'dx-theme-btn', class: 'icon-btn', type: 'button', 'aria-label': t('shell.appearance'),
    title: t('shell.appearance'), 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, icon('palette', 16, 1.8));
  const menu = h('div', { class: 'menu appearance', role: 'menu', hidden: true });
  const wrap = h('span', { class: 'menu-wrap ctx-wide' }, btn, menu);
  const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  const draw = () => drawAppearance(menu);
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (menu.hidden) draw();
    menu.hidden = !menu.hidden;
    btn.setAttribute('aria-expanded', String(!menu.hidden));
  });
  menu.addEventListener('click', (e) => e.stopPropagation());
  document.addEventListener('click', (e) => { if (!wrap.contains(e.target)) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  return wrap;
}

customElements.define('dx-shell', DtShell);
