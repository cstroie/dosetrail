// The administration tab bar, shared by the admin pages (order as in the Console mockup).
import { h } from '../core/dom.js';

export const ADMIN_PAGES = [
  ['Devices', 'devices.html'], ['Integration', 'dicom.html'], ['Protocol mappings', 'review.html'],
  ['DRLs', 'drl-sets.html'], ['Local DRLs', 'local-drl.html'], ['Data quality', 'quality.html'], ['Alert rules', 'rules.html'],
  ['Users', 'users.html'], ['Audit log', 'audit.html'],
];

export function adminTabs(current) {
  return h('nav', { class: 'tabs', 'aria-label': 'Administration', style: { 'margin-bottom': 'var(--sp-3)' } },
    ADMIN_PAGES.map(([label, href]) => h('a', { href, 'aria-current': href === current ? 'page' : false }, label)));
}
