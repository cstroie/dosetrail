// The administration tab bar, shared by the admin pages (order as in the Console mockup).
import { h } from '../core/dom.js';

export const ADMIN_PAGES = [
  ['Devices', 'devices.html'], ['Integration', 'integration.html'], ['Protocol mappings', 'review.html'], ['Indications', 'indications.html'],
  ['DRLs', 'drl-sets.html'], ['Local DRLs', 'local-drl.html'], ['Protocol comparison', 'protocols.html'], ['Data quality', 'quality.html'], ['Alerts', 'rules.html'],
  ['Users', 'users.html'], ['Audit log', 'audit.html'], ['Maintenance', 'maintenance.html'], ['Settings', 'settings.html'],
];

export function adminTabs(current) {
  return h('nav', { class: 'tabs', 'aria-label': 'Administration', style: { 'margin-bottom': 'var(--sp-3)' } },
    ADMIN_PAGES.map(([label, href]) => h('a', { href, 'aria-current': href === current ? 'page' : false }, label)));
}
