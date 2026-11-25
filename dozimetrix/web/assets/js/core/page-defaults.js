// The filters a page opens with: the user's own last-used ones (this browser) come first, then
// the default an admin set for everyone (/api/page-defaults), then the page's built-in one.

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

/**
 * Buttons for a page's toolbar: "Reset filters" forgets the user's own (prefKeys) and reloads;
 * admins also get "Make default for everyone", which stores current() as the page default.
 */
export function defaultsActions(page, { admin, prefKeys, current }) {
  const reset = h('button', { class: 'btn ghost', type: 'button', title: 'Back to the filters this page opens with for everyone',
    onclick: () => { for (const k of prefKeys) prefs.set(k, undefined); location.replace(location.pathname); } }, 'Reset filters');
  if (isSample || !admin) return [reset];
  const make = h('button', { class: 'btn ghost', type: 'button', title: 'Users without filters of their own open this page with these',
    onclick: async () => {
      try { await putPageDefaults(page, current()); toast('Saved as the default for everyone'); } catch (e) { toast(`Not saved: ${e.message}`); }
    } }, 'Make default for everyone');
  return [reset, make];
}
