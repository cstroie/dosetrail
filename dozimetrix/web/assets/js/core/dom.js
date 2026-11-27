// Tiny DOM helpers. Text is always set through textContent, never innerHTML,
// so data coming from DICOM headers cannot inject markup.

const SVG_NS = 'http://www.w3.org/2000/svg';
const SVG_TAGS = new Set(['svg', 'path', 'circle', 'rect', 'polyline', 'line', 'g']);

/**
 * h('button', {class: 'btn', onclick: fn, 'aria-label': 'x'}, 'text', childNode, [more])
 * Attributes with value false/null/undefined are skipped; `on*` functions become listeners;
 * `dataset` and `style` accept objects.
 */
export function h(tag, attrs = {}, ...children) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') {
      for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    } else if (k === 'class') el.setAttribute('class', v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
/** HTML-escape a value for the few places that build markup strings (ECharts tooltips). */
export function esc(v) { return String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]); }

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/**
 * The one way to put a heading on a page (docs/design-rules.md, "Sections and headings"):
 *   section(title, { sub, actions, level, id, cls }, ...body)
 *     <section class="block">
 *       <hgroup><h2>title</h2><p>sub</p></hgroup>        (inside a <header> beside .actions when
 *       …the main element: table, form, panel, div…       there are actions; level 1 for a page title)
 *   panel(title, { sub, actions, id }, ...body)         the same inside a card: section.panel > header.panel-head
 * `title` may be text, a node or an array; `sub` and `actions` are optional.
 */
function headGroup(title, sub, level, id) {
  return h('hgroup', {}, h(`h${level}`, { id }, title), sub ? h('p', {}, sub) : null);
}

/** The heading of a card: header.panel-head > hgroup, then what follows (tags, a spacer, buttons). */
export function panelHead(title, { id = null, level = 2 } = {}, ...rest) {
  return h('header', { class: 'panel-head' }, headGroup(title, null, level, id), ...rest);
}

/** The title row of a page: header > hgroup (h1 + p), then its tags and buttons. */
export function pageHead(title, { sub = null, id = null, style = null } = {}, ...rest) {
  return h('header', { class: 'row', style }, headGroup(title, sub, 1, id), ...rest);
}

export function section(title, { sub = null, actions = null, level = 2, id = null, cls = '' } = {}, ...body) {
  const head = headGroup(title, sub, level, id);
  return h('section', { class: `block ${cls}`.trim(), 'aria-labelledby': id },
    actions ? h('header', {}, head, h('div', { class: 'actions' }, actions)) : head, ...body);
}

export function panel(title, { sub = null, actions = null, id = null, cls = '' } = {}, ...body) {
  return h('section', { class: `panel ${cls}`.trim(), 'aria-labelledby': id },
    h('header', { class: 'panel-head' }, headGroup(title, sub, 2, id), h('span', { class: 'spacer' }), actions), ...body);
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

/** Inline SVG icon from a small hand-drawn set (stroke icons, 24×24 grid). */
const ICONS = {
  logo: 'M5 4h5.5a8 8 0 0 1 0 16H5z M8.5 16.5C11 16.5 11.5 10 15 9.5',  // the D-curve mark (docs/branding)
  overview: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  studies: 'M4 6h16M4 12h16M4 18h16',
  charts: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  drl: 'M3 12h18M6 20V12M11 20v-5M16 20V8M21 4l-5 4',
  analysis: 'M3 17l6-6 4 4 8-8M15 7h6v6',
  reports: 'M6 2h9l5 5v15H6zM14 2v6h6M9 13h8M9 17h8',
  quality: 'M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6zM8.5 12l2.5 2.5L15.5 9.5',
  alerts: 'M12 3 2 20h20zM12 10v4M12 17v.5',
  admin: 'M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2',
  search: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14M20 20l-3.5-3.5',
  sun: 'M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8M12 2v2M12 20v2M2 12h2M20 12h2',
  moon: 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z',
  palette: 'M12 3a9 9 0 1 0 0 18c1.2 0 1.8-.8 1.8-1.7 0-1.3-1-1.6-1-2.6 0-.9.7-1.4 1.6-1.4H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3zM7.5 12.5v.01M9.5 8v.01M14.5 8v.01M17 11.5v.01',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  user: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 7.5a3 3 0 1 0 0 6a3 3 0 1 0 0-6M6.5 18.2c1.2-2 3.2-3 5.5-3s4.3 1 5.5 3',
  key: 'M14.5 4a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11M10.6 13.4L3 21M5.5 18.5l2 2M8 16l2 2M14.5 8v.01',
  sliders: 'M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4',
  logout: 'M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 8l-4 4 4 4M6 12h10',
  globe: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M3 12h18M12 3c2.5 2.5 3.5 5.5 3.5 9s-1 6.5-3.5 9c-2.5-2.5-3.5-5.5-3.5-9s1-6.5 3.5-9',
  back: 'M15 5l-7 7 7 7',
  plus: 'M12 5v14M5 12h14',
  close: 'M6 6l12 12M18 6 6 18',
  grip: 'M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01',
};

export function icon(name, size = 18, strokeWidth = 1.8) {
  return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    'stroke-width': strokeWidth, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
  h('path', { d: ICONS[name] || '' }));
}

/** Small transient status message (role=status, announced by screen readers). */
export function toast(message) {
  let host = document.getElementById('dx-toast');
  if (!host) {
    host = h('div', { id: 'dx-toast', class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(host);
  }
  host.textContent = message;
  host.classList.add('show');
  clearTimeout(host._t);
  host._t = setTimeout(() => host.classList.remove('show'), 2600);
}
