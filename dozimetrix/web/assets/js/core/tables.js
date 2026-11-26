// Small table helpers for the report pages. Cells are built with h(): values are text, never markup.
import { h } from './dom.js';
import { num } from './format.js';

/** cols: [{ key|get, label, right?, fmt?, cls? }]; rows: objects. Empty rows → a line saying so. */
export function dataTable(cols, rows, empty = 'Nothing to show.') {
  const cell = (c, r) => {
    const v = c.get ? c.get(r) : r[c.key];
    if (v instanceof Node) return v;
    if (v === null || v === undefined || v === '') return h('span', { class: 'muted' }, '—');
    return c.fmt ? c.fmt(v, r) : (typeof v === 'number' ? num(v, Number.isInteger(v) ? 0 : 2) : String(v));
  };
  const body = h('tbody', {}, rows.map((r) => h('tr', { class: r._cls || '' },
    cols.map((c) => h('td', { class: c.right ? 'r' : '' }, cell(c, r))))));
  if (!rows.length) body.append(h('tr', {}, h('td', { colspan: cols.length, class: 'empty' }, empty)));
  return h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
    h('thead', {}, h('tr', {}, cols.map((c) => h('th', { class: c.right ? 'r' : '' }, c.label)))), body)));
}

/** A row of figures: [[code, value, note]]. */
export function kpiRow(items) {
  return h('div', { class: 'kpis drl-kpis' }, items.map(([code, v, m]) => h('div', { class: 'kpi' },
    h('span', { class: 'code' }, code), h('span', { class: 'v' }, typeof v === 'number' ? num(v) : String(v ?? '—')),
    h('span', { class: 'm' }, m || ''))));
}

export const tag = (text, kind = '') => h('span', { class: `tag ${kind}`.trim() }, text);

/** Date-time text from an ISO string: 2026-11-26 09:30 */
export const when = (iso) => (iso ? iso.replace('T', ' ').slice(0, 16) : '—');

/** A labelled form field. */
export const field = (label, input, hint) => h('label', { class: 'field' }, label, input,
  hint ? h('span', { class: 'muted small' }, hint) : null);
