// Runs the browser query language (dozimetrix/web/assets/js/core/query.js) on the shared
// corpus and checks it against the expected results that the Python side also asserts.
//   node --test tests/js/query_parity.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, matchStudy } from '../../dozimetrix/web/assets/js/core/query.js';

const corpus = JSON.parse(readFileSync(new URL('../query_corpus.json', import.meta.url)));
const expected = JSON.parse(readFileSync(new URL('../query_expected.json', import.meta.url)));
const ctx = { devices: new Map(corpus.devices.map((d) => [d.id, d])), now: corpus.now };

export function run(q) {
  return corpus.studies.filter((s) => matchStudy(s, parse(q), ctx)).map((s) => s.accession);
}

for (const { q } of corpus.cases) {
  test(`query "${q}"`, () => assert.deepEqual(run(q), expected[q]));
}
