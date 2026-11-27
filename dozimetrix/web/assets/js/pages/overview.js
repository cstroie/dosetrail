// Overview: studies per modality, alert queue, device feed health, exam lookup, CT series.

import '../components/dx-shell.js';
import { h, append, clear, toast, panelHead, pageHead } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, ago, dateTime } from '../core/format.js';
import { live } from '../core/live.js';
import { overview, lookup, studyHref, studyLabel, ackAlert, deadlineDone, isSample, dropCache, studies } from '../core/api.js';

// the API's groups are CT, DX, RF, XA, MG, NM, PT; anything else takes the accent
const MOD_COLOR = { CT: '--m-ct', DX: '--m-dx', CR: '--m-dx', RF: '--m-rf', XA: '--m-xa', MG: '--m-mg', NM: '--m-nm', PT: '--m-pt' };
const modColor = (code) => MOD_COLOR[code] || '--accent';
const page = document.getElementById('page');

function sparkline(values, colorVar) {
  const w = 120;
  const hgt = 44;
  const min = Math.min(...values) * 0.9;
  const max = Math.max(...values);
  const bw = w / values.length;
  return h('svg', { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, 'aria-hidden': 'true' },
    values.map((v, i) => h('rect', { x: (i * bw + 1).toFixed(1), width: (bw - 2).toFixed(1),
      y: (hgt - ((v - min) / (max - min || 1)) * hgt).toFixed(1), height: (((v - min) / (max - min || 1)) * hgt).toFixed(1),
      fill: `var(${colorVar})`, opacity: i === values.length - 1 ? 1 : 0.55 })));
}

function kpis(mods) {
  return h('section', { class: 'kpis', 'aria-label': 'Studies per modality' },
    mods.map((m) => h('a', { class: 'kpi', href: `studies.html?mod=${encodeURIComponent(m.code)}&q=${encodeURIComponent('date:90d origin:local -phantom')}`, title: `${m.label}: ${m.weekly.join(', ')}` },
      h('span', { class: 'code', style: { color: `var(${modColor(m.code)})` } }, m.label),
      h('span', { class: 'v' }, num(m.week)),
      h('span', { class: 'm' }, `${t('overview.thisweek')} · ${m.delta_pct > 0 ? '+' : ''}${m.delta_pct}%`),
      h('span', { class: 'm mono' }, `${num(m.month)} ${t('overview.thismonth')}`),
      sparkline(m.weekly, modColor(m.code)))));
}

/** Alerts from the API carry study_id (their own id is the alert's); sample alerts do not. */
function alertHref(a) {
  return studyHref(a.study_id != null ? { id: a.study_id } : { accession: a.accession });
}

const QUEUE_MAX = 10;  // the overview shows the most recent open alerts; the full list is on alerts.html

function alertQueue(allAlerts, now, total) {
  const alerts = allAlerts.slice(0, QUEUE_MAX);
  const list = h('ul', { class: 'queue' });
  for (const a of alerts) {
    const li = h('li', {},
      h('span', { class: `tag ${a.level}` }, a.kind),
      h('span', {}, h('a', { class: 't', href: alertHref(a) }, a.title), h('br'),
        h('span', { class: 'm mono' }, `${a.accession || (a.study_id != null ? `study ${a.study_id}` : '—')} · ${dateTime(a.time)} · ${ago(a.time, now)} ago`)),
      h('span', { class: 'v' }, a.value, h('br'), h('span', { class: 'm' }, a.limit)),
      h('span'),
      h('span', { class: 'acts' },
        h('a', { class: 'btn', href: alertHref(a) }, t('overview.open')),
        h('button', { class: 'btn', type: 'button', onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          if (isSample) {
            li.classList.add('acked');
            toast(`Acknowledged: ${a.title} (sample mode, not saved)`);
            return;
          }
          try {
            await ackAlert(a.id, null);
            li.classList.add('acked');
            toast(`Acknowledged: ${a.title}`);
          } catch (err) {
            btn.disabled = false;
            toast(err.message);
          }
        } }, t('overview.ack'))));
    list.append(li);
  }
  const open = total ?? allAlerts.length;  // the API lists the newest 10 and sends the total
  return h('section', { class: 'panel', id: 'alerts', 'aria-labelledby': 'h-q' },
    panelHead(t('overview.alerts'), { id: 'h-q' }, h('span', { class: 'tag alert' }, `${open} open`),
      open > alerts.length ? h('a', { class: 'btn xs ghost', href: 'alerts.html' }, t('overview.allalerts').replace('{n}', open)) : null),
    list);
}

function feedHealth(feeds, now) {
  const today = (now || '').slice(0, 10);
  const days = feeds[0]?.days.map((d) => {
    const wd = new Date(`${d.date}T12:00`).toLocaleDateString(document.documentElement.lang, { weekday: 'short' }).slice(0, 2);
    return d.date === today ? `${wd} ${d.date.slice(8)}*` : `${wd} ${d.date.slice(8)}`;
  }) || [];
  const stale = feeds.filter((f) => f.status !== 'ok').length;
  const grid = h('div', { class: 'feed', role: 'table', 'aria-label': 'Studies received per device, last 7 days' },
    h('span', { class: 'hd', role: 'columnheader', style: { 'text-align': 'left' } }, t('overview.device')),
    days.map((d) => h('span', { class: 'hd', role: 'columnheader' }, d)),
    h('span', { class: 'hd r', role: 'columnheader' }, t('overview.last')));
  for (const f of feeds) {
    const max = Math.max(1, ...f.days.map((d) => d.n));
    append(grid, [h('span', { role: 'rowheader' }, f.name),
      f.days.map((d) => h('i', { class: `c ${d.n ? '' : 'zero'}`, role: 'cell', title: `${d.date}: ${d.n} studies${d.date === today ? ' so far today' : ''}`,
        style: { opacity: d.n ? (0.45 + (d.n / max) * 0.55).toFixed(2) : '1' } })),
      h('span', { class: 'last', role: 'cell', style: { color: f.status === 'ok' ? 'var(--text)' : 'var(--muted)' }, title: f.status === 'ok' ? undefined : t('overview.stale') },
        !f.last_study ? '—' : f.status === 'ok' ? dateTime(f.last_study).slice(11) : ago(f.last_study, now))]);
  }
  return h('section', { class: 'panel', 'aria-labelledby': 'h-f' },
    panelHead(t('overview.feeds'), { id: 'h-f' }, stale ? h('span', { class: 'tag warn' }, `${stale} ${t('overview.stale')}`) : null),
    grid, h('p', { class: 'muted', style: { 'font-size': '11px', padding: '0 12px 10px' } }, `${t('overview.feed.hint')} * today, still filling.`));
}

// background services (live): redrawn in place by the `services` topic
const servicesBody = h('div', { class: 'services' }, h('p', { class: 'muted', style: { padding: '0 12px 10px' } }, t('state.loading')));
const SVC_TAG = { ok: ['ok', 'running'], warn: ['warn', 'check'], alert: ['alert', 'down'], idle: ['', 'idle'] };

function sinceText(at) {
  if (!at) return '';
  const s = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 1000));
  return s < 90 ? `${s} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
}

let lastServices = null;
function drawServices(list) {
  lastServices = list;
  // two columns: the service with its detail under it (wraps), then state and age stacked
  servicesBody.replaceChildren(h('table', { class: 'grid svc-table' }, h('tbody', {}, list.map((x) => {
    const [cls, label] = SVC_TAG[x.state] || ['', x.state];
    return h('tr', {},
      h('td', {}, x.name, x.detail ? h('div', { class: 'muted small', style: { 'overflow-wrap': 'anywhere' } }, x.detail) : null),
      h('td', { style: { 'text-align': 'right', 'white-space': 'nowrap', width: '1%' } },
        h('span', { class: `tag ${cls}` }, label),
        x.at ? h('div', { class: 'muted small mono', title: `last heartbeat ${x.at.replace('T', ' ')}` }, sinceText(x.at)) : null));
  }))));
}
setInterval(() => lastServices && drawServices(lastServices), 15000);  // tick the ages

function servicesPanel() {
  return h('section', { class: 'panel', 'aria-labelledby': 'h-svc' },
    panelHead(t('overview.services'), { id: 'h-svc' },
      h('a', { class: 'muted small', href: 'integration.html' }, 'Integration →')),
    servicesBody);
}

function lookupPanel() {
  const results = h('div', { class: 'lookup-results', style: { display: 'flex', 'flex-direction': 'column', gap: '6px' }, 'aria-live': 'polite' });
  let seq = 0;
  const input = h('input', { id: 'lk', class: 'input mono', type: 'search', autocomplete: 'off', placeholder: 'A2026…',
    oninput: async () => {
      const mine = ++seq;
      const found = await lookup(input.value);
      if (mine !== seq) return;
      clear(results);
      for (const s of found) {
        results.append(h('a', { class: 'res', href: s.id != null || s.modality === 'XA' ? studyHref(s) : `studies.html?q=${encodeURIComponent('acc:' + s.accession)}` },
          h('span', { class: 'tag info' }, s.modality), h('span', { class: 'mono' }, studyLabel(s)), h('span', { class: 'mono' }, s.dose),
          h('span', { class: 'd' }, `${s.description} · ${s.device_name} · ${dateTime(s.date)}`)));
      }
      if (input.value.trim().length >= 3 && !found.length) results.append(h('span', { class: 'muted' }, t('state.empty')));
    } });
  return h('section', { class: 'panel lookup-panel', 'aria-labelledby': 'h-l' },
    panelHead(t('overview.lookup'), { id: 'h-l' }),
    h('div', { class: 'lookup' }, h('label', { class: 'muted small', for: 'lk' }, t('overview.lookup.label')), input, results,
      h('span', { class: 'muted small' }, t('overview.lookup.hint'))));
}

/** CT series (events) panel: shows recent CT acquisitions with DLP and CTDIvol. */
async function eventsPanel() {
  // Fetch recent CT studies (last 7 days by default)
  const data = await studies({ q: 'modality:CT origin:local -phantom date:7d', sort: 'date', dir: 'desc', size: 20 });
  const rows = data.rows || [];
  if (!rows.length) return null;

  const header = h('div', { class: 'evb-header', role: 'row' },
    h('span', { class: 'muted', style: { 'font-size': '11px' } }, t('overview.event')),
    h('span', { class: 'muted', style: { 'font-size': '11px' } }, t('overview.dlp')),
    h('span', { class: 'muted', style: { 'font-size': '11px', 'text-align': 'right' } }, t('overview.ctdivol')));

  const list = h('div', { class: 'evb-list', role: 'table', 'aria-label': t('overview.events') });
  list.append(header);

  for (const s of rows) {
    const events = s.events || { spiral: 0, axial: 0, localizer: 0 };
    const totalEvents = events.spiral + events.axial + events.localizer;
    if (!totalEvents) continue;

    const dlpStr = s.dlp_total != null ? num(s.dlp_total, 1) : '—';
    const ctdiStr = s.ctdivol_max != null ? num(s.ctdivol_max, 2) : '—';

    // Determine event type from the protocol/description
    const desc = s.description || s.protocol || '';
    const hasSpiral = events.spiral > 0;
    const hasLocalizer = events.localizer > 0;
    const hasAxial = events.axial > 0;

    // If only localizer (topogram)
    if (hasLocalizer && !hasSpiral && !hasAxial) {
      for (let i = 0; i < events.localizer; i++) {
        const eventDiv = h('div', { class: 'evb-row', role: 'row' },
          h('span', { class: 'evb-event' }, 'Topogram'),
          h('span', { class: 'evb-dlp mono' }, dlpStr),
          h('i', { class: 'loc', style: { width: '2%' } }),
          h('span', { class: 'evb-ctdivol mono', style: { 'text-align': 'right' } }, ctdiStr));
        list.append(eventDiv);
      }
      continue;
    }

    // Localizer events first
    if (hasLocalizer) {
      for (let i = 0; i < events.localizer; i++) {
        const eventDiv = h('div', { class: 'evb-row', role: 'row' },
          h('span', { class: 'evb-event' }, 'Topogram'),
          h('span', { class: 'evb-dlp mono' }, dlpStr),
          h('i', { class: 'loc', style: { width: '2%' } }),
          h('span', { class: 'evb-ctdivol mono', style: { 'text-align': 'right' } }, ctdiStr));
        list.append(eventDiv);
      }
    }

    // Spiral event
    if (hasSpiral) {
      const eventDiv = h('div', { class: 'evb-row', role: 'row' },
        h('span', { class: 'evb-event' }, desc),
        h('span', { class: 'evb-dlp mono' }, dlpStr),
        h('i', { class: 'loc', style: { width: '66%' } }),
        h('span', { class: 'evb-ctdivol mono', style: { 'text-align': 'right' } }, ctdiStr));
      list.append(eventDiv);
    }

    // Axial event
    if (hasAxial) {
      const eventDiv = h('div', { class: 'evb-row', role: 'row' },
        h('span', { class: 'evb-event' }, desc),
        h('span', { class: 'evb-dlp mono' }, dlpStr),
        h('i', { class: 'loc', style: { width: '32%' } }),
        h('span', { class: 'evb-ctdivol mono', style: { 'text-align': 'right' } }, ctdiStr));
      list.append(eventDiv);
    }
  }

  return h('section', { class: 'panel', 'aria-labelledby': 'h-ev' },
    panelHead(t('overview.events'), { id: 'h-ev' }),
    list);
}

// Reporting deadlines of Order 1255/2016 (art. 8 quarterly, art. 12 annual): shown as the date
// approaches; a person marks one as sent, Dozimetrix sends nothing.
function deadlinePanel(list, reload) {
  if (!list?.length) return null;
  const rows = list.map((d) => {
    const period = d.set ? '' : d.quarter ? `T${d.quarter} ${d.year}` : String(d.month || d.year);
    const left = d.days_left < 0 ? t('deadline.late').replace('{n}', -d.days_left) : t('deadline.left').replace('{n}', d.days_left);
    const href = d.set ? 'local-drl.html' : `reports.html?r=${encodeURIComponent(d.spec)}`;
    const lang = document.documentElement.lang === 'ro' ? 'ro' : 'en';
    return h('li', {},
      h('span', { class: `tag ${d.level}` }, d.level === 'alert' ? '!' : '·'),
      h('span', {}, h('span', { class: 't' }, period ? `${d.title[lang] || d.title.en} · ${period}` : (d.title[lang] || d.title.en)), h('br'),
        h('span', { class: 'm mono' }, `${t('deadline.due').replace('{date}', d.due)} · ${left}`)),
      h('span', { class: 'acts' }, h('a', { class: 'btn', href }, t('deadline.go')),
        h('button', { class: 'btn', type: 'button', onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { await deadlineDone(d.id); reload(); } catch (err) { e.currentTarget.disabled = false; toast(err.message); }
        } }, t('deadline.sent'))));
  });
  return h('section', { class: 'panel mt-3', 'aria-labelledby': 'h-dl' },
    panelHead(t('deadline.title'), { id: 'h-dl' }), h('ul', { class: 'queue' }, rows));
}

async function render(quiet = false) {
  // a live refresh keeps the page as it is until the new data is in (no skeleton flash)
  if (!quiet) page.replaceChildren(h('div', { class: 'state', 'aria-busy': 'true' }, h('span', { class: 'skeleton' }), h('span', { class: 'skeleton', style: { width: '60%' } })));
  try {
    const data = await overview();
    const evPanel = await eventsPanel();
    page.replaceChildren(
      pageHead(t('overview.title'), { style: { 'align-items': 'baseline', gap: '12px' } },
        h('span', { class: 'muted mono' }, dateTime(data.generated))),
      // replaceChildren() would print a null as the text "null" (no deadline left to show)
      ...[deadlinePanel(data.deadlines, render),
        kpis(data.modalities),
        h('div', { class: 'cols stack mt-3' }, evPanel, alertQueue(data.alerts, data.generated, data.alerts_open), feedHealth(data.feeds, data.generated), servicesPanel(), lookupPanel())].filter(Boolean));
  } catch (err) {
    page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'),
      h('span', { class: 'muted mono' }, err.message), h('button', { class: 'btn', type: 'button', onclick: render }, t('state.retry'))));
  }
}

// new studies or alerts: refetch (the overview itself is too heavy to stream every few seconds)
let pulse = null;
let busy = false;
const onPulse = async (p) => {
  const key = JSON.stringify(p);
  if (pulse === null) { pulse = key; return; }
  if (key === pulse || busy) return;
  if (page.contains(document.activeElement)) {  // someone is typing (lookup box): after that
    page.addEventListener('focusout', () => setTimeout(() => onPulse(p), 0), { once: true });
    return;
  }
  busy = true;
  try { dropCache(); await render(true); pulse = key; } finally { busy = false; }
};
render().then(() => live(['pulse', 'services'], { pulse: onPulse, services: drawServices }));
