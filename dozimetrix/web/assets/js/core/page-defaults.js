// The filters a page opens with: a link's own (?q=… in the URL, for that visit), then the
// user's own last-used ones, then the default an admin set for everyone (/api/page-defaults),
// then the page's built-in one. Only what the user changed is kept as theirs, filter by
// filter (`keepOwn`): a later default from an admin still reaches every other filter.

import { h, toast } from './dom.js';
import { pageDefaults, putPageDefaults, me, isSample } from './api.js';
import { prefs } from './prefs.js';

let loaded;

/** → { value: the admin's default for this page ({} if none), admin: whether this user is one } */
export async function adminDefaults(page) {
  loaded ??= Promise.all([pageDefaults(), me().catch(() => null)]);
  const [all, user] = await loaded;
  return { value: all?.[page] || {}, admin: !!user?.roles?.includes('admin') };
}

/** Keep a filter as the user's own only when it differs from the page's default for it. */
export function keepOwn(prefKey, value, dflt) {
  prefs.set(prefKey, value === dflt ? undefined : value);
}

/**
 * Buttons for a page's toolbar: "Reset filters" forgets the user's own (prefKeys) and reloads;
 * admins also get "Make default for everyone", which stores current() as the page default.
 */
export function defaultsActions(page, { admin, prefKeys, current }) {
  const reset = h('button', { class: 'btn ghost', type: 'button', title: 'Back to the filters this page opens with for everyone',
    onclick: () => { for (const k of prefKeys) prefs.set(k, undefined); location.replace(location.pathname); } }, 'Reset filters');
  // say when the user's own filters are in use: a default set later by an admin does not replace them
  const own = h('span', { class: 'tag', title: 'Some filters are your own, not the page default for everyone: Reset filters goes back to it' }, 'your filters');
  const show = () => { own.hidden = !prefKeys.some((k) => prefs.get(k) !== undefined); };
  show();
  prefs.subscribe((name) => { if (prefKeys.includes(name)) show(); });
  if (isSample || !admin) return [own, reset];
  const make = h('button', { class: 'btn ghost', type: 'button', title: 'Users without filters of their own open this page with these',
    onclick: async () => {
      try { await putPageDefaults(page, current()); toast('Saved as the default for everyone'); } catch (e) { toast(`Not saved: ${e.message}`); }
    } }, 'Make default for everyone');
  return [own, reset, make];
}
