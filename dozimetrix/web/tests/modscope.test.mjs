// The modality tab owns the modality: splitting it out of queries never broadens them.
//   node --test dozimetrix/web/tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parse, splitModality, scopeQuery, groupOf, retab, withoutAuto, normaliseSaved, scopeWarnings, panelWarnings, groupsOfModalities } from '../assets/js/core/query.js';

const P = [
  { code: 'CT', modalities: ['CT'] }, { code: 'DX', modalities: ['DX', 'CR'] }, { code: 'RF', modalities: ['RF'] },
  { code: 'MG', modalities: ['MG'] }, { code: 'NM', modalities: ['NM'] }, { code: 'PT', modalities: ['PT'] },
];

test('a token naming the whole group becomes the tab', () => {
  assert.deepEqual(splitModality('modality:CT age:>=18', P), { mod: 'CT', q: 'age:>=18', dropped: [], multi: false });
  assert.equal(splitModality('modality:DX,CR date:90d', P).mod, 'DX');
  assert.equal(splitModality('modality:dx,cr date:90d', P).q, 'date:90d');
  assert.equal(splitModality('modalitate:CT', P).mod, 'CT');
});

test('never broaden: part of a group stays as a narrowing filter', () => {
  const r = splitModality('modality:DX age:0..17', P);
  assert.deepEqual([r.mod, r.q], ['DX', 'modality:DX age:0..17']);
  const eff = scopeQuery(r.mod, r.q, P);
  assert.equal(eff, 'modality:DX,CR modality:DX age:0..17');
  assert.deepEqual(splitModality(eff, P), { mod: 'DX', q: 'modality:DX age:0..17', dropped: [], multi: false });
  assert.equal(splitModality('modality:CR', P).q, 'modality:CR');
  assert.equal(splitModality('-modality:CR', P, 'DX').q, '-modality:CR');
});

test('several groups: the All tab, tokens kept', () => {
  assert.deepEqual(splitModality('modality:CT,MG date:30d', P), { mod: null, q: 'modality:CT,MG date:30d', dropped: [], multi: true });
  assert.equal(splitModality('modality:CT modality:DX', P).multi, true);  // contradiction: left visible
  assert.equal(splitModality('modality:CT,MG modality:CT', P).mod, 'CT');  // the intersection is one group
  assert.equal(splitModality('modality:CT,MG modality:CT', P).q, '');
});

test('negations and switched-off tokens never move the tab', () => {
  assert.deepEqual(splitModality('-modality:NM age:>=18', P, 'CT'), { mod: 'CT', q: 'age:>=18', dropped: ['-modality:NM'], multi: false });
  assert.equal(splitModality('-modality:NM', P).mod, null);
  assert.equal(splitModality('-modality:NM', P).q, '-modality:NM');
  assert.deepEqual(splitModality('~modality:CT date:7d', P, 'DX'), { mod: 'DX', q: '~modality:CT date:7d', dropped: [], multi: false });
  assert.equal(splitModality('acc:123*', P).mod, null);
});

test('scopeQuery', () => {
  assert.equal(scopeQuery('CT', '', P), 'modality:CT');
  assert.equal(scopeQuery(null, 'date:7d', P), 'date:7d');
  assert.equal(scopeQuery('XX', 'date:7d', P), 'date:7d');
});

test('groupOf intersects every active positive token', () => {
  assert.equal(groupOf('modality:CT,DX modality:DX', P).code, 'DX');
  assert.equal(groupOf('modality:CT,DX', P), null);
  assert.equal(groupOf('~modality:CT', P), null);
  assert.equal(groupOf('-modality:CT', P), null);
  assert.equal(groupOf('modality:CT,US', P), null);  // a modality of no group
});

test('retab switches off what does not fit, and back on', () => {
  const a = retab('age:0..17 dlp:>500 ~ctdi:>20', 'DX', P);
  assert.equal(a.q, 'age:0..17 ~dlp:>500 ~ctdi:>20');
  assert.deepEqual(a.off, ['dlp:>500']);
  const b = retab(a.q, 'NM', P, a.auto);  // NM has DLP too
  assert.equal(b.q, 'age:0..17 dlp:>500 ~ctdi:>20');
  const c = retab(a.q, 'CT', P, a.auto);
  assert.equal(c.q, 'age:0..17 dlp:>500 ~ctdi:>20');  // the hand-made ~ctdi stays off
  assert.deepEqual(c.on, ['dlp:>500']);
  assert.equal(c.auto.size, 0);
  assert.equal(retab('modality:CR age:>=18', 'CT', P).q, '~modality:CR age:>=18');
  assert.equal(retab('dlp:>500', null, P).q, 'dlp:>500');  // All: everything applies
  // a narrowing modality: typed on All it stays; the All tab clicked switches it off, DX brings it back
  assert.equal(retab('modality:CT,MG', null, P).q, 'modality:CT,MG');
  const d = retab('modality:CR age:>=18', null, P, new Set(), { clicked: true });
  assert.equal(d.q, '~modality:CR age:>=18');
  assert.equal(retab(d.q, 'DX', P, d.auto, { clicked: true }).q, 'modality:CR age:>=18');
});

test('withoutAuto keeps hand-made ~ only', () => {
  const a = retab('dlp:>500 ~age:>=18', 'DX', P);
  assert.equal(withoutAuto(a.q, a.auto), '~age:>=18');
  assert.equal(withoutAuto('dlp:>500', new Set()), 'dlp:>500');
});

test('normaliseSaved: old queries get a mod, new ones are kept', () => {
  assert.deepEqual(normaliseSaved({ name: 'a', q: 'modality:CT date:90d' }, P), { name: 'a', q: 'date:90d', mod: 'CT' });
  assert.deepEqual(normaliseSaved({ name: 'b', q: 'modality:CT,MG' }, P), { name: 'b', q: 'modality:CT,MG', mod: 'all' });
  assert.deepEqual(normaliseSaved({ name: 'c', q: 'age:0..17' }, P), { name: 'c', q: 'age:0..17', mod: null });
  assert.deepEqual(normaliseSaved({ name: 'd', q: 'modality:CR' }, P), { name: 'd', q: 'modality:CR', mod: 'DX' });
  // a shared view saved before mod existed: value.mod reads as undefined
  assert.deepEqual(normaliseSaved({ name: 'f', q: 'modality:CT date:90d', mod: undefined }, P), { name: 'f', q: 'date:90d', mod: 'CT' });
  const kept = { name: 'e', q: 'x', mod: null };
  assert.equal(normaliseSaved(kept, P), kept);
});

test('scopeWarnings explains an empty list without changing the query', () => {
  const devs = new Map([[1, { display_name: 'CT 1', model: 'X', station: 'S1', modality: 'CT' }],
    [2, { display_name: 'Room 2', model: 'Y', station: 'S2', modality: 'DX' }]]);
  const w = (q, mod, o) => scopeWarnings(parse(q), mod, P, devs, o);
  assert.deepEqual(w('age:>=18 dlp:>500', 'DX'), [null, 'only for CT, NM, PT: nothing on DX has it']);
  assert.deepEqual(w('~ctdi:>5', 'DX'), [null]);
  assert.deepEqual(w('dlp:>500', null), [null]);
  assert.match(w('device:"CT 1"', 'DX')[0], /no DX device matches \(CT only\)/);
  assert.deepEqual(w('device:"CT 1","Room 2"', 'DX'), [null]);
  assert.deepEqual(w('device:nowhere', 'DX'), [null]);
  assert.match(w('modality:CT modality:DX', null)[1], /exclude each other/);
  assert.deepEqual(w('modality:CT,DX modality:DX', null), [null, null]);
  assert.match(w('modality:CT date:7d', null, { tabsOwn: true })[0], /tabs choose/);
});

test('panelWarnings names the panels a filter empties', () => {
  const w = (q, codes) => panelWarnings(parse(q), codes, P);
  assert.deepEqual(w('dlp:>500 date:30d', ['CT', 'NM']), [null, null]);
  assert.deepEqual(w('dlp:>500', ['CT', 'DX']), ['the DX panel comes out empty with it']);
  assert.deepEqual(w('modality:CT', ['CT', 'DX', 'RF']), ['the DX, RF panels come out empty with it']);
  assert.deepEqual(w('~modality:CT -modality:DX', ['CT', 'DX']), [null, null]);
  assert.deepEqual(groupsOfModalities('DX,CR', P), ['DX']);
  assert.deepEqual(groupsOfModalities('CT', P), ['CT']);
});
