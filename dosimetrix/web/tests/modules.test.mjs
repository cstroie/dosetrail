// Every page module must load: one duplicate declaration or a missing export in a shared module
// (core/api.js, core/i18n.js, …) leaves every page that imports it blank in the browser, and no
// server-side test sees it. A tiny fake browser is enough to link and evaluate them.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const pages = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../assets/js/pages');
const el = () => ({ setAttribute() {}, append() {}, addEventListener() {}, replaceChildren() {}, style: { setProperty() {} }, classList: { add() {}, remove() {} }, dataset: {} });
Object.assign(globalThis, {
  document: { documentElement: { dataset: { api: 'sample' }, lang: 'en', setAttribute() {}, getAttribute() {} }, getElementById: el, createElement: el, createTextNode: () => ({}), createElementNS: el, addEventListener() {}, querySelector: () => null, body: { append() {} } },
  localStorage: { getItem: () => null, setItem() {} },
  customElements: { define() {}, get() {} }, HTMLElement: class {}, location: { search: '', href: 'http://x/', origin: 'http://x' },
  matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, Node: class {},
  fetch: async () => ({ ok: true, json: async () => ({}) }),
});
globalThis.window = globalThis;

for (const f of fs.readdirSync(pages).filter((x) => x.endsWith('.js')).sort()) {
  test(`page module loads: ${f}`, async () => {
    await assert.doesNotReject(import(pathToFileURL(path.join(pages, f)).href));
  });
}
