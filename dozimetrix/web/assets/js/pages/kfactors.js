// CT coefficients (admin): the DLP to effective dose conversion coefficients, mSv per mGy·cm, by
// body region, age and the CTDI phantom of the DLP. None ship: they come from a published set
// the physicist chooses (ICRP 103 based) and each row keeps its source. Saving replaces the whole
// table. The estimate is a population-level one and is not shown for a series whose phantom has no
// row; nothing is guessed from another phantom.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, toast, pageHead } from '../core/dom.js';
import { confirmDialog } from '../core/confirm.js';
import { ctKFactors, saveCtKFactors } from '../core/api.js';
import { dataTable } from '../core/tables.js';

const page = document.getElementById('page');
const host = h('div', { style: { display: 'grid', gap: 'var(--sp-3)' } });

const HEADER = 'region,age_to_years,phantom_cm,k,source';

function parse(text) {
  const rows = [];
  for (const [n, line] of text.split(/\r?\n/).entries()) {
    if (!line.trim() || (n === 0 && line.toLowerCase().startsWith('region'))) continue;
    const m = line.match(/^([^,]*),([^,]*),([^,]*),([^,]*),(.*)$/);
    if (!m) throw new Error(`line ${n + 1}: not "${HEADER}"`);
    rows.push({ region: m[1].trim(), age_to_years: m[2].trim() === '' ? null : Number(m[2]), phantom_cm: Number(m[3]), k: Number(m[4]), source: m[5].trim().replace(/^"|"$/g, '') });
  }
  return rows;
}

async function load() {
  clear(host).append(h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try { r = await ctKFactors(); } catch (e) { clear(host).append(h('p', { class: 'error' }, e.message)); return; }
  const area = h('textarea', { class: 'input mono', rows: 10, style: { width: '100%', height: 'auto', 'min-height': 'calc(10lh + 1.5rem)', padding: '6px 8px' }, spellcheck: false, 'aria-label': 'Coefficients as CSV' },
    [HEADER, ...r.rows.map((x) => `${x.region},${x.age_to_years ?? ''},${x.phantom_cm},${x.k},"${x.source.replaceAll('"', "'")}"`)].join('\n'));
  const save = async () => {
    let rows;
    try { rows = parse(area.value); } catch (e) { toast(e.message); return; }
    if (!(await confirmDialog(rows.length ? `Replace the coefficients with these ${rows.length} rows?` : 'Remove every coefficient? No CT effective dose will be estimated.', { title: 'Save coefficients', label: 'Save' }))) return;
    try { await saveCtKFactors(rows); toast('Saved'); load(); } catch (e) { toast(e.message); }
  };
  clear(host).append(
    r.rows.length ? dataTable([{ key: 'region', label: 'Region' }, { get: (x) => (x.age_to_years == null ? 'adult' : `up to ${x.age_to_years} y`), label: 'Age' }, { key: 'phantom_cm', label: 'Phantom, cm', right: true }, { key: 'k', label: 'k, mSv/(mGy·cm)', right: true, fmt: (v) => String(v) }, { key: 'source', label: 'Source' }], r.rows)
      : h('p', { class: 'panel', style: { padding: 'var(--sp-3)' } }, 'No coefficients: CT effective dose is not estimated. Paste a set below (one row per region, age limit and phantom; empty age = adult).'),
    h('p', { class: 'muted small' }, `Regions: ${r.regions.join(', ')} (abdomen_pelvis covers abdomen, pelvis and lumbar spine; trunk covers chest-abdomen(-pelvis)). Phantom 16 or 32: the CTDI phantom of the DLP the coefficient applies to. Each row needs its source.`),
    area, h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: save }, 'Save')));
}

clear(page).append(adminTabs('kfactors.html'),
  pageHead('CT effective dose coefficients', { sub: 'DLP × k: a population-level estimate for comparing exams, rooms and years, not for a patient. Organ doses are not estimated.' }),
  host);
load();
