// The dose information a patient is given in writing with the result (Order MS 1255/2016
// art. 4): the patient, the examination and the dose quantities that apply. A document for the
// patient, so always in Romanian whatever the interface language is. It names the patient: the
// page asks first, and the server needs the pid role and writes an audit entry.

import '../components/dt-shell.js';
import { h, clear, toast } from '../core/dom.js';
import { confirmDialog } from '../core/confirm.js';
import { doseStatement } from '../core/api.js';
import { smart } from '../core/format.js';

const page = document.getElementById('page');
const id = new URLSearchParams(location.search).get('id');

const LABEL = {
  dap: 'Produsul doză-arie (DAP)', esak: 'Kerma în aer la suprafața de intrare (ESAK)',
  rp: 'Doza în punctul de referință (Ka,r)', mgd_right: 'Doza medie glandulară (MGD), sân drept',
  mgd_left: 'Doza medie glandulară (MGD), sân stâng', dlp: 'Produsul doză-lungime (DLP)',
  activity: 'Activitatea administrată',
};

function row(k, v) {
  return h('tr', {}, h('th', { class: 'l' }, k), h('td', {}, v || '—'));
}

function draw(s) {
  const p = s.patient;
  const when = s.when ? s.when.replace('T', ' ') : '';
  clear(page).append(
    h('div', { class: 'panel statement' },
      h('div', { class: 'panel-body' },
        h('h1', { class: 'section-title' }, 'Informare privind doza de radiații primită'),
        h('p', { class: 'muted small' }, 'Ordinul MS nr. 1255/2016, art. 4: valorile se comunică pacientului în scris, odată cu rezultatul examinării.'),
        h('table', { class: 'stats' }, h('tbody', {},
          row('Unitatea', s.institution), row('Pacient', p.name), row('CNP', p.id), row('Data nașterii', p.birth_date),
          row('Examinarea', s.exam), row('Data și ora', when), row('Aparat', s.device))),
        h('h2', { class: 'section-title mt-4' }, 'Doza'),
        s.doses.length
          ? h('table', { class: 'stats' }, h('tbody', {}, s.doses.map((d) => h('tr', {},
            h('th', { class: 'l' }, LABEL[d.key] || d.label, d.note === 'calculated' ? h('span', { class: 'muted small' }, ' (calculată după anexa 2)') : null),
            h('td', { class: 'mono r' }, smart(d.value)), h('td', {}, d.unit)))))
          : h('p', { class: 'muted' }, 'Pentru această examinare aparatul nu a transmis o mărime de doză.'),
        h('p', { class: 'muted small mt-4' }, 'Valorile descriu expunerea aparatului în timpul examinării și sunt folosite pentru optimizare; ele nu sunt doza efectivă a pacientului.'),
        h('p', { class: 'mt-5' }, 'Data eliberării: ', new Date().toLocaleDateString('ro-RO'), '        Semnătura și parafa: ______________________'))),
    h('div', { class: 'row mt-3 no-print' }, h('button', { class: 'btn primary', type: 'button', onclick: () => window.print() }, 'Tipărește')));
}

async function init() {
  if (!id) { clear(page).append(h('p', { class: 'error' }, 'Lipsește studiul (?id=).')); return; }
  const ok = await confirmDialog('Pagina conține numele și CNP-ul pacientului. Deschiderea ei se înscrie în jurnalul de audit.',
    { title: 'Date de identificare', label: 'Deschide', cancel: 'Anulează' });
  if (!ok) { clear(page).append(h('p', { class: 'muted' }, 'Anulat.')); return; }
  try { draw(await doseStatement(id)); } catch (e) {
    clear(page).append(h('p', { class: 'error' }, e.message.includes('pid') ? 'Contul dumneavoastră nu are rolul pid (date de identificare).' : e.message));
    toast(e.message);
  }
}

init();
