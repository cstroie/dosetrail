// Classic (non-module) script loaded in <head>: applies the stored theme and language
// before the first paint, so dark-theme users never see a light flash.
(function () {
  var p = {};
  try { p = JSON.parse(localStorage.getItem('dozimetrix.prefs') || localStorage.getItem('dosimetrix.prefs') || '{}'); } catch (e) { /* ignore; 'dosimetrix.prefs' is the key before the rename */ }
  document.documentElement.dataset.theme = p.theme === 'light' ? 'light' : 'dark';
  // Film is the default; a palette stored before it became the default (no paletteV 2) is ignored
  document.documentElement.dataset.palette = p.paletteV === 2 && /^(iodine|cherenkov|phosphor|film)$/.test(p.palette || '') ? p.palette : 'film';
  document.documentElement.lang = p.lang === 'ro' ? 'ro' : 'en';
}());
