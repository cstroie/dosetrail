// Unit tests for the query language and statistics (no browser needed).
//   node --test dozimetrix/web/tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parse, serialize, active, matchStudy, parseNumberCond, parseDateCond, suggest } from '../assets/js/core/query.js';
import { summary, quantile } from '../assets/js/core/stats.js';

test('parse and serialize round-trip', () => {
  const q = 'modality:CT date:2026-09-01..2026-11-21 device:"CT 1 – Emergency","CT 2 – Radiology" age:>=18 -phantom head';
  const toks = parse(q);
  assert.equal(toks.length, 6);
  assert.deepEqual(toks[2], { key: 'device', value: '"CT 1 – Emergency","CT 2 – Radiology"', neg: false });
  assert.deepEqual(toks[4], { key: 'phantom', value: '', neg: true });
  assert.deepEqual(toks[5], { key: 'text', value: 'head', neg: false });
  assert.equal(serialize(toks), q);
});

test('romanian aliases and errors', () => {
  assert.equal(parse('aparat:CT1')[0].key, 'device');
  assert.equal(parse('vârstă:>=18')[0].key, 'age');
  assert.match(parse('age:abc')[0].error, /number/);
  assert.match(parse('foo:1')[0].error, /unknown key/);
});

test('number and date conditions', () => {
  assert.equal(parseNumberCond('>700')(701), true);
  assert.equal(parseNumberCond('>700')(700), false);
  assert.equal(parseNumberCond('>=18')(18), true);
  assert.equal(parseNumberCond('0..17')(17), true);
  assert.equal(parseNumberCond('0..17')(18), false);
  assert.equal(parseNumberCond('..5')(-3), true);
  assert.equal(parseNumberCond('x'), null);
  assert.deepEqual(parseDateCond('2026-01-01..2026-02-01'), { from: '2026-01-01', to: '2026-02-01' });
  assert.deepEqual(parseDateCond('30d', '2026-11-21T10:42'), { from: '2026-10-22', to: '2026-11-21' });
});

const devices = new Map([[1, { id: 1, display_name: 'CT 1 – Emergency', model: 'SOMATOM go.Top', station: 'CT1_EMERG', modality: 'CT', origin: 'local' }]]);
const study = {
  accession: 'A2026112100107', date: '2026-11-21T04:36', device_id: 1, description: 'CT head without contrast', protocol: 'Head routine',
  patient: { age: 38, sex: 'M', weight: null, pseudo_id: 'P-8G42' }, events: { spiral: 1, axial: 0, localizer: 1 },
  ctdivol_max: 52.1, dlp_total: 911.0, flags: [], test: false,
};

test('matchStudy applies every token', () => {
  const ctx = { devices, now: '2026-11-21T10:42' };
  const m = (q, s = study) => matchStudy(s, parse(q), ctx);
  assert.equal(m('modality:CT date:7d device:"CT 1" age:>=18 dlp:>900 -phantom'), true);
  assert.equal(m('dlp:>1000'), false);
  assert.equal(m('weight:50..90'), false, 'missing weight never matches a weight range');
  assert.equal(m('acc:A20261121*'), true);
  assert.equal(m('-desc:head'), false);
  assert.equal(m('origin:external'), false);
  assert.equal(m('', { ...study, test: true }), false, 'phantoms hidden by default');
  assert.equal(m('phantom', { ...study, test: true }), true);
});

test('suggestions', () => {
  assert.deepEqual(suggest('dl').map((s) => s.insert), ['dlp:']);
  const ctx = { devices };
  assert.deepEqual(suggest('device:CT', ctx).map((s) => s.insert), ['device:"CT 1 – Emergency"']);
  assert.ok(suggest('-ph').some((s) => s.insert === '-phantom'));
});

test('summary statistics (type 7 quantiles)', () => {
  const s = summary([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  assert.equal(s.n, 10);
  assert.equal(s.median, 5.5);
  assert.equal(s.q1, 3.25);
  assert.equal(s.p75, 7.75);
  assert.equal(s.iqr, 4.5);
  assert.equal(quantile([], 0.5), null);
  assert.deepEqual(summary([null, undefined]), { n: 0 });
});

test('a switched-off filter ("~") is kept in the string but not applied', () => {
  const q = 'modality:CT ~date:30d ~-phantom desc:head';
  const toks = parse(q);
  assert.deepEqual(toks.map((t) => !!t.off), [false, true, true, false]);
  assert.equal(serialize(toks), q);  // round-trips, nothing lost
  assert.equal(active(q), 'modality:CT desc:head');
  assert.equal(active('modality:CT'), 'modality:CT');
  const ctx = { devices: new Map([[1, { id: 1, modality: 'CT' }]]) };
  const s = { device_id: 1, modality: 'CT', date: '2020-01-01T00:00:00', description: 'head' };
  assert.equal(matchStudy(s, parse('modality:CT date:7d'), ctx), false);  // too old
  assert.equal(matchStudy(s, parse('modality:CT ~date:7d'), ctx), true);  // date switched off
});
