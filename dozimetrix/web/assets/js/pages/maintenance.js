// Maintenance (admin): start the jobs that keep the derived data right, and watch them. A job is a
// `dozimetrix` command (maintain, classify, recompute) that the server starts as its own process;
// only one runs at a time (the same lock the nightly run and the command line use), and its
// progress arrives over the live stream. Below, the tasks of the last periodic run.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, section, toast } from '../core/dom.js';
import { confirmDialog } from '../core/confirm.js';
import { maintenanceInfo, startMaintenance, dropCache } from '../core/api.js';
import { live } from '../core/live.js';

const page = document.getElementById('page');
const host = h('div', { style: { display: 'grid', gap: 'var(--sp-4)' } });
let info = null;     // the last /api/maintenance
let state = null;    // the running or last job (live)
let seenRunning = false;

const when = (iso) => (iso ? iso.replace('T', ' ').slice(0, 19) : '—');
const secs = (s) => (s == null ? '' : s >= 90 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);
const running = () => state?.state === 'running';
const tag = (st) => h('span', { class: `tag ${st === 'done' ? 'ok' : st === 'running' ? 'info' : 'alert'}` },
  { done: 'done', running: 'running', failed: 'failed', interrupted: 'interrupted' }[st] || st);

function elapsed(st) {
  const a = Date.parse(st.started);
  const b = st.finished ? Date.parse(st.finished) : Date.now();
  return Number.isFinite(a) && Number.isFinite(b) ? secs(Math.max(0, (b - a) / 1000)) : '';
}

function current() {
  if (!state) return h('p', { class: 'muted' }, 'No job has been run from here or from the command line yet.');
  const job = info?.jobs.find((j) => j.name === state.job);
  const days = state.options?.days;
  const log = (state.lines || []).join('\n') || (state.state === 'running' ? 'starting…' : '');
  return h('div', { class: 'panel', style: { padding: 'var(--sp-3)' } },
    h('div', { class: 'row', style: { gap: 'var(--sp-2)', 'align-items': 'baseline' } },
      h('strong', {}, job?.label || state.job), tag(state.state),
      h('span', { class: 'muted small' },
        `${days ? `last ${days} days` : 'every study'} · started ${when(state.started)} by ${state.by || '—'} · ${elapsed(state)}`)),
    state.error ? h('p', { class: 'error' }, state.error) : null,
    state.state === 'interrupted' ? h('p', { class: 'muted small' }, 'The process stopped before it finished (the server was restarted or the job was killed). Nothing is half-done: start it again.') : null,
    log ? h('pre', { class: 'mono small', style: { 'max-height': '16em', overflow: 'auto', margin: 'var(--sp-2) 0 0' } }, log) : null);
}

function cards() {
  const grid = h('div', { style: { display: 'grid', gap: 'var(--sp-3)', 'grid-template-columns': 'repeat(auto-fit, minmax(340px, 1fr))' } });
  for (const j of info.jobs) {
    const days = j.days
      ? h('input', { class: 'input num-input', type: 'number', min: 1, max: 3650, step: 1, value: j.days.default, 'aria-label': 'Last N days' })
      : null;
    const every = j.days ? h('label', { class: 'muted small', style: { display: 'inline-flex', gap: '6px', 'align-items': 'center' } },
      h('input', { type: 'checkbox' }), j.days.every) : null;
    const go = h('button', { class: 'btn primary', type: 'button', disabled: running() }, 'Run');
    if (every) every.querySelector('input').addEventListener('change', (e) => { days.disabled = e.target.checked; });
    go.addEventListener('click', async () => {
      const all = every ? every.querySelector('input').checked : !j.days;
      const n = j.days && !all ? Number(days.value) : null;
      if ((all || !j.days) && !(await confirmDialog(`${j.label}: ${j.days ? 'every study' : 'all the data'}. It can take several minutes and runs in the background. Start it?`, { label: 'Start' }))) return;
      go.disabled = true;
      try { state = await startMaintenance(j.name, n); toast(`${j.label} started`); draw(); } catch (e) { toast(e.message); go.disabled = false; }
    });
    grid.append(h('div', { class: 'panel form-card' },
      h('h3', { style: { margin: 0 } }, j.label),
      h('p', { class: 'muted small' }, j.description),
      h('div', { class: 'row', style: { gap: 'var(--sp-2)', 'align-items': 'center' } },
        days ? [h('span', { class: 'muted small' }, 'Studies of the last'), days, h('span', { class: 'muted small' }, 'days'), every] : null, go)));
  }
  return grid;
}

function lastRun() {
  const r = info.last_run;
  if (!r) return h('p', { class: 'muted' }, 'The periodic maintenance has not run yet.');
  const body = h('tbody');
  for (const [name, t] of Object.entries(r.tasks || {})) {
    const detail = typeof t.detail === 'string' ? t.detail : Object.entries(t.detail || {}).map(([k, v]) => `${k}: ${v}`).join(' · ');
    body.append(h('tr', {}, h('td', {}, name), h('td', {}, h('span', { class: `tag ${t.ok ? 'ok' : 'alert'}` }, t.ok ? 'ok' : 'failed')),
      h('td', { class: 'num' }, secs(t.seconds)), h('td', { class: 'mono small' }, detail.slice(0, 220))));
  }
  return h('div', { class: 'panel', style: { padding: 'var(--sp-3)' } },
    h('p', { class: 'muted small' }, `${when(r.at)} · ${r.mode}${r.rules_changed ? ' (the rules had changed)' : ''} · ${r.ok ? 'ok' : 'failed'} in ${secs(r.seconds)}`),
    h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Task'), h('th', {}, ''), h('th', { class: 'num' }, 'Time'), h('th', {}, 'Result'))), body)));
}

function draw() {
  if (!info) return;
  clear(host).append(
    section(running() ? 'Running' : 'Last job', { sub: 'Only one job runs at a time; the nightly run and the command line use the same lock.' }, current()),
    section('Start a job', { sub: info.rules_changed ? 'The classification rules changed since the last full pass: the next periodic run covers every study by itself.' : 'A job runs in the background: you can leave this page.' }, cards()),
    section('Last periodic run', { sub: 'The tasks of the last "Periodic maintenance", whoever started it.' }, lastRun()));
}

async function load() {
  dropCache();
  try { info = await maintenanceInfo(); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  state = info.current;
  seenRunning = running();
  draw();
}

clear(page).append(adminTabs('maintenance.html'), host);
load().then(() => live(['maintenance'], { maintenance: (v) => {
  const next = v?.current ?? null;
  const was = seenRunning;
  state = next;
  seenRunning = running();
  if (was && !seenRunning) { load(); return; }  // finished: the last-run table moved too
  draw();
} }));
setInterval(() => { if (running()) draw(); }, 5000);  // the elapsed time
