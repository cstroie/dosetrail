// Number, unit and time formatting. Values are stored in fixed units
// (DAP Gy·cm², DLP mGy·cm, CTDIvol mGy, Ka,r mGy/Gy) and converted only here, at display time.

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
  return prefs.get('dapUnit') in DAP_UNITS ? prefs.get('dapUnit') : 'Gy·cm²';
}

/** DAP stored in Gy·cm², shown in the user's unit. `extraDigits` for per-event values. */
export function dap(gycm2, extraDigits = 0) {
  const u = DAP_UNITS[dapUnit()];
  return num(gycm2 * u.factor, Math.max(0, u.digits + extraDigits));
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
