// Interface strings in English and Romanian. Keys are stable; add new ones to both maps.
// Number formatting follows the language too (EN 1,412.5 · RO 1.412,5).

import { prefs } from './prefs.js';

const STRINGS = {
  en: {
    'nav.overview': 'Overview', 'nav.studies': 'Studies', 'nav.charts': 'Charts', 'nav.drl': 'Reference levels', 'nav.reports': 'Reports', 'nav.alerts': 'Alerts', 'nav.admin': 'Admin',
    'nav.overview.short': 'Overview', 'nav.studies.short': 'Studies', 'nav.charts.short': 'Charts', 'nav.drl.short': 'DRL', 'nav.reports.short': 'Reports', 'nav.alerts.short': 'Alerts', 'nav.admin.short': 'Admin',
    'shell.search': 'Search accession, patient ID, device, or type a command…',
    'shell.modality': 'Modality', 'shell.origin': 'Origin', 'shell.all': 'All',
    'shell.local': 'Local', 'shell.external': 'External', 'shell.both': 'Local + external',
    'shell.theme.dark': 'Switch to light theme', 'shell.theme.light': 'Switch to dark theme',
    'shell.lang': 'Language: English. Switch to Romanian', 'shell.user': 'User menu',
    'state.loading': 'Loading…', 'state.empty': 'Nothing matches.', 'state.error': 'Could not load data.', 'state.retry': 'Retry',
    'overview.title': 'Situation', 'overview.alerts': 'Alert queue', 'overview.feeds': 'Feed health · studies per day',
    'overview.lookup': 'Exam lookup', 'overview.lookup.label': 'Accession or patient ID',
    'overview.thisweek': 'this week', 'overview.thismonth': 'this month', 'overview.open': 'Open', 'overview.ack': 'Acknowledge',
    'overview.stale': 'stale', 'overview.last': 'Last study', 'overview.device': 'Device',
    'overview.lookup.hint': 'Matches as you type. Patient ID search needs the identified-data role and is audit-logged.',
    'overview.feed.hint': 'Dashed cell = no studies that day. A feed is stale after 2 days (configurable per device).',
    'studies.title': 'Studies', 'studies.count': 'studies', 'studies.columns': 'Columns', 'studies.density': 'Density',
    'studies.chart': 'Chart this →', 'studies.preview': 'Preview', 'studies.open': 'Open ↗', 'studies.keys': 'J/K move · Enter opens',
    'studies.query.hint': 'Keys: date device desc proto age weight acc dlp ctdi spiral loc origin phantom. Tab completes, Enter applies. The query is kept in the URL.',
    'study.readouts': 'Dose readouts', 'study.cumulative': '12-week cumulative', 'study.curve': 'Running Ka,r and events',
    'study.events': 'Events', 'study.skin': 'Skin dose map', 'study.planned': 'planned', 'study.reveal': 'Reveal…', 'study.print': 'Print',
    'charts.workspace': 'Workspace', 'charts.query': 'Workspace query', 'charts.add': 'Add panel', 'charts.reset': 'Reset workspace',
    'admin.devices': 'Physical devices', 'admin.inspector': 'Merge inspector', 'admin.merge': 'Merge', 'admin.reject': 'Not the same device',
  },
  ro: {
    'nav.overview': 'Sinteză', 'nav.studies': 'Studii', 'nav.charts': 'Grafice', 'nav.drl': 'Niveluri de referință', 'nav.reports': 'Rapoarte', 'nav.alerts': 'Alerte', 'nav.admin': 'Administrare',
    'nav.overview.short': 'Sinteză', 'nav.studies.short': 'Studii', 'nav.charts.short': 'Grafice', 'nav.drl.short': 'NRD', 'nav.reports.short': 'Rapoarte', 'nav.alerts.short': 'Alerte', 'nav.admin.short': 'Admin.',
    'shell.search': 'Caută accesie, ID pacient, aparat sau scrie o comandă…',
    'shell.modality': 'Modalitate', 'shell.origin': 'Proveniență', 'shell.all': 'Toate',
    'shell.local': 'Local', 'shell.external': 'Extern', 'shell.both': 'Local + extern',
    'shell.theme.dark': 'Comută pe tema luminoasă', 'shell.theme.light': 'Comută pe tema întunecată',
    'shell.lang': 'Limba: română. Comută pe engleză', 'shell.user': 'Meniu utilizator',
    'state.loading': 'Se încarcă…', 'state.empty': 'Niciun rezultat.', 'state.error': 'Datele nu au putut fi încărcate.', 'state.retry': 'Reîncearcă',
    'overview.title': 'Situație', 'overview.alerts': 'Alerte de rezolvat', 'overview.feeds': 'Flux de date · studii pe zi',
    'overview.lookup': 'Caută examinare', 'overview.lookup.label': 'Accesie sau ID pacient',
    'overview.thisweek': 'săptămâna aceasta', 'overview.thismonth': 'luna aceasta', 'overview.open': 'Deschide', 'overview.ack': 'Confirmă',
    'overview.stale': 'inactiv', 'overview.last': 'Ultimul studiu', 'overview.device': 'Aparat',
    'overview.lookup.hint': 'Rezultatele apar pe măsură ce scrieți. Căutarea după ID pacient cere rolul de date identificate și este înregistrată.',
    'overview.feed.hint': 'Celulă punctată = niciun studiu în ziua respectivă. Fluxul devine inactiv după 2 zile (configurabil pe aparat).',
    'studies.title': 'Studii', 'studies.count': 'studii', 'studies.columns': 'Coloane', 'studies.density': 'Densitate',
    'studies.chart': 'Grafic →', 'studies.preview': 'Previzualizare', 'studies.open': 'Deschide ↗', 'studies.keys': 'J/K mută · Enter deschide',
    'studies.query.hint': 'Chei: date device desc proto age weight acc dlp ctdi spiral loc origin phantom (sunt acceptate și aparat:, vârstă:). Tab completează, Enter aplică. Interogarea este păstrată în URL.',
    'study.readouts': 'Valori de doză', 'study.cumulative': 'Cumulat 12 săptămâni', 'study.curve': 'Ka,r cumulat și evenimente',
    'study.events': 'Evenimente', 'study.skin': 'Hartă doză piele', 'study.planned': 'planificat', 'study.reveal': 'Afișează…', 'study.print': 'Tipărește',
    'charts.workspace': 'Spațiu de lucru', 'charts.query': 'Interogarea spațiului', 'charts.add': 'Adaugă panou', 'charts.reset': 'Resetează',
    'admin.devices': 'Aparate fizice', 'admin.inspector': 'Verificare unificare', 'admin.merge': 'Unifică', 'admin.reject': 'Nu este același aparat',
  },
};

export function lang() {
  return prefs.get('lang') === 'ro' ? 'ro' : 'en';
}

export function t(key) {
  return STRINGS[lang()][key] ?? STRINGS.en[key] ?? key;
}

/** Locale for Intl formatting. */
export function locale() {
  return lang() === 'ro' ? 'ro-RO' : 'en-GB';
}
