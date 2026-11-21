// Exam mapping: which local texts (protocol, anatomy, study description) have no standard exam
// code, and the decisions people have recorded. A decision maps one text to a nomenclator code;
// every study with that text is reclassified at once. Saving needs the admin role.

import '../components/dt-shell.js';
import { h, clear, toast } from '../core/dom.js';
import { num } from '../core/format.js';
import { classificationSummary, unclassified, mappings, codes, decideMapping } from '../core/api.js';

const page = document.getElementById('page');
const FAMILIES = { rx: 'Radiography', rf: 'Fluoroscopy / angiography', ct: 'CT' };
const SYSTEM = { rx: 'ro-nom-rx', rf: 'ro-nom-rx', ct: 'ro-nom-ct' };
const FIELD_LABEL = { protocol: 'Protocol', anatomy: 'Anatomy', study_description: 'Study description', procedure_name: 'Procedure' };
const codeCache = new Map();
let family = 'rx';

async function codeList(fam) {
  const sys = SYSTEM[fam];
  if (!codeCache.has(sys)) codeCache.set(sys, await codes(sys));
  const all = codeCache.get(sys);
  // radiography and fluoroscopy share one nomenclator: offer the relevant half
  if (fam === 'rx') return all.filter((c) => c.code.startsWith('RG-'));
  if (fam === 'rf') return all.filter((c) => c.code.startsWith('RF-'));
  return all;
}

function codeSelect(list) {
  const groups = new Map();
  for (const c of list) {
    if (!groups.has(c.chapter)) groups.set(c.chapter, []);
    groups.get(c.chapter).push(c);
  }
  return h('select', { class: 'input', 'aria-label': 'Exam code' },
    h('option', { value: '' }, '— choose exam —'),
    [...groups].map(([chapter, cs]) => h('optgroup', { label: chapter || '' },
      cs.map((c) => h('option', { value: c.code }, `${c.code} · ${c.meaning}`)))));
}

async function render() {
  clear(page);
  page.append(h('nav', { class: 'tabs', 'aria-label': 'Administration', style: { 'margin-bottom': 'var(--sp-3)' } },
    h('a', { href: 'devices.html' }, 'Devices'), h('a', { href: 'dicom.html' }, 'DICOM nodes'),
    h('a', { href: 'review.html', 'aria-current': 'page' }, 'Protocol mappings'),
    h('a', { href: 'drl.html' }, 'DRLs'),
    h('a', { href: 'quality.html' }, 'Data quality'),
    h('a', { href: 'rules.html' }, 'Alert rules')));
  let summary;
  try {
    summary = await classificationSummary();
  } catch (e) {
    page.append(h('p', { class: 'empty' }, e.message));
    return;
  }
  page.append(h('div', { class: 'kpis' }, summary.map((s) => h('div', { class: 'kpi' },
    h('span', { class: 'code' }, FAMILIES[s.family]),
    h('span', { class: 'v' }, s.share == null ? '—' : `${num(s.share * 100, 1)} %`),
    h('span', { class: 'm' }, `${num(s.classified)} of ${num(s.studies)} local studies have an exam code`)))));

  const famBtns = Object.entries(FAMILIES).map(([k, label]) => h('button', {
    type: 'button', 'aria-pressed': String(k === family), dataset: { fam: k },
    onclick: () => { family = k; famBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.fam === k))); loadTodo(); },
  }, label));
  page.append(
    h('div', { class: 'toolbar', style: { 'margin-top': 'var(--sp-3)' } }, h('span', { class: 'seg' }, famBtns),
      h('span', { class: 'muted small' }, 'Most frequent texts without an exam code. Pick the text that identifies the exam, then the code.')),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid', id: 'todo' }))),
    h('h2', { class: 'section-title' }, 'Decisions'),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid', id: 'done' }))),
  );
  await Promise.all([loadTodo(), loadDone()]);
}

async function loadTodo() {
  const table = document.getElementById('todo');
  clear(table).append(h('tbody', {}, h('tr', {}, h('td', {}, 'Loading…'))));
  const fam = family;
  const [items, list] = await Promise.all([unclassified(fam, 100), codeList(fam)]);
  if (fam !== family) return;
  const rx = fam === 'rx';
  const head = h('thead', {}, h('tr', {}, [rx ? 'Exposures' : 'Studies', 'Protocol / anatomy / description', 'Map this text', 'to exam', '']
    .map((c, i) => h('th', { class: i === 0 ? 'r' : '' }, c))));
  const body = h('tbody');
  for (const it of items) {
    const fields = Object.entries(it.texts).filter(([, v]) => v && v.trim());
    const fieldSel = h('select', { class: 'input', 'aria-label': 'Text to map' },
      fields.map(([f, v]) => h('option', { value: f }, `${FIELD_LABEL[f] || f}: ${v}`)));
    const codeSel = codeSelect(list);
    const save = h('button', { class: 'btn xs primary', type: 'button' }, 'Save');
    save.addEventListener('click', async () => {
      if (!codeSel.value) { toast('Choose an exam code first'); return; }
      const field = fieldSel.value;
      save.disabled = true;
      try {
        const r = await decideMapping(fam, field, it.texts[field], codeSel.value);
        toast(`${r.studies_reclassified} studies reclassified as ${codeSel.value}`);
        await Promise.all([loadTodo(), loadDone()]);
      } catch (e) {
        toast(e.message);
        save.disabled = false;
      }
    });
    body.append(h('tr', { class: 'map-row' },
      h('td', { class: 'r' }, num(rx ? it.exposures : it.studies)),
      h('td', { title: fields.map(([, v]) => v).join(' · ') }, fields.map(([, v]) => v).join(' · ') || '(empty)'),
      h('td', {}, fields.length ? fieldSel : h('span', { class: 'muted' }, 'nothing to map')),
      h('td', {}, fields.length ? codeSel : ''),
      h('td', {}, fields.length ? save : '')));
  }
  if (!items.length) body.append(h('tr', {}, h('td', { colspan: 5, class: 'empty' }, 'Every local study of this kind has an exam code.')));
  clear(table).append(head, body);
}

async function loadDone() {
  const table = document.getElementById('done');
  const list = await mappings();
  const head = h('thead', {}, h('tr', {}, ['Kind', 'Field', 'Text', 'Exam', 'Since', ''].map((c) => h('th', {}, c))));
  const body = h('tbody');
  for (const m of list) {
    const del = h('button', { class: 'btn xs ghost', type: 'button', title: 'Remove: the automatic rules apply again' }, 'Remove');
    del.addEventListener('click', async () => {
      try {
        await decideMapping(m.family, m.field, m.text, null);
        toast('Decision removed');
        await Promise.all([loadTodo(), loadDone()]);
      } catch (e) { toast(e.message); }
    });
    body.append(h('tr', {},
      h('td', {}, FAMILIES[m.family] || m.family), h('td', {}, FIELD_LABEL[m.field] || m.field),
      h('td', { class: 'mono' }, m.text), h('td', { title: m.meaning }, `${m.code} · ${m.meaning}`),
      h('td', {}, (m.updated_at || '').slice(0, 10)), h('td', {}, del)));
  }
  if (!list.length) body.append(h('tr', {}, h('td', { colspan: 6, class: 'empty' }, 'No decisions yet: the rules decide everything.')));
  clear(table).append(head, body);
}

render();
