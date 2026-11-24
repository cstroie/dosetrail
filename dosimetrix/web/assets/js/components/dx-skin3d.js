// <dx-skin3d> — the skin dose map drawn on the model's own surface, in 3D.
//
//   el.map = skinDoseResult   // from GET /api/studies/{id}/skin (theta, z_cm, dose_mgy, phantom_cm)
//
// The surface is the elliptical cylinder the estimate was computed on (skindose.py): angle
// around the body (0° anterior, 90° patient's left) × cranio-caudal position. Drag to turn the
// patient, wheel or +/- to zoom, arrow keys for the keyboard. Plain canvas, no 3D library.
// Colours come from the CSS tokens (same ramp as the 2D map), so both themes and print work.

import { chartTheme } from './dx-chart.js';

const SIDES = ['anterior', 'anterior-left', 'left', 'posterior-left', 'posterior', 'posterior-right', 'right', 'anterior-right'];

function rgb(ctx, color) {
  ctx.fillStyle = '#000';
  ctx.fillStyle = color || '#888';
  const c = ctx.fillStyle;
  if (c.startsWith('#')) return [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const m = c.match(/[\d.]+/g) || [128, 128, 128];
  return m.slice(0, 3).map(Number);
}

class DxSkin3d extends HTMLElement {
  constructor() {
    super();
    this.yaw = -35;    // degrees: 0 shows the anterior surface
    this.pitch = 15;   // degrees: tilt toward the viewer's eye
    this.zoom = 1;
  }

  connectedCallback() {
    if (this.canvas) return;
    this.canvas = document.createElement('canvas');
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute('role', 'img');
    this.canvas.style.cssText = 'width:100%;height:360px;display:block;cursor:grab;touch-action:none';
    this.tip = document.createElement('div');
    this.tip.className = 'muted small mono';
    this.tip.style.cssText = 'min-height:1.4em;padding:2px 4px';
    this.legend = document.createElement('div');
    this.legend.className = 'muted small';
    this.legend.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px';
    this.append(this.canvas, this.tip, this.legend);
    let drag = null;
    this.canvas.addEventListener('pointerdown', (e) => { drag = [e.clientX, e.clientY, this.yaw, this.pitch]; this.canvas.setPointerCapture(e.pointerId); this.canvas.style.cursor = 'grabbing'; });
    this.canvas.addEventListener('pointermove', (e) => {
      if (drag) {
        this.yaw = drag[2] - (e.clientX - drag[0]) * 0.6;  // the surface follows the pointer
        this.pitch = Math.max(-80, Math.min(80, drag[3] + (e.clientY - drag[1]) * 0.4));
        this.draw();
      } else this.hover(e);
    });
    this.canvas.addEventListener('pointerup', () => { drag = null; this.canvas.style.cursor = 'grab'; });
    this.canvas.addEventListener('wheel', (e) => { e.preventDefault(); this.zoom = Math.max(0.5, Math.min(3, this.zoom * (e.deltaY < 0 ? 1.1 : 0.9))); this.draw(); }, { passive: false });
    this.canvas.addEventListener('keydown', (e) => {
      const k = { ArrowLeft: [10, 0], ArrowRight: [-10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] }[e.key];
      if (k) { e.preventDefault(); this.yaw += k[0]; this.pitch = Math.max(-80, Math.min(80, this.pitch + k[1])); this.draw(); }
      if (e.key === '+' || e.key === '-') { this.zoom = Math.max(0.5, Math.min(3, this.zoom * (e.key === '+' ? 1.1 : 0.9))); this.draw(); }
    });
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(this.canvas);
    this.themeObs = new MutationObserver(() => this.draw());
    this.themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'data-palette'] });
  }

  disconnectedCallback() { this.ro?.disconnect(); this.themeObs?.disconnect(); }

  set map(r) {
    this._map = r;
    this.max = Math.max(1, ...r.dose_mgy.flat());
    this.canvas?.setAttribute('aria-label', `Skin dose map in 3D, peak ${Math.round(r.psd_mgy)} mGy`);
    this.view(r.psd_at ? r.psd_at.theta : 180);
  }

  /** Turn the patient so that the surface at angle theta faces the viewer. */
  view(theta) { this.yaw = -theta; this.draw(); }

  colour(v, th) {
    // panel → seq → warn → alert, as the 2D map's visual map
    const stops = this._stops;
    if (v <= 0) return stops.none;
    const t = Math.min(1, v / this.max) * (stops.ramp.length - 1);
    const i = Math.min(stops.ramp.length - 2, Math.floor(t));
    const f = t - i;
    return stops.ramp[i].map((c, k) => c + (stops.ramp[i + 1][k] - c) * f);
  }

  project(x, y, z) {
    // body frame: x patient's left, y anterior, z toward the head; screen: z up
    const cy = Math.cos(this.yaw * Math.PI / 180), sy = Math.sin(this.yaw * Math.PI / 180);
    const cp = Math.cos(this.pitch * Math.PI / 180), sp = Math.sin(this.pitch * Math.PI / 180);
    const X = x * cy - y * sy;          // screen right
    const D = x * sy + y * cy;          // toward the viewer
    const Y = z * cp - D * sp;          // screen up
    const depth = D * cp + z * sp;
    return [this.cx + X * this.scale, this.cy - Y * this.scale, depth];
  }

  draw() {
    const r = this._map;
    const cv = this.canvas;
    if (!r || !cv || !cv.clientWidth) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = cv.clientWidth * dpr;
    cv.height = cv.clientHeight * dpr;
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = cv.clientWidth, H = cv.clientHeight;
    const th = chartTheme();
    this._stops = { none: rgb(ctx, th.line2 || '#444'), ramp: [th.seq || '#8a7fd0', th.warn || '#f0b03c', th.alert || '#f05a4a'].map((c) => rgb(ctx, c)) };
    ctx.clearRect(0, 0, W, H);
    const a = r.phantom_cm.lateral, b = r.phantom_cm.ap;
    const zs = r.z_cm, thetas = r.theta;
    const zr = (zs[zs.length - 1] - zs[0]) / 2 || 1;
    this.scale = Math.min(W / (2.6 * Math.max(a, b)), H / (2.3 * zr + 2 * b * 0.4)) * this.zoom;
    this.cx = W / 2; this.cy = H / 2;
    const step = (thetas[1] - thetas[0]) || 3;
    const dz = (zs[1] - zs[0]) || 1;
    const zmid = (zs[0] + zs[zs.length - 1]) / 2;
    const pt = (deg, z) => { const t = deg * Math.PI / 180; return [a * Math.sin(t), b * Math.cos(t), z - zmid]; };
    const quads = [];
    const light = [0.35, 0.6, 0.7];
    const cy = Math.cos(this.yaw * Math.PI / 180), sy = Math.sin(this.yaw * Math.PI / 180);
    const cp = Math.cos(this.pitch * Math.PI / 180);
    thetas.forEach((deg, ti) => {
      const t = deg * Math.PI / 180;
      // outward normal of the ellipse at this angle, then its component toward the viewer
      const nx = Math.sin(t) / a, ny = Math.cos(t) / b, nl = Math.hypot(nx, ny);
      const facing = ((nx * sy + ny * cy) / nl) * cp;
      if (facing <= -0.05) return;
      const shade = 0.55 + 0.45 * Math.max(0, (nx * light[0] + ny * light[1]) / nl * 0.5 + facing * 0.5);
      zs.forEach((z, zi) => {
        const corners = [pt(deg - step / 2, z - dz / 2), pt(deg + step / 2, z - dz / 2), pt(deg + step / 2, z + dz / 2), pt(deg - step / 2, z + dz / 2)]
          .map((p) => this.project(...p));
        quads.push({ corners, depth: corners.reduce((s, c) => s + c[2], 0) / 4, v: r.dose_mgy[zi][ti], shade, ti, zi });
      });
    });
    quads.sort((p, q) => p.depth - q.depth);
    for (const q of quads) {
      const c = this.colour(q.v, th).map((x) => Math.round(x * q.shade));
      ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      ctx.beginPath();
      q.corners.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = ctx.fillStyle;  // hide the seams between cells
      ctx.lineWidth = 0.6;
      ctx.stroke();
    }
    this.quads = quads;
    // orientation: head arrow and side labels on the rim
    ctx.font = `11px ${th.mono || 'monospace'}`;
    ctx.fillStyle = th.muted;
    const top = this.project(0, 0, zr + 3);
    ctx.textAlign = 'center';
    ctx.fillText('▲ head', top[0], top[1] - 4);
    for (const [deg, label] of [[0, 'A'], [90, 'L'], [180, 'P'], [270, 'R']]) {
      const [x, y, z] = pt(deg, zs[0]);
      const p = this.project(x * 1.25, y * 1.25, z - 2);
      const t = deg * Math.PI / 180;
      if ((Math.sin(t) / a * sy + Math.cos(t) / b * cy) / Math.hypot(Math.sin(t) / a, Math.cos(t) / b) > -0.3) ctx.fillText(label, p[0], p[1] + 12);
    }
    // the peak, marked
    if (r.psd_at) {
      const [x, y, z] = pt(r.psd_at.theta, r.psd_at.z_cm);
      const p = this.project(x * 1.02, y * 1.02, z);
      const t = r.psd_at.theta * Math.PI / 180;
      if ((Math.sin(t) / a * sy + Math.cos(t) / b * cy) > 0) {
        ctx.strokeStyle = th.text; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(p[0], p[1], 7, 0, 2 * Math.PI); ctx.stroke();
      }
    }
    this.legend.replaceChildren(
      Object.assign(document.createElement('span'), { textContent: '0' }),
      Object.assign(document.createElement('span'), { style: `display:inline-block;width:160px;height:10px;background:linear-gradient(90deg,${th.seq},${th.warn},${th.alert})` }),
      Object.assign(document.createElement('span'), { textContent: `${Math.round(this.max)} mGy` }),
      Object.assign(document.createElement('span'), { textContent: '· drag to turn, wheel to zoom, ○ peak' }));
  }

  hover(e) {
    if (!this.quads) return;
    const rect = this.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    for (let i = this.quads.length - 1; i >= 0; i--) {
      const q = this.quads[i];
      if (inside(x, y, q.corners)) {
        const r = this._map;
        const deg = r.theta[q.ti];
        this.tip.textContent = `${deg}° ${SIDES[Math.round(deg / 45) % 8]} · ${r.z_cm[q.zi]} cm · ${Math.round(q.v)} mGy`;
        return;
      }
    }
    this.tip.textContent = '';
  }
}

function inside(x, y, poly) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

customElements.define('dx-skin3d', DxSkin3d);
