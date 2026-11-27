// The tab bars: administration, doses (reference levels) and registers. Analysis draws its own. Each page keeps its
// file name, so bookmarks work; only the bar it shows changes. Registers are for the
// exporter and admin roles (the server enforces it; the bars hide what the user cannot open).
import { h } from '../core/dom.js';
import { me } from '../core/api.js';

export const ADMIN_PAGES = [
  ['Devices', 'devices.html'], ['Integration', 'integration.html'], ['Protocol mappings', 'review.html'], ['Indications', 'indications.html'],
  ['Local DRLs', 'local-drl.html'], ['Alerts', 'rules.html'],
  ['Report digests', 'digests.html'], ['CT coefficients', 'kfactors.html'],
  ['Users', 'users.html'], ['Audit log', 'audit.html'], ['Maintenance', 'maintenance.html'], ['Settings', 'settings.html'],
];
// Third field 'x': the exporter or admin role only (the server enforces it; the bar hides it).
export const DOSE_PAGES = [
  ['Reference levels', 'drl.html'], ['Protocol comparison', 'protocols.html', 'x'],
  ['Before / after', 'analysis.html?r=optimisation', 'x'], ['DRL sets', 'drl-sets.html'],
];
export const ANALYSIS_PAGES = [
  ['Dose Check', 'analysis.html?r=dose-check'], ['Exposure index', 'analysis.html?r=exposure-index'],
  ['Collimation', 'analysis.html?r=collimation'], ['Scan length', 'analysis.html?r=scan-length'],
  ['Repeats', 'analysis.html?r=repeats'], ['Effective dose', 'analysis.html?r=effective-dose'],
  ['Centering', 'analysis.html?r=centering'], ['Data quality', 'quality.html'],
];
export const REGISTER_PAGES = [
  ['Follow-ups', 'followups.html'], ['Incidents', 'incidents.html'], ['QA register', 'qa.html'],
  ['Regulatory reports', 'reports.html'],
];

function bar(label, pages, current, adminOnly) {
  const nav = h('nav', { class: 'tabs', 'aria-label': label, style: { 'margin-bottom': 'var(--sp-3)' } },
    pages.map(([text, href, x]) => h('a', { href, 'data-x': x ? '' : null, 'aria-current': href === current ? 'page' : false }, text)));
  me().then((u) => {
    if (!u) return;
    if (adminOnly && !u.roles.includes('admin')) { nav.remove(); return; }
    if (!u.roles.some((r) => r === 'exporter' || r === 'admin')) nav.querySelectorAll('a[data-x]').forEach((a) => a.remove());
  }).catch(() => {});
  return nav;
}

export const adminTabs = (current) => bar('Administration', ADMIN_PAGES, current, true);
export const doseTabs = (current) => bar('Doses', DOSE_PAGES, current, false);
export const analysisTabs = (current) => bar('Analysis', ANALYSIS_PAGES, current, false);
export const registerTabs = (current) => bar('Registers', REGISTER_PAGES, current, false);
