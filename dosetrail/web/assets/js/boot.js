// Classic (non-module) script loaded in <head>: applies the stored theme and language
// before the first paint, so dark-theme users never see a light flash.
(function () {
  var p = {};
  try { p = JSON.parse(localStorage.getItem('dosetrail.prefs') || '{}'); } catch (e) { /* ignore */ }
  document.documentElement.dataset.theme = p.theme === 'light' ? 'light' : 'dark';
  document.documentElement.lang = p.lang === 'ro' ? 'ro' : 'en';
}());
