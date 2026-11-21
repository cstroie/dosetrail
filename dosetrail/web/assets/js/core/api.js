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
