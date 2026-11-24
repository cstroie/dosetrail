// Number, unit and time formatting. Values are stored in fixed units
// (DAP Gy·cm², DLP mGy·cm, CTDIvol mGy, Ka,r mGy/Gy) and converted only here, at display time.
// Shown in mGy-based units by default: DAP mGy·cm² (the user may switch), Ka,r always mGy.

import { locale } from './i18n.js';
import { prefs } from './prefs.js';

export const DAP_UNITS = {
  'Gy·cm²': { factor: 1, digits: 1 },
  'mGy·cm²': { factor: 1000, digits: 0 },
  'µGy·m²': { factor: 100, digits: 0 },
};

export function num(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat(locale(), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

/** Up to `sig` significant decimals, trimming zeros: 0.13, 8.1, 54.2, 1,412 */
export function smart(value) {
  if (value === null || value === undefined) return '—';
  const a = Math.abs(value);
  const digits = a >= 100 ? 0 : a >= 10 ? 1 : 2;
  return num(value, digits);
}

export function dapUnit() {
  return prefs.get('dapUnit') in DAP_UNITS ? prefs.get('dapUnit') : 'mGy·cm²';
}

/** DAP stored in Gy·cm², shown in the user's unit. Totals use the unit's fixed digits;
 *  per-event values (`extraDigits` > 0) and anything under 10 keep significant decimals. */
export function dap(gycm2, extraDigits = 0) {
  if (gycm2 === null || gycm2 === undefined) return '—';
  const u = DAP_UNITS[dapUnit()];
  const v = gycm2 * u.factor;
  return extraDigits > 0 || Math.abs(v) < 10 ? smart(v) : num(v, u.digits);
}

// unit codes as the server sends them (DRL sets, statistics) → Gy·cm² factor
const TO_GYCM2 = { 'Gy.cm2': 1, 'dGy.cm2': 0.1, 'cGy.cm2': 0.01, 'mGy.cm2': 0.001, 'µGy.m2': 0.01, 'uGy.m2': 0.01, 'Gy.m2': 1e4 };
const LABELS = { 'mGy.cm': 'mGy·cm', 'mA.s': 'mAs', 1: '' };

/** How a value stored in `code` is shown: DAP in the user's DAP unit, Gy as mGy, the rest as is.
 *  → { f: multiply the stored value by this, unit: label to print } */
export function shownUnit(code) {
  if (code === 'Gy') return { f: 1000, unit: 'mGy' };
  if (code in TO_GYCM2) return { f: TO_GYCM2[code] * DAP_UNITS[dapUnit()].factor, unit: dapUnit() };
  return { f: 1, unit: LABELS[code] ?? code ?? '' };
}

/** A value stored in `code`, in the shown unit, without the unit: "284,600", "0.52". */
export function shown(value, code) {
  return value === null || value === undefined ? '—' : smart(value * shownUnit(code).f);
}

/** Air kerma (Ka,r, skin dose) in mGy: whole mGy from 10 up, decimals below. */
export function mgy(value) {
  if (value === null || value === undefined) return '—';
  return Math.abs(value) >= 10 ? num(value, 0) : smart(value);
}

export function mmss(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function dateTime(iso) {
  return iso ? iso.replace('T', ' ').slice(0, 16) : '—';
}

export function shortDateTime(iso) {
  return iso ? iso.slice(5, 16).replace('T', ' ') : '—';
}

export function clock(iso) {
  return iso ? iso.slice(11, 19) : '—';
}

/** "12 min", "3 h", "2 d" relative to `now` (ISO strings). */
export function ago(iso, nowIso) {
  const ms = new Date(nowIso) - new Date(iso);
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min} min`;
  const hours = Math.round(min / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.floor(hours / 24)} d`;
}

export function patientSummary(p) {
  if (!p) return '—';
  const parts = [p.age !== null && p.age !== undefined ? `${p.age} y` : '—', p.sex || '—'];
  parts.push(p.weight ? `${p.weight} kg` : '— kg');
  return parts.join(' · ');
}
