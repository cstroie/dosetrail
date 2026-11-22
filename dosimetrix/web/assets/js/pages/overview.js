// Overview: studies per modality, alert queue, device feed health, exam lookup.

import '../components/dt-shell.js';
import { h, append, clear, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, ago, dateTime } from '../core/format.js';
import { overview, lookup, studyHref, ackAlert, isSample } from '../core/api.js';

// the API's groups are CT, DX, RF, MG (the sample data says XA); anything else takes the accent
const MOD_COLOR = { CT: '--m-ct', DX: '--m-dx', CR: '--m-dx', RF: '--m-rf', XA: '--m-rf', MG: '--m-mg', NM: '--m-nm' };
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
    mods.map((m) => h('a', { class: 'kpi', href: m.code === 'CT' ? 'studies.html' : '#', title: `${m.label}: ${m.weekly.join(', ')}` },
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

function alertQueue(alerts, now, total) {
  const list = h('ul', { class: 'queue' });
  for (const a of alerts) {
    const li = h('li', {},
      h('span', { class: `tag ${a.level}` }, a.kind),
      h('span', {}, h('a', { class: 't', href: alertHref(a) }, a.title), h('br'),
        h('span', { class: 'm mono' }, `${a.accession} · ${dateTime(a.time)} · ${ago(a.time, now)} ago`)),
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
  const open = total ?? alerts.length;  // the API lists the newest 20 and sends the total
  return h('section', { class: 'panel', id: 'alerts', 'aria-labelledby': 'h-q' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-q' }, t('overview.alerts')), h('span', { class: 'tag alert' }, `${open} open`)),
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
    h('span', { class: 'hd', role: 'columnheader', style: { 'text-align': 'right' } }, t('overview.last')));
  for (const f of feeds) {
    const max = Math.max(1, ...f.days.map((d) => d.n));
    append(grid, [h('span', { role: 'rowheader' }, f.name),
      f.days.map((d) => h('i', { class: `c ${d.n ? '' : 'zero'}`, role: 'cell', title: `${d.date}: ${d.n} studies${d.date === today ? ' so far today' : ''}`,
        style: { opacity: d.n ? (0.45 + (d.n / max) * 0.55).toFixed(2) : '1' } })),
      h('span', { class: 'last', role: 'cell', style: { color: `var(--${f.status === 'ok' ? 'text' : f.status})` } },
        f.status === 'ok' ? dateTime(f.last_study).slice(11) : `${ago(f.last_study, now)} · ${t('overview.stale')}`)]);
  }
  return h('section', { class: 'panel', 'aria-labelledby': 'h-f' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-f' }, t('overview.feeds')), stale ? h('span', { class: 'tag warn' }, `${stale} ${t('overview.stale')}`) : null),
    grid, h('p', { class: 'muted', style: { 'font-size': '11px', padding: '0 12px 10px' } }, `${t('overview.feed.hint')} * today, still filling.`));
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
          h('span', { class: 'tag info' }, s.modality), h('span', { class: 'mono' }, s.accession), h('span', { class: 'mono' }, s.dose),
          h('span', { class: 'd' }, `${s.description} · ${s.device_name} · ${dateTime(s.date)}`)));
      }
      if (input.value.trim().length >= 3 && !found.length) results.append(h('span', { class: 'muted' }, t('state.empty')));
    } });
  return h('section', { class: 'panel lookup-panel', 'aria-labelledby': 'h-l' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-l' }, t('overview.lookup'))),
    h('div', { class: 'lookup' }, h('label', { class: 'muted', for: 'lk', style: { 'font-size': '11px' } }, t('overview.lookup.label')), input, results,
      h('span', { class: 'muted', style: { 'font-size': '11px' } }, t('overview.lookup.hint'))));
}

async function render() {
  page.replaceChildren(h('div', { class: 'state', 'aria-busy': 'true' }, h('span', { class: 'skeleton' }), h('span', { class: 'skeleton', style: { width: '60%' } })));
  try {
    const data = await overview();
    page.replaceChildren(
      h('div', { class: 'row', style: { 'align-items': 'baseline', gap: '12px' } }, h('h1', {}, t('overview.title')),
        h('span', { class: 'muted mono' }, dateTime(data.generated))),
      kpis(data.modalities),
      h('div', { class: 'cols', style: { 'margin-top': '12px' } }, alertQueue(data.alerts, data.generated, data.alerts_open), feedHealth(data.feeds, data.generated), lookupPanel()));
  } catch (err) {
    page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'),
      h('span', { class: 'muted mono' }, err.message), h('button', { class: 'btn', type: 'button', onclick: render }, t('state.retry'))));
  }
}

render();
