// <dt-command-palette> — Ctrl K. Finds studies by accession and runs commands
// (navigation, theme, language, DAP unit). Native <dialog>, so focus is trapped and Esc closes.

import { h, clear } from '../core/dom.js';
import { lookup, studyHref } from '../core/api.js';
import { prefs } from '../core/prefs.js';
import { lang } from '../core/i18n.js';

const COMMANDS = [
  { group: 'go', label: 'Overview', keys: 'G O', run: () => { location.href = 'index.html'; } },
  { group: 'go', label: 'Studies · CT', keys: 'G S', run: () => { location.href = 'studies.html'; } },
  { group: 'go', label: 'Charts · workspace', keys: 'G C', run: () => { location.href = 'charts.html'; } },
  { group: 'go', label: 'Admin · devices', keys: 'G A', run: () => { location.href = 'devices.html'; } },
  { group: 'filter', label: 'Studies above DRL, last 30 days', run: () => { location.href = 'studies.html?q=' + encodeURIComponent('flag:drl date:30d -phantom'); } },
  { group: 'theme', label: 'Toggle light / dark theme', run: async () => (await import('./dt-shell.js')).toggleTheme() },
  { group: 'lang', label: 'Limba: română / Language: English', run: () => { prefs.set('lang', lang() === 'en' ? 'ro' : 'en'); location.reload(); } },
  { group: 'unit', label: 'DAP unit: Gy·cm²', run: () => setUnit('Gy·cm²') },
  { group: 'unit', label: 'DAP unit: mGy·cm²', run: () => setUnit('mGy·cm²') },
  { group: 'unit', label: 'DAP unit: µGy·m²', run: () => setUnit('µGy·m²') },
];

function setUnit(u) {
  prefs.set('dapUnit', u);
  document.dispatchEvent(new CustomEvent('dt-unit-change', { detail: u }));
}

class DtCommandPalette extends HTMLElement {
  connectedCallback() {
    if (this.dialog) return;
    this.input = h('input', { class: 'pal-input mono', type: 'text', role: 'combobox', 'aria-expanded': 'true',
      'aria-controls': 'pal-list', 'aria-autocomplete': 'list', 'aria-label': 'Search or command', placeholder: 'A2026… or a command',
      oninput: () => this.refresh(), onkeydown: (e) => this.onKey(e) });
    this.list = h('ul', { id: 'pal-list', class: 'pal-list', role: 'listbox', 'aria-label': 'Results' });
    this.dialog = h('dialog', { class: 'palette', 'aria-label': 'Command palette', onclick: (e) => { if (e.target === this.dialog) this.dialog.close(); } },
      h('div', { class: 'pal-head' }, h('span', { class: 'muted', 'aria-hidden': 'true' }, '›'), this.input, h('kbd', {}, 'Esc')),
      this.list);
    this.append(this.dialog);
    this.items = [];
    this.active = 0;
  }

  open() {
    this.input.value = '';
    this.dialog.showModal();
    this.input.focus();
    this.refresh();
  }

  async refresh() {
    const q = this.input.value.trim();
    const seq = (this._seq = (this._seq || 0) + 1);
    const studies = await lookup(q).catch(() => []);
    if (seq !== this._seq) return; // a newer keystroke won
    const cmds = COMMANDS.filter((c) => !q || c.label.toLowerCase().includes(q.toLowerCase()));
    this.items = [
      ...studies.map((s) => ({ group: 'studies', kind: s.modality, label: `${s.accession} · ${s.description}`, extra: s.dose,
        run: () => { location.href = s.id != null || s.modality === 'XA' ? studyHref(s) : `studies.html?q=${encodeURIComponent('acc:' + s.accession)}`; } })),
      ...cmds.map((c) => ({ ...c, kind: c.group, extra: c.keys || '' })),
    ];
    if (/^[A-Z]?\d{4,}/i.test(q)) {
      this.items.push({ group: 'filter', kind: 'filter', label: `Filter Studies by accession ${q.toUpperCase()}*`, extra: '',
        run: () => { location.href = 'studies.html?q=' + encodeURIComponent(`acc:${q.toUpperCase()}*`); } });
    }
    this.active = 0;
    this.render();
  }

  render() {
    clear(this.list);
    let lastGroup = '';
    this.items.forEach((it, i) => {
      if (it.group !== lastGroup) {
        lastGroup = it.group;
        this.list.append(h('li', { class: 'pal-group', role: 'presentation' }, it.group === 'studies' ? 'Studies' : it.group === 'go' ? 'Go to' : 'Commands'));
      }
      this.list.append(h('li', { id: `pal-${i}`, role: 'option', class: 'pal-item', 'aria-selected': i === this.active ? 'true' : 'false',
        onclick: () => this.choose(i), onmousemove: () => { if (this.active !== i) { this.active = i; this.mark(); } } },
      h('span', { class: `tag ${it.group === 'studies' ? 'info' : ''}` }, it.kind),
      h('span', { class: 'pal-label' }, it.label),
      h('span', { class: 'muted mono' }, it.extra || '')));
    });
    if (!this.items.length) this.list.append(h('li', { class: 'pal-empty muted', role: 'presentation' }, 'No match. Type at least 3 characters of an accession.'));
    this.mark();
  }

  mark() {
    this.list.querySelectorAll('[role=option]').forEach((li) => li.setAttribute('aria-selected', li.id === `pal-${this.active}` ? 'true' : 'false'));
    this.input.setAttribute('aria-activedescendant', `pal-${this.active}`);
    this.list.querySelector(`#pal-${this.active}`)?.scrollIntoView({ block: 'nearest' });
  }

  onKey(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); this.active = Math.min(this.items.length - 1, this.active + 1); this.mark(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this.active = Math.max(0, this.active - 1); this.mark(); }
    else if (e.key === 'Enter') { e.preventDefault(); this.choose(this.active); }
  }

  choose(i) {
    const it = this.items[i];
    if (!it) return;
    this.dialog.close();
    it.run();
  }
}

customElements.define('dt-command-palette', DtCommandPalette);
