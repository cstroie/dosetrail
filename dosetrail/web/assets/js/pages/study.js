// Study detail (fluoroscopy / interventional): facts, dose readouts with thresholds,
// 12-week cumulative dose, running Ka,r curve with events, event table, skin-dose placeholder.
// Clicking an event bar selects the row in the table, and selecting a row highlights the bar.

import '../components/dt-shell.js';
import '../components/dt-data-grid.js';
import '../components/dt-chart.js';
import { baseOption } from '../components/dt-chart.js';
import { h, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { num, dap, dapUnit, mmss, clock, DAP_UNITS } from '../core/format.js';
import { study, isSample } from '../core/api.js';
import { prefs } from '../core/prefs.js';

const page = document.getElementById('page');
const params = new URL(location.href).searchParams;
const acc = params.get('acc') || 'A2026112100123';
const ref = params.get('id') ?? acc; // the API addresses studies by id

function gauge(value, limit) {
  const pct = Math.min(100, (value / (limit * 1.15)) * 100);
  const mark = (limit / (limit * 1.15)) * 100;
  const over = value >= limit;
  return h('span', { class: 'gauge', role: 'img', 'aria-label': `${Math.round((value / limit) * 100)}% of the threshold` },
    h('i', { style: { width: `${pct}%`, background: `var(--${over ? 'alert' : 'accent'})` } }),
    h('b', { style: { left: `${mark}%` } }));
}

function readouts(s) {
  const ev = s.events;
  const sum = (f, k) => ev.filter(f).reduce((a, e) => a + e[k], 0);
  const u = dapUnit();
  const T = s.totals;
  const th = s.thresholds;
  const dapF = sum((e) => e.type === 'fluoro', 'dap_gycm2');
  const row = (label, value, opts = {}) => h('div', { class: `readout ${opts.hot ? 'hot' : ''}` },
    h('span', { class: 'l' }, label), h('span', { class: `v ${opts.sm ? 'sm' : ''}` }, value, opts.unit ? h('span', { class: 'unit' }, ` ${opts.unit}`) : null),
    opts.gauge || null, opts.sub ? h('span', { class: 'sub' }, opts.sub) : null);
  return [
    row('Total DAP', dap(T.dap_gycm2), { unit: u, hot: T.dap_gycm2 >= th.dap_gycm2, gauge: gauge(T.dap_gycm2, th.dap_gycm2), sub: `trigger ${dap(th.dap_gycm2)} ${u}` }),
    row('Total Ka,r', num(T.kar_gy, 2), { unit: 'Gy', hot: T.kar_gy >= th.kar_gy, gauge: gauge(T.kar_gy, th.kar_gy), sub: `alert ${num(th.kar_gy, 2)} Gy · notify ${num(th.kar_notify_gy, 2)} Gy` }),
    row('Fluoro time', mmss(T.fluoro_s), { unit: 'mm:ss', sub: `${ev.filter((e) => e.type === 'fluoro').length} fluoro events` }),
    row('Acquisitions', num(T.acquisitions), { unit: 'runs', sub: `${num(T.frames)} frames` }),
    row('Fluoro / acq. DAP', `${dap(dapF)} / ${dap(T.dap_gycm2 - dapF)}`, { sm: true, sub: `${Math.round((dapF / T.dap_gycm2) * 100)}% / ${100 - Math.round((dapF / T.dap_gycm2) * 100)}% · ${u}` }),
    row('Plane A / B DAP', `${dap(sum((e) => e.plane === 'A', 'dap_gycm2'))} / ${dap(sum((e) => e.plane === 'B', 'dap_gycm2'))}`, { sm: true,
      sub: `Ka,r ${num(sum((e) => e.plane === 'A', 'kar_mgy') / 1000, 2)} / ${num(sum((e) => e.plane === 'B', 'kar_mgy') / 1000, 2)} Gy` }),
    row('Peak skin dose', '—', { sub: 'estimate in a later release' }),
  ];
}

function cumulative(s) {
  const items = [...s.previous, { date: s.start.slice(0, 10), dap_gycm2: s.totals.dap_gycm2, kar_gy: s.totals.kar_gy, description: 'this study' }];
  const max = Math.max(...items.map((x) => x.dap_gycm2));
  const sumDap = items.reduce((a, x) => a + x.dap_gycm2, 0);
  const sumKar = items.reduce((a, x) => a + x.kar_gy, 0);
  const over = sumKar >= s.thresholds.cum_kar_gy;
  return h('section', { class: 'panel', 'aria-labelledby': 'h-cu' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'h-cu' }, t('study.cumulative')),
      over ? h('span', { class: 'tag alert' }, `Σ Ka,r ${num(sumKar, 2)} Gy`) : null),
    h('div', { class: 'cum', role: 'table', 'aria-label': 'Studies in the last 12 weeks' },
      h('span', { class: 'h', role: 'columnheader' }, 'Date'), h('span', { class: 'h', role: 'columnheader' }, 'DAP'),
      h('span', { class: 'h', role: 'columnheader', style: { 'text-align': 'right' } }, dapUnit()), h('span', { class: 'h', role: 'columnheader', style: { 'text-align': 'right' } }, 'Ka,r Gy'),
      items.map((x) => [
        h('span', { class: 'mono', title: x.description }, x.date.slice(5)),
        h('i', { class: 'bar', style: { width: `${(x.dap_gycm2 / max) * 100}%` }, 'aria-hidden': 'true' }),
        h('span', { class: 'mono', style: { 'text-align': 'right' } }, dap(x.dap_gycm2)),
        h('span', { class: 'mono', style: { 'text-align': 'right' } }, num(x.kar_gy, 2))]),
      h('span', { class: 'tot' }, `Σ ${items.length}`), h('span', { class: 'tot' }),
      h('span', { class: 'mono tot', style: { 'text-align': 'right' } }, dap(sumDap)),
      h('span', { class: 'mono tot', style: { 'text-align': 'right', color: over ? 'var(--alert)' : 'inherit' } }, num(sumKar, 2))),
    h('p', { class: 'muted', style: { 'font-size': '11px', padding: '0 12px 10px' } },
      `Alert levels over ${s.thresholds.cum_weeks} weeks: Σ Ka,r ${num(s.thresholds.cum_kar_gy, 2)} Gy, Σ DAP ${dap(s.thresholds.cum_dap_gycm2)} ${dapUnit()}.`));
}

function curveOption(s, selected) {
  return (th) => {
    const base = baseOption(th);
    const t0 = new Date(s.events[0].time).getTime();
    const mins = (iso) => (new Date(iso).getTime() - t0) / 60000;
    let run = 0;
    const line = [[0, 0]];
    for (const e of s.events) {
      line.push([mins(e.time), run / 1000]);
      run += e.kar_mgy;
      line.push([mins(e.time) + e.duration_s / 60, run / 1000]);
    }
    const f = DAP_UNITS[dapUnit()].factor;
    const bar = (type) => s.events.map((e, i) => (e.type === type ? {
      value: [mins(e.time), e.dap_gycm2 * f],
      itemStyle: { color: type === 'fluoro' ? th.devices[0] : th.devices[1], opacity: e.plane === 'B' ? 0.55 : 1,
        borderColor: i === selected ? th.text : 'transparent', borderWidth: i === selected ? 2 : 0 },
      eventIndex: i } : null)).filter(Boolean);
    const [sh, sm] = s.events[0].time.slice(11, 16).split(':').map(Number);
    const hhmm = (v) => { const m = Math.round(sh * 60 + sm + v); return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`; };
    const xMax = Math.ceil(line[line.length - 1][0] / 5) * 5;
    return {
      ...base,
      legend: { ...base.legend, data: ['Σ Ka,r', 'Fluoro', 'Acquisition'], left: 'center' },
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p) => (p.seriesName === 'Σ Ka,r' ? `Σ Ka,r ${num(p.value[1], 2)} Gy`
        : `#${p.data.eventIndex + 1} ${p.seriesName} · plane ${s.events[p.data.eventIndex].plane}<br>${clock(s.events[p.data.eventIndex].time)} · DAP ${dap(s.events[p.data.eventIndex].dap_gycm2, 1)} ${dapUnit()}`) },
      grid: [{ left: 56, right: 90, top: 44, height: '46%' }, { left: 56, right: 90, top: '68%', bottom: 30 }],
      xAxis: [
        { type: 'value', gridIndex: 0, min: 0, max: xMax, ...base._axis, axisLabel: { show: false }, splitLine: { show: false } },
        { type: 'value', gridIndex: 1, min: 0, max: xMax, interval: 15, ...base._axis, axisLabel: { ...base._axis.axisLabel, formatter: hhmm }, splitLine: { show: false } },
      ],
      yAxis: [
        { type: 'value', gridIndex: 0, name: 'Ka,r Gy', min: 0, max: 4, ...base._axis },
        { type: 'value', gridIndex: 1, name: `DAP ${dapUnit()}`, ...base._axis, splitNumber: 2 },
      ],
      series: [
        { name: 'Σ Ka,r', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: line, showSymbol: false, lineStyle: { color: th.accent, width: 2.5 }, itemStyle: { color: th.accent },
          markLine: { silent: true, symbol: 'none', label: { position: 'end', color: th.muted, fontFamily: th.mono, fontSize: 10, formatter: '{b}' },
            data: [
              { name: `alert ${num(s.thresholds.kar_gy, 1)} Gy`, yAxis: s.thresholds.kar_gy, lineStyle: { color: th.alert, type: 'dashed' } },
              { name: `notify ${num(s.thresholds.kar_notify_gy, 1)} Gy`, yAxis: s.thresholds.kar_notify_gy, lineStyle: { color: th.warn, type: 'dashed' } }] } },
        { name: 'Fluoro', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: bar('fluoro'), barWidth: 5, itemStyle: { color: th.devices[0] } },
        { name: 'Acquisition', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: bar('acquisition'), barWidth: 5, barGap: '-100%', itemStyle: { color: th.devices[1] } },
      ],
    };
  };
}

const EVENT_COLUMNS = [
  { key: 'no', label: '#', align: 'right', width: '40px' },
  { key: 'time', label: 'Time', width: '80px', render: (e) => clock(e.time) },
  { key: 'protocol', label: 'Protocol', render: (e) => h('span', { style: { 'font-family': 'var(--font-sans)' } }, e.protocol) },
  { key: 'type', label: 'Type', width: '90px', render: (e) => h('span', { style: { color: `var(${e.type === 'fluoro' ? '--d1' : '--d2'})` } }, e.type === 'fluoro' ? 'FLUORO' : 'ACQ') },
  { key: 'plane', label: 'Pl', width: '36px' },
  { key: 'kvp', label: 'kVp', align: 'right' }, { key: 'ma', label: 'mA', align: 'right' },
  { key: 'pulse_rate', label: 'Pulse', unit: '/s', align: 'right' }, { key: 'cu_mm', label: 'Cu', unit: 'mm', align: 'right' },
  { key: 'primary_angle', label: 'Primary', align: 'right', render: (e) => `${e.primary_angle < 0 ? 'RAO' : 'LAO'} ${Math.abs(e.primary_angle)}` },
  { key: 'secondary_angle', label: 'Second.', align: 'right', render: (e) => `${e.secondary_angle < 0 ? 'CAU' : 'CRA'} ${Math.abs(e.secondary_angle)}` },
  { key: 'field_cm', label: 'Field', unit: 'cm', align: 'right' }, { key: 'sid_cm', label: 'SID', unit: 'cm', align: 'right' },
  { key: 'dap_gycm2', label: 'DAP', unit: '', align: 'right', render: (e) => dap(e.dap_gycm2, 1) },
  { key: 'kar_mgy', label: 'Ka,r', unit: 'mGy', align: 'right', render: (e) => num(e.kar_mgy, 1) },
  { key: 'duration_s', label: 'Dur.', unit: 's', align: 'right', render: (e) => num(e.duration_s, 1) },
];

async function render() {
  const s = await study(ref);
  let selected = 0;
  const p = s.patient;
  const identity = h('span', { class: 'mono' }, p.pseudo_id);
  const revealBtn = h('button', { class: 'btn xs no-print', type: 'button', onclick: () => {
    if (!confirm('Show the patient name and ID? This is recorded in the audit log.')) return;
    identity.textContent = `${p.identified.name} · ${p.identified.id}`;
    revealBtn.remove();
  } }, t('study.reveal'));

  const unitSeg = h('span', { class: 'seg no-print', role: 'group', 'aria-label': 'DAP unit' },
    Object.keys(DAP_UNITS).map((u) => h('button', { type: 'button', 'aria-pressed': String(u === dapUnit()), onclick: () => {
      prefs.set('dapUnit', u);
      document.dispatchEvent(new CustomEvent('dt-unit-change', { detail: u }));
    } }, u)));

  const readoutBox = h('div');
  const cumHost = h('div');
  const chart = h('dt-chart', { label: 'Cumulative Ka,r over the procedure, with irradiation events as bars (DAP)', height: '320' });
  const grid = h('dt-data-grid', { class: 'study-events', label: 'Irradiation events', noun: 'events', 'storage-key': 'grid.events' },
    h('button', { slot: 'actions', class: 'btn', type: 'button', onclick: () => exportEvents(s) }, 'CSV'));
  const selInfo = h('span', { class: 'muted mono' });

  const refresh = () => {
    readoutBox.replaceChildren(...readouts(s));
    cumHost.replaceChildren(cumulative(s));
    EVENT_COLUMNS.find((c) => c.key === 'dap_gycm2').unit = dapUnit();
    chart.draw(curveOption(s, selected));
    grid.reload();
    unitSeg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.textContent === dapUnit())));
  };

  grid.columns = EVENT_COLUMNS;
  grid.rowId = (e) => e.no;
  grid.setState({ sort: 'no', dir: 'asc', page: 1, size: 500 });
  grid.fetcher = async ({ sort, dir }) => {
    const rows = [...s.events].sort((a, b) => (a[sort] < b[sort] ? -1 : a[sort] > b[sort] ? 1 : 0) * (dir === 'asc' ? 1 : -1));
    return { total: rows.length, rows };
  };
  grid.addEventListener('row-focus', (e) => {
    const i = e.detail.no - 1;
    if (i === selected) return;
    selected = i;
    selInfo.textContent = `selected #${i + 1}`;
    chart.draw(curveOption(s, selected));
  });

  const sampleNote = isSample && acc !== s.accession
    ? h('div', { class: 'panel panel-body', role: 'note' }, h('span', { class: 'tag info' }, 'Sample'),
      ` Only ${s.accession} has full detail in sample mode, so it is shown instead of ${acc}.`)
    : null;

  page.replaceChildren(
    sampleNote || '',
    h('div', { class: 'row' }, h('h1', {}, s.description), s.alerts.map((a) => h('span', { class: `tag ${a.level}`, title: a.text }, a.kind === 'kar' ? 'Ka,r ≥ alert' : 'Σ 12 wk ≥ alert')),
      h('span', { class: 'spacer' }), unitSeg, h('button', { class: 'btn no-print', type: 'button', onclick: () => print() }, t('study.print'))),
    h('div', { class: 'detail' },
      h('div', { class: 'side' },
        h('section', { class: 'panel', 'aria-label': 'Patient and exam' },
          h('dl', { class: 'kv' },
            h('dt', {}, 'Accession'), h('dd', { class: 'mono' }, s.accession),
            h('dt', {}, 'Patient'), h('dd', {}, identity, ' ', revealBtn),
            h('dt', {}, 'Age · sex'), h('dd', { class: 'mono' }, `${p.age} y · ${p.sex}`),
            h('dt', {}, 'Height · weight'), h('dd', { class: 'mono' }, `${p.height_cm ?? '—'} cm · ${p.weight_kg ?? '—'} kg`),
            h('dt', {}, 'Date'), h('dd', { class: 'mono' }, `${s.start.replace('T', ' ')}–${s.end.slice(11)}`),
            h('dt', {}, 'Device'), h('dd', {}, `${s.device.name}${s.device.biplane ? ' (biplane)' : ''}`),
            h('dt', {}, 'Physician'), h('dd', {}, s.physician))),
        h('section', { class: 'panel', 'aria-labelledby': 'h-ro' }, h('div', { class: 'panel-head' }, h('h2', { id: 'h-ro' }, t('study.readouts'))), readoutBox),
        cumHost),
      h('div', { class: 'main' },
        h('section', { class: 'panel', 'aria-labelledby': 'h-tl' },
          h('div', { class: 'panel-head' }, h('h2', { id: 'h-tl' }, t('study.curve')), h('span', { class: 'spacer' }),
            h('span', { class: 'muted', style: { 'font-size': '11px' } }, 'faded bar = plane B · click a bar to find it in the table'),
            h('button', { class: 'btn xs no-print', type: 'button', onclick: () => chart.downloadPNG(`${s.accession}-dose-curve`) }, 'PNG')),
          h('div', { class: 'panel-body' }, chart)),
        grid,
        selInfo,
        h('section', { class: 'panel', 'aria-labelledby': 'h-sk' },
          h('div', { class: 'panel-head' }, h('h2', { id: 'h-sk' }, t('study.skin')), h('span', { class: 'tag info' }, t('study.planned'))),
          h('div', { class: 'skin' }, h('div', {}, '2D unfolded body surface · heat map (mGy)'), h('div', {}, '3D phantom · later'))))));

  document.title = `${s.accession} · DoseTrail`;
  refresh();
  chart.on('click', (ev) => {
    if (ev.data?.eventIndex === undefined) return;
    selected = ev.data.eventIndex;
    grid.focusById(selected + 1);
    chart.draw(curveOption(s, selected));
  });
  document.addEventListener('dt-unit-change', refresh);
}

function exportEvents(s) {
  const cols = ['no', 'time', 'protocol', 'type', 'plane', 'kvp', 'ma', 'pulse_rate', 'cu_mm', 'primary_angle', 'secondary_angle', 'field_cm', 'sid_cm', 'dap_gycm2', 'kar_mgy', 'duration_s'];
  const text = [cols.join(',')].concat(s.events.map((e) => cols.map((c) => e[c]).join(','))).join('\r\n');
  const href = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  h('a', { href, download: `${s.accession}-events.csv` }).click();
  toast('Exported events (DAP in Gy·cm², Ka,r in mGy)');
}

render().catch((err) => page.replaceChildren(h('div', { class: 'state' }, h('span', { class: 'tag alert' }, 'Error'), t('state.error'), h('span', { class: 'muted mono' }, err.message))));
