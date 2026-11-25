// The study's clinical indication (ro-nom-indications) for the study pages: the code and its
// meaning, how it was found (HIS guideline item / free text, DICOM, CSV, a person), and
// for admins a "Change…" control. Server side: dozimetrix/indications.py.
import { h, toast } from '../core/dom.js';
import { me, codes, setStudyIndication } from '../core/api.js';

const STATUS = { auto: ['ok', 'matched'], confirmed: ['ok', 'mapped'], manual: ['ok', 'set by hand'], suggested: ['warn', 'suggested'] };
const SOURCE = { his_guide: 'HIS guideline item', his_text: 'HIS request text', dicom: 'DICOM request', csv: 'CSV import', manual: 'set by hand' };

function view(ind) {
  if (!ind) return h('span', { class: 'muted' }, '—');
  const tag = ind.status ? h('span', { class: `tag ${STATUS[ind.status]?.[0] || ''}`, title: SOURCE[ind.source] || '' }, STATUS[ind.status]?.[1] || ind.status) : null;
  return h('span', {},
    ind.code ? h('span', { title: ind.text || '' }, h('span', { class: 'mono' }, ind.code), ' ', ind.meaning) : h('span', { class: 'muted', title: 'not coded yet' }, `“${ind.text}”`),
    ' ', tag,
    ind.code && ind.text ? h('div', { class: 'muted small' }, `“${ind.text}”`) : null);
}

export function indicationRow(s) {
  const dd = h('dd', {}, view(s.indication));
  me().then((u) => {
    if (!u || !(u.roles || []).includes('admin')) return;
    const btn = h('button', { class: 'btn xs ghost no-print', type: 'button' }, 'Change…');
    btn.addEventListener('click', async () => {
      const list = await codes('ro-nom-indications').catch(() => null);
      const items = (list?.codes || list || []);
      const id = `ind-${s.id}`;
      const input = h('input', { class: 'input w-260', list: id, placeholder: 'code or words…' });
      const dl = h('datalist', { id }, items.map((c) => h('option', { value: c.code }, `${c.code} ${c.meaning}`)));
      const save = async (code) => {
        try { s.indication = await setStudyIndication(s.id, code); dd.replaceChildren(view(s.indication), ' ', btn); } catch (e) { toast(e.message); }
      };
      dd.replaceChildren(view(s.indication), h('div', { class: 'row no-print' }, input, dl,
        h('button', { class: 'btn xs primary', type: 'button', onclick: () => save(input.value.trim().split(/\s/)[0] || null) }, 'Save'),
        h('button', { class: 'btn xs', type: 'button', title: 'Back to automatic matching', onclick: () => save(null) }, 'Automatic')));
    });
    dd.append(' ', btn);
  }).catch(() => {});
  return [h('dt', {}, 'Indication'), dd];
}
