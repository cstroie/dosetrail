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

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

/** Inline SVG icon from a small hand-drawn set (stroke icons, 24×24 grid). */
const ICONS = {
  logo: 'M3 18c3 0 4-9 7-9s3 6 6 6 3-8 5-10',
  overview: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  studies: 'M4 6h16M4 12h16M4 18h16',
  charts: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  alerts: 'M12 3 2 20h20zM12 10v4M12 17v.5',
  admin: 'M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2',
  search: 'M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14M20 20l-3.5-3.5',
  sun: 'M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8M12 2v2M12 20v2M2 12h2M20 12h2',
  moon: 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z',
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
  let host = document.getElementById('dt-toast');
  if (!host) {
    host = h('div', { id: 'dt-toast', class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(host);
  }
  host.textContent = message;
  host.classList.add('show');
  clearTimeout(host._t);
  host._t = setTimeout(() => host.classList.remove('show'), 2600);
}
