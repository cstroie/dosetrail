// The typed query language used by the query bar, the URL (?q=) and saved queries.
//
//   modality:CT date:2026-09-01..2026-11-21 device:"CT 1 – Emergency","CT 2 – Radiology"
//   age:>=18 dlp:>700 desc:head -phantom
//
// key:value · comparisons > >= < <= · ranges a..b · comma lists · "quoted values" ·
// a leading "-" negates · a bare word searches the description.
// The server will receive the same string; in sample mode `matchStudy` filters locally.

export const KEYS = {
  modality: { label: 'modality', kind: 'enum', values: ['CT', 'DX', 'XA', 'RF', 'MG', 'NM', 'PT'], hint: 'CT, DX, XA, RF, MG, NM, PT (PT = PET/CT)' },
  date: { label: 'date', kind: 'date', hint: 'YYYY-MM-DD..YYYY-MM-DD, or 7d / 30d / 90d' },
  device: { label: 'device', kind: 'text', hint: 'display name, model or station; comma-separated' },
  desc: { label: 'desc', kind: 'text', hint: 'study description contains' },
  proto: { label: 'proto', kind: 'text', hint: 'acquisition protocol contains' },
  age: { label: 'age', kind: 'number', hint: 'years, e.g. >=18 or 0..17' },
  weight: { label: 'weight', kind: 'number', hint: 'kg, e.g. 50..90' },
  acc: { label: 'acc', kind: 'text', hint: 'accession number (prefix with *)' },
  pid: { label: 'pid', kind: 'text', hint: 'patient ID (restricted, audit-logged)' },
  dlp: { label: 'dlp', kind: 'number', hint: 'total DLP, mGy·cm' },
  ctdi: { label: 'ctdi', kind: 'number', hint: 'max CTDIvol, mGy' },
  spiral: { label: 'spiral', kind: 'number', hint: 'number of spiral events' },
  axial: { label: 'axial', kind: 'number', hint: 'number of axial events' },
  loc: { label: 'loc', kind: 'number', hint: 'number of localisers' },
  origin: { label: 'origin', kind: 'enum', values: ['local', 'external', 'any'], hint: 'local, external, any' },
  phantom: { label: 'phantom', kind: 'flag', hint: 'test / phantom studies; use -phantom to exclude' },
  flag: { label: 'flag', kind: 'enum', values: ['drl', 'alert', 'any'], hint: 'studies above DRL or with an alert' },
  ind: { label: 'indication', kind: 'text', hint: 'clinical indication codes, e.g. PED-TCE,TRM-TCE or TRM-* (a chapter)' },
};

// Romanian aliases are accepted on input and normalised to the English key.
const ALIASES = { aparat: 'device', 'vârstă': 'age', varsta: 'age', greutate: 'weight', data: 'date', descriere: 'desc', protocol: 'proto', fantoma: 'phantom', 'fantomă': 'phantom', modalitate: 'modality', origine: 'origin', indicatie: 'ind', 'indicație': 'ind' };

/** Split respecting double quotes. */
function lex(text) {
  const out = [];
  let cur = '';
  let quoted = false;
  for (const ch of text) {
    if (ch === '"') { quoted = !quoted; cur += ch; continue; }
    if (/\s/.test(ch) && !quoted) { if (cur) out.push(cur); cur = ''; continue; }
    cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

function splitList(v) {
  const parts = [];
  let cur = '';
  let quoted = false;
  for (const ch of v) {
    if (ch === '"') { quoted = !quoted; continue; }
    if (ch === ',' && !quoted) { parts.push(cur); cur = ''; continue; }
    cur += ch;
  }
  parts.push(cur);
  return parts.map((p) => p.trim()).filter(Boolean);
}

/** Parse a query string into tokens: {key, value, neg, error?}. */
export function parse(text) {
  return lex(text || '').map(parseOne);
}

// A leading "~" switches a filter off without losing it: "~modality:CT". It stays in the
// URL and in saved queries; `active()` drops it before the query is applied or sent.
function parseOne(raw) {
  const off = raw.startsWith('~') && raw.length > 1;
  const tok = parseBody(off ? raw.slice(1) : raw);
  if (off) tok.off = true;
  return tok;
}

function parseBody(raw) {
  {
    let neg = false;
    let body = raw;
    if (body.startsWith('-') && body.length > 1) { neg = true; body = body.slice(1); }
    const i = body.indexOf(':');
    if (i < 0) {
      const k = ALIASES[body.toLowerCase()] || body.toLowerCase();
      if (KEYS[k]?.kind === 'flag') return { key: k, value: '', neg };
      return { key: 'text', value: body.replace(/"/g, ''), neg };
    }
    const rawKey = body.slice(0, i).toLowerCase();
    const key = ALIASES[rawKey] || rawKey;
    const value = body.slice(i + 1);
    const tok = { key, value, neg };
    if (!KEYS[key]) tok.error = `unknown key “${rawKey}”`;
    else if (KEYS[key].kind === 'number' && !parseNumberCond(value)) tok.error = 'expected a number, >n, <=n or a..b';
    else if (KEYS[key].kind === 'date' && !parseDateCond(value)) tok.error = 'expected YYYY-MM-DD..YYYY-MM-DD or 30d';
    return tok;
  }
}

function quote(v) {
  return /[\s,]/.test(v) ? `"${v}"` : v;
}

export function tokenToString(t) {
  const neg = (t.off ? '~' : '') + (t.neg ? '-' : '');
  if (t.key === 'text') return neg + quote(t.value);
  if (KEYS[t.key]?.kind === 'flag') return neg + t.key;
  const v = KEYS[t.key]?.kind === 'text' ? splitList(t.value).map(quote).join(',') : t.value;
  return `${neg}${t.key}:${v}`;
}

/** The query as it applies: switched-off ("~") filters left out. Use before sending a query. */
export function active(q) {
  return typeof q === 'string' && q.includes('~') ? serialize(parse(q).filter((t) => !t.off)) : q;
}
export function serialize(tokens) {
  return tokens.map(tokenToString).join(' ');
}

/** Value condition for numbers: returns predicate or null. */
export function parseNumberCond(v) {
  let m = /^(>=|<=|>|<)?(-?\d+(?:\.\d+)?)$/.exec(v);
  if (m) {
    const n = Number(m[2]);
    switch (m[1]) {
      case '>=': return (x) => x >= n;
      case '<=': return (x) => x <= n;
      case '>': return (x) => x > n;
      case '<': return (x) => x < n;
      default: return (x) => x === n;
    }
  }
  m = /^(-?\d+(?:\.\d+)?)?\.\.(-?\d+(?:\.\d+)?)?$/.exec(v);
  if (m && (m[1] || m[2])) {
    const lo = m[1] ? Number(m[1]) : -Infinity;
    const hi = m[2] ? Number(m[2]) : Infinity;
    return (x) => x >= lo && x <= hi;
  }
  return null;
}

/** Date condition: returns {from, to} ISO date strings or null. `now` is an ISO string. */
export function parseDateCond(v, now = new Date().toISOString()) {
  let m = /^(\d+)d$/.exec(v);
  if (m) {
    const to = new Date(now);
    const from = new Date(to.getTime() - Number(m[1]) * 86400000);
    return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) };
  }
  m = /^(\d{4}-\d{2}-\d{2})?\.\.(\d{4}-\d{2}-\d{2})?$/.exec(v);
  if (m && (m[1] || m[2])) return { from: m[1] || '0000-01-01', to: m[2] || '9999-12-31' };
  m = /^(\d{4}-\d{2}-\d{2})$/.exec(v);
  if (m) return { from: m[1], to: m[1] };
  return null;
}

/**
 * Local filter used in sample mode (the real API applies the same rules server-side).
 * `ctx` = { devices: Map(id → record), now: ISO string }
 */
export function matchStudy(s, tokens, ctx) {
  tokens = tokens.filter((t) => !t.off);
  const dev = ctx.devices.get(s.device_id);
  let phantomAllowed = false;
  for (const t of tokens) {
    if (t.error) continue;
    let ok = true;
    const v = t.value;
    switch (t.key) {
      case 'text': ok = s.description.toLowerCase().includes(v.toLowerCase()); break;
      case 'modality': ok = splitList(v).some((m) => m.toUpperCase() === (dev?.modality || 'CT')); break;
      case 'date': {
        const r = parseDateCond(v, ctx.now);
        const d = s.date.slice(0, 10);
        ok = !!r && d >= r.from && d <= r.to;
        break;
      }
      case 'device': {
        const hay = dev ? `${dev.display_name} ${dev.model} ${dev.station}`.toLowerCase() : '';
        ok = splitList(v).some((x) => hay.includes(x.toLowerCase()));
        break;
      }
      case 'desc': ok = s.description.toLowerCase().includes(v.toLowerCase()); break;
      case 'proto': ok = (s.protocol || '').toLowerCase().includes(v.toLowerCase()); break;
      case 'acc': ok = v.endsWith('*') ? s.accession.startsWith(v.slice(0, -1)) : s.accession === v; break;
      case 'pid': ok = s.patient.pseudo_id === v; break;
      case 'age': ok = s.patient.age !== null && parseNumberCond(v)(s.patient.age); break;
      case 'weight': ok = !!s.patient.weight && parseNumberCond(v)(s.patient.weight); break;
      case 'dlp': ok = parseNumberCond(v)(s.dlp_total); break;
      case 'ctdi': ok = parseNumberCond(v)(s.ctdivol_max); break;
      case 'spiral': ok = parseNumberCond(v)(s.events.spiral); break;
      case 'axial': ok = parseNumberCond(v)(s.events.axial); break;
      case 'loc': ok = parseNumberCond(v)(s.events.localizer); break;
      case 'origin': ok = v === 'any' || (dev?.origin || 'local') === v; break;
      case 'phantom': if (!t.neg) phantomAllowed = true; ok = t.neg ? !s.test : s.test; break;
      case 'ind': ok = splitList(v).some((x) => { const c = (s.indication || '').toUpperCase(); const w = x.toUpperCase(); return w.endsWith('*') ? c.startsWith(w.slice(0, -1)) && !!c : c === w; }); break;
      case 'flag': ok = v === 'any' ? s.flags.length > 0 : s.flags.some((f) => (v === 'alert' ? f.level === 'alert' : f.kind === 'drl')); break;
      default: ok = true;
    }
    if (t.neg && t.key !== 'phantom') ok = !ok;
    if (!ok) return false;
  }
  // test / phantom studies are hidden unless asked for explicitly
  return phantomAllowed || !s.test;
}

/** Suggestions for the token being typed. Returns [{insert, label, hint}]. */
export function suggest(partial, ctx = {}) {
  const neg = partial.startsWith('-') ? '-' : '';
  const body = neg ? partial.slice(1) : partial;
  const i = body.indexOf(':');
  if (i < 0) {
    return Object.entries(KEYS)
      .filter(([k]) => k.startsWith(body.toLowerCase()))
      .map(([k, d]) => ({ insert: d.kind === 'flag' ? `${neg}${k}` : `${neg}${k}:`, label: d.kind === 'flag' ? k : `${k}:`, hint: d.hint }));
  }
  const key = ALIASES[body.slice(0, i).toLowerCase()] || body.slice(0, i).toLowerCase();
  const val = body.slice(i + 1);
  const def = KEYS[key];
  if (!def) return [];
  const opts = [];
  if (def.kind === 'enum') def.values.forEach((x) => opts.push({ v: x, hint: '' }));
  if (key === 'device' && ctx.devices) [...ctx.devices.values()].forEach((d) => opts.push({ v: quote(d.display_name), hint: `${d.manufacturer} ${d.model}` }));
  if (key === 'date') [['7d', 'last 7 days'], ['30d', 'last 30 days'], ['90d', 'last 90 days'], ['2026-07-01..2026-12-31', '2026 H2']].forEach(([v, h]) => opts.push({ v, hint: h }));
  if (key === 'dlp' && ctx.drl) Object.entries(ctx.drl).forEach(([p, v]) => opts.push({ v: `>${v}`, hint: `above local DRL, ${p}` }));
  if (key === 'age') [['>=18', 'adults'], ['0..17', 'paediatric']].forEach(([v, h]) => opts.push({ v, hint: h }));
  return opts
    .filter((o) => o.v.toLowerCase().replace(/"/g, '').startsWith(val.toLowerCase().replace(/"/g, '')))
    .slice(0, 8)
    .map((o) => ({ insert: `${neg}${key}:${o.v}`, label: `${key}:${o.v}`, hint: o.hint }));
}
