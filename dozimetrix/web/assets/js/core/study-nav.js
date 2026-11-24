// Previous / next study from the Studies list the user came from.
//
// The Studies page records its query, sort and the ids of the page it shows (per tab, in
// sessionStorage); a study page finds itself in that list and steps to its neighbours,
// fetching the adjacent page of the same query when it reaches an edge.

import { h } from './dom.js';
import { studies } from './api.js';

const KEY = 'studies.nav';

/** Studies page: remember the list as it is now shown. */
export function rememberList(state, q, rows, total) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({
      q, sort: state.sort, dir: state.dir, page: state.page || 1, size: state.size || 50, total,
      ids: rows.map((r) => r.id).filter((id) => id != null),
    }));
  } catch { /* storage full or off: no navigation, nothing breaks */ }
}

function read() {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; }
}

async function loadPage(nav, page) {
  const res = await studies({ q: nav.q, sort: nav.sort, dir: nav.dir, page, size: nav.size });
  const next = { ...nav, page, total: res.total ?? nav.total, ids: (res.rows || []).map((r) => r.id).filter((id) => id != null) };
  sessionStorage.setItem(KEY, JSON.stringify(next));
  return next;
}

/** The study before (-1) or after (+1) `id` in the remembered list, or null. */
async function neighbour(id, step) {
  let nav = read();
  if (!nav) return null;
  let i = nav.ids.indexOf(id);
  if (i < 0) return null;
  i += step;
  if (i < 0) {
    if (nav.page <= 1) return null;
    nav = await loadPage(nav, nav.page - 1);
    return nav.ids[nav.ids.length - 1] ?? null;
  }
  if (i >= nav.ids.length) {
    if (nav.page * nav.size >= (nav.total ?? 0)) return null;
    nav = await loadPage(nav, nav.page + 1);
    return nav.ids[0] ?? null;
  }
  return nav.ids[i];
}

/** The ‹ › split button for a study page (hidden when the study was not opened from a list). */
export function studyNav(id) {
  const nav = read();
  const i = nav ? nav.ids.indexOf(id) : -1;
  if (i < 0) return null;
  const pos = (nav.page - 1) * nav.size + i + 1;
  const total = nav.total ?? nav.ids.length;
  const go = (step) => async (e) => {
    const btn = e.currentTarget;  // gone after the await
    btn.disabled = true;
    try {
      const to = await neighbour(id, step);
      if (to != null) location.href = `study.html?id=${to}`;
    } finally { btn.disabled = false; }
  };
  return h('span', { class: 'seg nav-split no-print', role: 'group', 'aria-label': `Study ${pos} of ${total} in the list` },
    h('button', { type: 'button', title: `Previous study (${pos} of ${total})`, 'aria-label': 'Previous study', disabled: pos <= 1, onclick: go(-1) }, '‹'),
    h('button', { type: 'button', title: `Next study (${pos} of ${total})`, 'aria-label': 'Next study', disabled: pos >= total, onclick: go(1) }, '›'));
}
