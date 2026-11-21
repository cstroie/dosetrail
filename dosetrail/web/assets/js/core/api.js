// Data access. Pages call these functions only, never fetch() directly.
//
// Mode is chosen by <html data-api="...">:
//   "sample" (default) → static JSON in data/sample/, filtered and paged in the browser;
//   anything else      → treated as the base URL of the DoseTrail REST API (planned endpoints
//                        are noted per function; the response shapes are the same).

import { parse, matchStudy } from './query.js';

const MODE = document.documentElement.dataset.api || 'sample';
const SAMPLE = new URL('../../../data/sample/', import.meta.url);
const cache = new Map();

async function getJSON(url) {
  if (!cache.has(url)) {
    cache.set(url, fetch(url, { headers: { Accept: 'application/json' } }).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${url}`);
      return r.json();
    }));
  }
  try {
    return await cache.get(url);
  } catch (e) {
    cache.delete(url);
    throw e;
  }
}

const sample = (name) => getJSON(new URL(name, SAMPLE).href);
const remote = (path, params = {}) => {
  const u = new URL(path, MODE);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') u.searchParams.set(k, v);
  return getJSON(u.href);
};

export const isSample = MODE === 'sample';

/** Writes (API mode only). Every successful write drops the read cache. */
async function send(method, path, body) {
  const r = await fetch(new URL(path, MODE).href, {
    method, headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  cache.clear();
  return data;
}

/** PATCH /api/devices/{id}  {display_name?, modality_override? ('auto' clears), origin?} */
export const updateDevice = (id, patch) => send('PATCH', `/api/devices/${id}`, patch);
/** POST /api/devices/merge  {record_ids, survivor_id, display_name?} */
export const mergeDevices = (recordIds, survivorId, displayName) =>
  send('POST', '/api/devices/merge', { record_ids: recordIds, survivor_id: survivorId, display_name: displayName });
/** POST /api/devices/{id}/split */
export const splitDevice = (id) => send('POST', `/api/devices/${id}/split`);
/** POST /api/devices/merge-suggestions/dismiss  {record_ids} */
export const dismissMerge = (recordIds) => send('POST', '/api/devices/merge-suggestions/dismiss', { record_ids: recordIds });

/** "Now" for relative times: the sample data was generated at a fixed moment. */
export async function now() {
  if (!isSample) return new Date().toISOString().slice(0, 16);
  return (await sample('overview.json')).generated;
}

/** GET /api/overview */
export async function overview() {
  return isSample ? sample('overview.json') : remote('/api/overview');
}

/** GET /api/devices → { records: [...], merge_suggestions: [...] } */
export async function devices() {
  return isSample ? sample('devices.json') : remote('/api/devices');
}

/** Map of physical device id → canonical record (aliases folded in). */
export async function deviceMap() {
  const { records } = await devices();
  const byId = new Map(records.map((r) => [r.id, r]));
  const out = new Map();
  for (const r of records) out.set(r.id, byId.get(r.physical_id) || r);
  return out;
}

export async function physicalDevices() {
  const { records } = await devices();
  return records.filter((r) => r.id === r.physical_id);
}

/** GET /api/drl → local DRL set */
export async function drl() {
  return isSample ? sample('drl.json') : remote('/api/drl');
}

const SORTERS = {
  date: (s) => s.date,
  device: (s, ctx) => ctx.devices.get(s.device_id)?.display_name || '',
  accession: (s) => s.accession,
  patient: (s) => s.patient.age ?? -1,
  description: (s) => s.description,
  events: (s) => s.events.spiral + s.events.axial + s.events.localizer,
  dlp_total: (s) => s.dlp_total,
  ctdivol_max: (s) => s.ctdivol_max,
};

/**
 * GET /api/studies?q=&sort=&dir=&page=&size=
 * → { total, rows: [study + device_name] }
 */
export async function studies({ q = '', sort = 'date', dir = 'desc', page = 1, size = 50 } = {}) {
  if (!isSample) return remote('/api/studies', { q, sort, dir, page, size });
  const [all, devs, n] = await Promise.all([sample('ct-studies.json'), deviceMap(), now()]);
  const tokens = parse(q);
  const ctx = { devices: devs, now: n };
  const rows = all.filter((s) => matchStudy(s, tokens, ctx));
  const key = SORTERS[sort] || SORTERS.date;
  const sgn = dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const x = key(a, ctx);
    const y = key(b, ctx);
    return (x < y ? -1 : x > y ? 1 : 0) * sgn;
  });
  const start = (page - 1) * size;
  return {
    total: rows.length,
    rows: rows.slice(start, start + size).map((s) => ({ ...s, device_name: devs.get(s.device_id)?.display_name || '—' })),
  };
}

/**
 * GET /api/studies/{id}. The API addresses studies by internal id: accession numbers can be
 * missing, hashed or shared. Sample mode keeps using the accession (one XA showcase).
 */
export async function study(ref) {
  if (!isSample) return remote(`/api/studies/${encodeURIComponent(ref)}`);
  return sample('study-A2026112100123.json');
}

/** Link to a study's detail page: by id when the API provides one, else by accession. */
export function studyHref(s) {
  return s.id != null ? `study.html?id=${s.id}` : `study.html?acc=${encodeURIComponent(s.accession)}`;
}

/** Quick lookup for the command palette and the overview: accession prefix. */
export async function lookup(text, limit = 6) {
  const t = text.trim().toUpperCase();
  if (t.length < 3) return [];
  if (!isSample) return remote('/api/lookup', { q: t, limit });
  const [all, devs, xa] = await Promise.all([sample('ct-studies.json'), deviceMap(), study('A2026112100123')]);
  const list = [{ accession: xa.accession, modality: 'XA', description: xa.description, device_name: xa.device.name, date: xa.start, dose: `${xa.totals.dap_gycm2} Gy·cm²` }];
  for (const s of all) list.push({ accession: s.accession, modality: 'CT', description: s.description, device_name: devs.get(s.device_id)?.display_name, date: s.date, dose: `${s.dlp_total} mGy·cm` });
  return list.filter((s) => s.accession.startsWith(t)).slice(0, limit);
}

/**
 * CT acquisitions for statistics.
 * GET /api/stats/ct/acquisitions?q= (the real API will return aggregates; sample returns rows)
 * → [{protocol, device_id, device, ctdivol, dlp, weight, time}]
 */
export async function ctAcquisitions(q = '') {
  if (!isSample) return remote('/api/stats/ct/acquisitions', { q });
  const [rows, devs, n] = await Promise.all([sample('ct-acquisitions.json'), deviceMap(), now()]);
  const tokens = parse(q).filter((t) => ['date', 'device', 'proto', 'origin', 'weight'].includes(t.key));
  const ctx = { devices: devs, now: n };
  return rows
    .map((r) => ({ protocol: r.p, device_id: r.d, device: devs.get(r.d)?.display_name, ctdivol: r.c, dlp: r.l, weight: r.w, time: r.t }))
    .filter((r) => matchStudy({
      description: '', protocol: r.protocol, date: r.time, device_id: r.device_id, test: false, flags: [],
      patient: { age: null, weight: r.weight }, events: { spiral: 1, axial: 0, localizer: 0 }, dlp_total: r.dlp, ctdivol_max: r.ctdivol,
    }, tokens, ctx));
}

/* ---------------- statistics, reference levels, exam classification (server only) ---------------- */

function serverOnly(what) {
  return Promise.reject(new Error(`${what} needs the DoseTrail server (sample mode has no data for it)`));
}

/**
 * GET /api/stats?q=&metric=&by=&system=&exam=&age_scheme=&weight_scheme=&bins=&min_n=
 * → { metric, unit, label, level, group_by, n, n_studies, overall, bins,
 *     groups: [{ key, label, n, n_studies, mean, sd, min, p25, median, p75, max, hist? }] }
 */
export function stats(params = {}) {
  return isSample ? serverOnly('Statistics') : remote('/api/stats', params);
}

/** GET /api/drl/sets → [{ key, name, source, notes, values }] */
export function drlSets() {
  return isSample ? serverOnly('Reference levels') : remote('/api/drl/sets');
}

/**
 * GET /api/drl/compare?q=&set=&by=facility|device&min_n=
 * → { set, name, source, notes, by, min_n, rows: [{ exam, exam_meaning, band, band_label,
 *     quantity, applies_to, reference, unit, status, reason?, groups: [{ device, device_name,
 *     n, median, p25, p75, mean, ratio, status }] }] }
 */
export function drlCompare(params = {}) {
  return isSample ? serverOnly('Reference levels') : remote('/api/drl/compare', params);
}

/** GET /api/classification/summary → [{ family, modalities, studies, classified, share }] */
export function classificationSummary() {
  return isSample ? serverOnly('Exam mapping') : remote('/api/classification/summary');
}

/** GET /api/classification/unclassified?family=rx|rf|ct&limit= → [{ texts, exposures?, studies }] */
export function unclassified(family, limit = 100) {
  return isSample ? serverOnly('Exam mapping') : remote('/api/classification/unclassified', { family, limit });
}

/** GET /api/classification/mappings → [{ id, family, field, text, system, code, meaning }] */
export function mappings() {
  return isSample ? serverOnly('Exam mapping') : remote('/api/classification/mappings');
}

/** GET /api/codes/{system} → [{ code, meaning, chapter, modality }] */
export function codes(system) {
  return isSample ? serverOnly('Exam mapping') : remote(`/api/codes/${encodeURIComponent(system)}`);
}

/** PUT /api/classification/mappings {family, field, text, code|null} (admin) */
export const decideMapping = (family, field, text, code) =>
  send('PUT', '/api/classification/mappings', { family, field, text, code });

/**
 * Download link for GET /api/export?q=&level=study|event&format=csv|xlsx&pid=
 * (exporter or admin role; pid=1 needs the pid role; every export is audit-logged).
 */
export function exportUrl({ q = '', level = 'study', format = 'csv', pid = false } = {}) {
  if (isSample) return null;
  const u = new URL('/api/export', MODE);
  u.searchParams.set('q', q);
  u.searchParams.set('level', level);
  u.searchParams.set('format', format);
  if (pid) u.searchParams.set('pid', '1');
  return u.href;
}

/** Download links: GET /api/drl/summary (ICRP 135) and /api/drl/dataset (exporter role, audited). */
export function drlDownloadUrl(kind, { q = '', set = '', min_n = 20, format = 'xlsx' } = {}) {
  if (isSample) return null;
  const u = new URL(kind === 'dataset' ? '/api/drl/dataset' : '/api/drl/summary', MODE);
  u.searchParams.set('q', q);
  u.searchParams.set('format', format);
  if (kind !== 'dataset') { u.searchParams.set('set', set); u.searchParams.set('min_n', String(min_n)); }
  return u.href;
}

/** GET /api/reports/insp1255?year=&quarter=&q= → { period, notes, count_bands, mean_bands, sheets } */
export function inspReport(params) {
  return isSample ? serverOnly('Reports') : remote('/api/reports/insp1255', params);
}

/** Download link for the same report as XLSX (official column order). */
export function inspReportUrl({ year, quarter, q = 'origin:local' }) {
  if (isSample) return null;
  const u = new URL('/api/reports/insp1255', MODE);
  for (const [k, v] of Object.entries({ year, quarter, q, format: 'xlsx' })) u.searchParams.set(k, String(v));
  return u.href;
}

/** Download link: GET /api/survey/export?month=YYYY-MM&format= (anonymised paediatric survey). */
export function surveyUrl({ month, format = 'csv', q = 'origin:local' }) {
  if (isSample) return null;
  const u = new URL('/api/survey/export', MODE);
  for (const [k, v] of Object.entries({ month, format, q })) u.searchParams.set(k, v);
  return u.href;
}

/** GET /api/quality?q=&limit= → { studies, rules: {rule: {severity, title, count}}, devices, flagged } */
export function quality(params = {}) {
  return isSample ? serverOnly('Data quality') : remote('/api/quality', params);
}

/** GET /api/alerts?status=open|acknowledged|all&limit= (same shape as overview.alerts) */
export function alerts(status = 'open', limit = 50) {
  return isSample ? sample('overview.json').then((o) => o.alerts) : remote('/api/alerts', { status, limit });
}
/** POST /api/alerts/{id}/ack {note} (audit-logged) */
export const ackAlert = (id, note) => send('POST', `/api/alerts/${id}/ack`, { note });
/** GET /api/alert-rules · PATCH /api/alert-rules/{id} (admin) */
export function alertRules() {
  return isSample ? serverOnly('Alert rules') : remote('/api/alert-rules');
}
export const updateAlertRule = (id, patch) => send('PATCH', `/api/alert-rules/${id}`, patch);
