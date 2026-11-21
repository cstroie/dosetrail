// Per-browser preferences (theme, language, DAP unit, density, workspace layout).
// Stored in localStorage when available; everything still works without it.

const KEY = 'dosetrail.prefs';
const DEFAULTS = { theme: 'dark', lang: 'en', dapUnit: 'Gy·cm²', density: 'compact', origin: 'local' };

function load() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') }; } catch { return { ...DEFAULTS }; }
}

let state = load();
const listeners = new Set();

export const prefs = {
  get(name) { return state[name]; },
  set(name, value) {
    state = { ...state, [name]: value };
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* private mode: keep in memory */ }
    for (const fn of listeners) fn(name, value);
  },
  /** fn(name, value) is called after every change; returns an unsubscribe function. */
  subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
};

/** Apply theme and language to <html> before first paint (called from the page head). */
export function applyDocumentPrefs() {
  document.documentElement.dataset.theme = state.theme;
  document.documentElement.lang = state.lang;
}
