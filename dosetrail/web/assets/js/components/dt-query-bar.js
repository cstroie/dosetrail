// <dt-query-bar value="modality:CT date:90d -phantom"></dt-query-bar>
//
// Typed filter tokens with autocomplete. Committed tokens render as removable chips;
// the token being typed gets suggestions (Tab / Enter completes). Fires
// `query-change` with detail = the full query string whenever the tokens change.
// Set `.context = { devices: Map, drl: {protocol: value} }` to enrich suggestions.

import { h, clear } from '../core/dom.js';
import { parse, serialize, suggest, tokenToString, KEYS } from '../core/query.js';

class DtQueryBar extends HTMLElement {
  connectedCallback() {
    if (this.input) return;
    this.context = this.context || {};
    this.tokens = parse(this.getAttribute('value') || '');
    this.chips = h('span', { class: 'qb-chips' });
    this.input = h('input', { class: 'qb-input', type: 'text', role: 'combobox', 'aria-label': 'Add filter',
      'aria-expanded': 'false', 'aria-controls': 'qb-list', 'aria-autocomplete': 'list', spellcheck: 'false', autocomplete: 'off',
      placeholder: 'add filter…  e.g. dlp:>700',
      oninput: () => this.updateSuggestions(), onkeydown: (e) => this.onKey(e), onblur: () => setTimeout(() => this.hideList(), 150) });
    this.list = h('ul', { id: 'qb-list', class: 'qb-list', role: 'listbox', hidden: true });
    this.error = h('span', { class: 'qb-error', role: 'alert' });
    this.append(h('div', { class: 'qb', onclick: (e) => { if (e.target === e.currentTarget) this.input.focus(); } }, this.chips, this.input), this.list, this.error);
    this.renderChips();
  }

  get value() { return serialize(this.tokens); }
  set value(v) { this.tokens = parse(v); if (this.chips) this.renderChips(); }

  renderChips() {
    clear(this.chips);
    this.tokens.forEach((tok, i) => {
      const def = KEYS[tok.key];
      const key = tok.key === 'text' ? 'text' : (tok.neg ? '−' : '') + tok.key;
      const val = def?.kind === 'flag' ? '' : tok.value;
      this.chips.append(h('span', { class: `qb-tok ${tok.neg ? 'neg' : ''} ${tok.error ? 'bad' : ''}`, title: tok.error || def?.hint || '' },
        h('b', {}, key), val ? h('span', {}, val.replace(/"/g, '')) : null,
        h('button', { type: 'button', 'aria-label': `Remove ${tokenToString(tok)}`, onclick: () => this.remove(i) }, '×')));
    });
    const bad = this.tokens.find((x) => x.error);
    this.error.textContent = bad ? `${tokenToString(bad)}: ${bad.error}` : '';
  }

  remove(i) {
    this.tokens.splice(i, 1);
    this.renderChips();
    this.emit();
    this.input.focus();
  }

  commit(text) {
    const parsed = parse(text);
    if (!parsed.length) return;
    for (const tok of parsed) {
      // one token per key (except text and device lists): a new date replaces the old one
      const same = this.tokens.findIndex((x) => x.key === tok.key && x.key !== 'text' && x.neg === tok.neg);
      if (same >= 0) this.tokens[same] = tok; else this.tokens.push(tok);
    }
    this.input.value = '';
    this.renderChips();
    this.hideList();
    this.emit();
  }

  emit() {
    this.setAttribute('value', this.value);
    this.dispatchEvent(new CustomEvent('query-change', { detail: this.value, bubbles: true }));
  }

  updateSuggestions() {
    const text = this.input.value;
    this.items = text ? suggest(text, this.context) : [];
    this.active = 0;
    clear(this.list);
    this.items.forEach((s, i) => this.list.append(h('li', { id: `qb-${i}`, role: 'option', 'aria-selected': i === 0 ? 'true' : 'false',
      onmousedown: (e) => { e.preventDefault(); this.accept(i); } }, h('code', {}, s.label), h('span', { class: 'muted' }, s.hint || ''))));
    this.list.hidden = !this.items.length;
    this.input.setAttribute('aria-expanded', String(!this.list.hidden));
    if (this.items.length) this.input.setAttribute('aria-activedescendant', 'qb-0');
  }

  accept(i) {
    const s = this.items[i];
    if (!s) return;
    if (s.insert.endsWith(':')) { this.input.value = s.insert; this.updateSuggestions(); this.input.focus(); return; }
    this.commit(s.insert);
  }

  hideList() {
    if (!this.list) return;
    this.list.hidden = true;
    this.input.setAttribute('aria-expanded', 'false');
  }

  onKey(e) {
    const open = !this.list.hidden && this.items?.length;
    if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      this.active = (this.active + (e.key === 'ArrowDown' ? 1 : -1) + this.items.length) % this.items.length;
      this.list.querySelectorAll('li').forEach((li, i) => li.setAttribute('aria-selected', i === this.active ? 'true' : 'false'));
      this.input.setAttribute('aria-activedescendant', `qb-${this.active}`);
    } else if (e.key === 'Tab' && open) {
      e.preventDefault();
      this.accept(this.active);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const typed = this.input.value.trim();
      // Enter takes the highlighted suggestion only if the user moved to it; otherwise the typed text
      if (open && this.active > 0) this.accept(this.active);
      else if (typed) this.commit(typed);
      else this.emit();
    } else if (e.key === 'Backspace' && !this.input.value && this.tokens.length) {
      this.remove(this.tokens.length - 1);
    } else if (e.key === 'Escape') {
      this.hideList();
    }
  }
}

customElements.define('dt-query-bar', DtQueryBar);
