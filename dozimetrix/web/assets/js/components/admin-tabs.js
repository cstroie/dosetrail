// The tab bars: administration, reports (analyses) and quality (registers). Each page keeps its
// file name, so bookmarks work; only the bar it shows changes. Reports and Quality are for the
// exporter and admin roles (the server enforces it; the bars hide what the user cannot open).
import { h } from '../core/dom.js';
import { me } from '../core/api.js';

export const ADMIN_PAGES = [
  ['Devices', 'devices.html'], ['Integration', 'integration.html'], ['Protocol mappings', 'review.html'], ['Indications', 'indications.html'],
  ['Local DRLs', 'local-drl.html'], ['Alerts', 'rules.html'],
  ['Report digests', 'digests.html'], ['CT coefficients', 'kfactors.html'],
  ['Users', 'users.html'], ['Audit log', 'audit.html'], ['Maintenance', 'maintenance.html'], ['Settings', 'settings.html'],
];
export const REPORT_PAGES = [
  ['Reports', 'reports.html'], ['DRLs', 'drl-sets.html'], ['Protocol comparison', 'protocols.html'], ['Data quality', 'quality.html'],
];
export const QUALITY_PAGES = [
  ['Follow-ups', 'followups.html'], ['Incidents', 'incidents.html'], ['QA register', 'qa.html'],
];

function bar(label, pages, current, adminOnly) {
  const nav = h('nav', { class: 'tabs', 'aria-label': label, style: { 'margin-bottom': 'var(--sp-3)' } },
    pages.map(([text, href]) => h('a', { href, 'aria-current': href === current ? 'page' : false }, text)));
  if (adminOnly) me().then((u) => { if (u && !u.roles.includes('admin')) nav.remove(); }).catch(() => {});
  return nav;
}

export const adminTabs = (current) => bar('Administration', ADMIN_PAGES, current, true);
export const reportTabs = (current) => bar('Reports', REPORT_PAGES, current, false);
export const qualityTabs = (current) => bar('Quality', QUALITY_PAGES, current, false);
