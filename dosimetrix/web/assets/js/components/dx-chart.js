// <dx-chart label="…" height="260"> — host for one Apache ECharts instance.
//
//   chart.draw((theme) => echartsOption)   // re-run automatically on theme change and resize
//   chart.csv = [['header', …], [row…]]   // data behind the chart, for the CSV download
//   chart.downloadPNG(name) / chart.downloadCSV(name)
//
// ECharts is the vendored file assets/vendor/echarts/echarts.min.js, loaded by the page as a
// classic script (window.echarts). Colours come from the CSS tokens, so both themes and print work.

import { h } from '../core/dom.js';

/** Read the current token values once per draw. */
export function chartTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  return {
    text: v('--text'), muted: v('--muted'), line: v('--line'), line2: v('--line-2'), panel: v('--panel'), bg: v('--bg'),
    accent: v('--accent'), ref: v('--ref'), warn: v('--warn'), alert: v('--alert'), seq: v('--seq'), ok: v('--ok'),
    devices: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--d${i}`)),
    mono: v('--font-mono'), sans: v('--font-sans'),
  };
}

/** Axis/tooltip defaults shared by every chart. */
export function baseOption(th) {
  const axis = {
    axisLine: { lineStyle: { color: th.line2 } },
    axisTick: { lineStyle: { color: th.line2 } },
    axisLabel: { color: th.muted, fontFamily: th.mono, fontSize: 11 },
    splitLine: { lineStyle: { color: th.line } },
    nameTextStyle: { color: th.muted, fontFamily: th.sans },
  };
  return {
    animation: false,
    textStyle: { fontFamily: th.sans, color: th.text },
    tooltip: { backgroundColor: th.panel, borderColor: th.line2, textStyle: { color: th.text, fontFamily: th.mono, fontSize: 12 } },
    // one scrolling row: many devices would otherwise wrap over the plot
    legend: { type: 'scroll', textStyle: { color: th.muted }, icon: 'roundRect', itemWidth: 10, itemHeight: 10, top: 0, left: 0, right: 0,
      pageIconColor: th.text, pageIconInactiveColor: th.line2, pageTextStyle: { color: th.muted } },
    grid: { left: 56, right: 24, top: 30, bottom: 36, containLabel: false },
    _axis: axis,
  };
}

class DtChart extends HTMLElement {
  static get observedAttributes() { return ['height']; }

  attributeChangedCallback(name, _old, value) {
    if (name === 'height' && this.box) this.box.style.height = `${value || 260}px`;
  }

  connectedCallback() {
    if (this.box) return;
    this.box = h('div', { class: 'chart-box', role: 'img', 'aria-label': this.getAttribute('label') || 'Chart',
      style: { height: `${this.getAttribute('height') || 260}px` } });
    this.append(this.box);
    this._onTheme = () => this.redraw();
    document.addEventListener('dx-theme-change', this._onTheme);
    // print uses the light tokens from base.css: redraw before and after printing
    this._onPrint = () => { this.redraw(); this.instance?.resize(); };
    window.addEventListener('beforeprint', this._onPrint);
    window.addEventListener('afterprint', this._onPrint);
    this._ro = new ResizeObserver(() => this.instance?.resize());
    this._ro.observe(this.box);
  }

  disconnectedCallback() {
    document.removeEventListener('dx-theme-change', this._onTheme);
    window.removeEventListener('beforeprint', this._onPrint);
    window.removeEventListener('afterprint', this._onPrint);
    this._ro?.disconnect();
    this.instance?.dispose();
    this.instance = null;
  }

  set label(text) { this.setAttribute('label', text); if (this.box) this.box.setAttribute('aria-label', text); }

  draw(build) {
    this._build = build;
    this.redraw();
  }

  redraw() {
    if (!this._build || !this.box) return;
    if (!window.echarts) {
      this.box.replaceChildren(h('p', { class: 'muted chart-missing' }, 'Chart library not loaded (assets/vendor/echarts/echarts.min.js).'));
      return;
    }
    if (!this.instance) this.instance = window.echarts.init(this.box, null, { renderer: 'canvas' });
    const option = this._build(chartTheme());
    delete option._axis;
    this.instance.setOption(option, true);
  }

  on(event, handler) {
    this.instance?.on(event, handler);
  }

  /** The PNG is laid out at least EXPORT_WIDTH CSS px wide, whatever the screen: a phone-width
   *  chart would come out narrow and crowded. Same height as on screen, drawn off-screen at 2x. */
  downloadPNG(name = 'chart') {
    if (!this.instance || !this._build) return;
    const EXPORT_WIDTH = 1200;
    const width = Math.max(this.box.clientWidth, EXPORT_WIDTH);
    const height = this.box.clientHeight;
    const th = chartTheme();
    const host = h('div', { 'aria-hidden': 'true',
      style: { position: 'fixed', left: '-100000px', top: '0', width: `${width}px`, height: `${height}px` } });
    document.body.append(host);
    const off = window.echarts.init(host, null, { renderer: 'canvas', width, height });
    try {
      const option = this._build(th);
      delete option._axis;
      off.setOption(option, true);
      const url = off.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: th.panel });
      h('a', { href: url, download: `${name}.png` }).click();
    } finally {
      off.dispose();
      host.remove();
    }
  }

  downloadCSV(name = 'chart') {
    if (!this.csv) return;
    const esc = (v) => (/[",\n;]/.test(String(v ?? '')) ? `"${String(v).replace(/"/g, '""')}"` : String(v ?? ''));
    const text = this.csv.map((r) => r.map(esc).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob(['﻿', text], { type: 'text/csv;charset=utf-8' }));
    h('a', { href: url, download: `${name}.csv` }).click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

customElements.define('dx-chart', DtChart);
