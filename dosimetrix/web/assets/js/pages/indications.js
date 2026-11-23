// Clinical indications (admin): the request texts that are not coded yet (or only
// suggested), most frequent first, with the best matches. One decision codes every study
// with the same text (now and later). Server: dosimetrix/indications.py.

import '../components/dx-shell.js';
import { adminTabs } from '../components/admin-tabs.js';
import { h, clear, toast } from '../core/dom.js';
import { num } from '../core/format.js';
import { indicationReview, decideIndication, codes } from '../core/api.js';

const page = document.getElementById('page');
const STATUS = { auto: 'matched', confirmed: 'mapped', manual: 'by hand', suggested: 'suggested only' };
let vocab = [];

async function decide(text, code) {
  try {
    const r = await decideIndication(text, code);
    toast(code ? `${code}: ${r.studies} studies` : `Removed (${r.studies} studies back to matching)`);
    load();
  } catch (e) { toast(e.message); }
}

async function load() {
  clear(page).append(adminTabs('indications.html'), h('p', { class: 'muted' }, 'Loading…'));
  let r;
  try {
    [r, vocab] = await Promise.all([indicationReview(), vocab.length ? vocab : codes('ro-nom-indications')]);
  } catch (e) { page.append(h('p', { class: 'error' }, e.message)); return; }
  const dl = h('datalist', { id: 'ind-codes' }, vocab.map((c) => h('option', { value: c.code }, `${c.code} ${c.meaning}`)));
  const coded = Object.entries(r.coded);
  const body = h('tbody', {}, r.items.map((it) => {
    const input = h('input', { class: 'input w-120', list: 'ind-codes', placeholder: 'code…' });
    return h('tr', {},
      h('td', {}, it.text, it.children ? h('span', { class: 'muted small' }, ' · mostly children') : null),
      h('td', { class: 'r' }, num(it.studies)),
      h('td', {}, it.suggestions.map((sg) => h('button', { class: 'btn xs', type: 'button', title: `${sg.meaning} (similarity ${sg.score})`, onclick: () => decide(it.text, sg.code) },
        h('span', { class: 'mono' }, sg.code), ' ', sg.meaning.length > 38 ? `${sg.meaning.slice(0, 36)}…` : sg.meaning))),
      h('td', { class: 'row' }, input, h('button', { class: 'btn xs primary', type: 'button', onclick: () => input.value.trim() && decide(it.text, input.value.trim().split(/\s/)[0]) }, 'Set')));
  }));
  if (!r.items.length) body.append(h('tr', {}, h('td', { colspan: 4, class: 'empty' }, 'Every request text is coded.')));
  clear(page).append(adminTabs('indications.html'), dl,
    h('p', { class: 'muted small' }, 'Request texts come from the HIS (the referral-guideline item, else the free text), the DICOM "reason for the request", or a CSV import. Clear matches are coded automatically; these need a person. A decision applies to every study with the same text, now and in future imports. Single studies can be changed on the study page.'),
    h('div', { class: 'kpis drl-kpis' },
      h('div', { class: 'kpi' }, h('span', { class: 'code' }, 'Studies with a request text'), h('span', { class: 'v' }, num(r.studies_with_text))),
      coded.map(([k, v]) => h('div', { class: 'kpi' }, h('span', { class: 'code' }, STATUS[k] || k), h('span', { class: 'v' }, num(v))))),
    h('h2', { class: 'section-title' }, 'To code (most frequent first)'),
    h('div', { class: 'panel' }, h('div', { class: 'grid-scroll' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ['Request text', 'Studies', 'Best matches (click to choose)', 'Other code'].map((c, i) => h('th', { class: i === 1 ? 'r' : '' }, c)))), body))),
    h('h2', { class: 'section-title' }, `Decisions (${r.decisions.length})`),
    h('div', { class: 'panel' }, h('table', { class: 'grid' }, h('tbody', {}, r.decisions.length ? r.decisions.map((d) => h('tr', {},
      h('td', {}, d.text), h('td', { class: 'mono' }, d.code), h('td', {}, d.meaning || ''),
      h('td', {}, h('button', { class: 'btn xs ghost', type: 'button', onclick: () => decide(d.text, null) }, 'Remove')))) :
      h('tr', {}, h('td', { class: 'empty' }, 'None yet.'))))));
}

load();
