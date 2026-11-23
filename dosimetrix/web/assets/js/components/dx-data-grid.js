// <dx-data-grid label="CT studies"> — dense, server-paged table.
//
//   grid.columns = [{ key, label, unit?, align?: 'right', render?(row) → Node|string, sortable?: true, hidden?: false }]
//   grid.fetcher = async ({ sort, dir, page, size }) => ({ total, rows })
//   grid.rowId   = (row) => row.accession
//   grid.reload()
//
// Children with slot="actions" are moved into the toolbar. Events: `row-focus` and
// `row-open` (detail = row), `state-change` (detail = {sort, dir, page, size}).
// Keyboard: ↑/↓ or J/K move the row focus, Enter opens. Column visibility and density persist.

import { h, clear } from '../core/dom.js';
import { num } from '../core/format.js';
import { prefs } from '../core/prefs.js';
import { t } from '../core/i18n.js';

class DtDataGrid extends HTMLElement {
  constructor() {
    super();
    this.columns = [];
    this.state = { sort: 'date', dir: 'desc', page: 1, size: 50 };
    this.rows = [];
    this.total = 0;
    this.focusIndex = 0;
    this.rowId = (r) => r.id;
  }

  connectedCallback() {
    if (this.table) return;
    const actions = [...this.querySelectorAll(':scope > [slot=actions]')];
    actions.forEach((a) => a.removeAttribute('slot'));
    const id = this.getAttribute('storage-key') || 'grid';
    this.hiddenCols = new Set(prefs.get(`${id}.hidden`) || []);
    this.storageId = id;

    this.countEl = h('strong', { class: 'mono' }, '…');
    this.colMenu = h('div', { class: 'menu', hidden: true, role: 'group', 'aria-label': t('studies.columns') });
    const colBtn = h('button', { class: 'btn', type: 'button', 'aria-expanded': 'false',
      onclick: () => { this.colMenu.hidden = !this.colMenu.hidden; colBtn.setAttribute('aria-expanded', String(!this.colMenu.hidden)); this.renderColMenu(); } },
    t('studies.columns'), ' ▾');
    const density = prefs.get('density');
    this.densitySeg = h('span', { class: 'seg', role: 'group', 'aria-label': t('studies.density') },
      ['compact', 'comfortable'].map((d) => h('button', { type: 'button', 'aria-pressed': String(d === density), dataset: { d },
        onclick: () => this.setDensity(d) }, d === 'compact' ? 'Compact' : 'Comfortable')));
    this.dataset.density = density;

    this.head = h('thead');
    this.body = h('tbody', { onkeydown: (e) => this.onKey(e) });
    this.table = h('table', { class: 'grid', 'aria-label': this.getAttribute('label') || '' }, this.head, this.body);
    this.status = h('div', { class: 'grid-status', role: 'status' });
    this.pageInfo = h('span', { class: 'mono' });
    this.prevBtn = h('button', { class: 'btn', type: 'button', 'aria-label': 'Previous page', title: 'Previous page', onclick: () => this.go(this.state.page - 1) }, '‹');
    this.nextBtn = h('button', { class: 'btn', type: 'button', 'aria-label': 'Next page', title: 'Next page', onclick: () => this.go(this.state.page + 1) }, '›');
    this.pageNo = h('span', { class: 'mono' });

    this.append(
      h('div', { class: 'panel-head' }, this.countEl, h('span', { class: 'muted' }, this.getAttribute('noun') || ''),
        h('span', { class: 'spacer' }), h('span', { class: 'menu-wrap' }, colBtn, this.colMenu), this.densitySeg, actions),
      h('div', { class: 'grid-scroll' }, this.table, this.status),
      h('div', { class: 'grid-foot' }, this.pageInfo, h('span', { class: 'spacer' }), h('span', { class: 'muted' }, t('studies.keys')),
        this.prevBtn, this.pageNo, this.nextBtn));
  }

  setDensity(d) {
    prefs.set('density', d);
    this.dataset.density = d;
    this.densitySeg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.d === d)));
  }

  renderColMenu() {
    clear(this.colMenu);
    for (const c of this.columns) {
      if (!c.label) continue;
      this.colMenu.append(h('label', { class: 'menu-item' },
        h('input', { type: 'checkbox', checked: !this.hiddenCols.has(c.key), onchange: (e) => {
          if (e.target.checked) this.hiddenCols.delete(c.key); else this.hiddenCols.add(c.key);
          prefs.set(`${this.storageId}.hidden`, [...this.hiddenCols]);
          this.renderHead();
          this.renderBody();
        } }), c.label));
    }
  }

  visibleColumns() {
    return this.columns.filter((c) => !this.hiddenCols.has(c.key));
  }

  renderHead() {
    clear(this.head);
    const tr = h('tr');
    for (const c of this.visibleColumns()) {
      const sorted = this.state.sort === c.key;
      const ariaSort = sorted ? (this.state.dir === 'asc' ? 'ascending' : 'descending') : false;
      const label = [c.label || h('span', { class: 'sr-only' }, c.srLabel || c.key), c.unit ? h('span', { class: 'unit' }, ` ${c.unit}`) : null,
        sorted ? (this.state.dir === 'asc' ? ' ↑' : ' ↓') : ''];
      tr.append(h('th', { scope: 'col', class: c.align === 'right' ? 'r' : '', 'aria-sort': ariaSort, style: c.width ? { width: c.width } : false },
        c.sortable === false ? label : h('button', { type: 'button', onclick: () => this.sortBy(c.key) }, label)));
    }
    this.head.append(tr);
  }

  renderBody() {
    clear(this.body);
    const cols = this.visibleColumns();
    this.rows.forEach((row, i) => {
      const tr = h('tr', { tabindex: i === this.focusIndex ? '0' : '-1', 'aria-selected': String(i === this.focusIndex),
        class: row._rowClass || '', onclick: () => this.focusRow(i), ondblclick: () => this.open(i) });
      for (const c of cols) {
        const v = c.render ? c.render(row) : row[c.key];
        tr.append(h('td', { class: c.align === 'right' ? 'r num' : '' }, v instanceof Node ? v : (v ?? '—')));
      }
      this.body.append(tr);
    });
  }

  sortBy(key) {
    if (this.state.sort === key) this.state.dir = this.state.dir === 'asc' ? 'desc' : 'asc';
    else { this.state.sort = key; this.state.dir = key === 'date' ? 'desc' : 'asc'; }
    this.state.page = 1;
    this.reload();
  }

  go(page) {
    const pages = Math.max(1, Math.ceil(this.total / this.state.size));
    this.state.page = Math.min(pages, Math.max(1, page));
    this.reload();
  }

  setState(partial) {
    Object.assign(this.state, partial);
  }

  async reload() {
    if (!this.fetcher) return;
    this.renderHead();
    this.setAttribute('aria-busy', 'true');
    this.status.className = 'grid-status loading';
    this.status.replaceChildren(h('span', { class: 'skeleton' }), h('span', { class: 'skeleton', style: { width: '70%' } }), h('span', { class: 'muted' }, t('state.loading')));
    const seq = (this._seq = (this._seq || 0) + 1);
    try {
      const res = await this.fetcher({ ...this.state });
      if (seq !== this._seq) return;
      this.rows = res.rows;
      this.total = res.total;
      this.focusIndex = 0;
      this.renderBody();
      this.status.className = 'grid-status';
      this.status.replaceChildren(this.total ? '' : h('div', { class: 'empty' }, h('strong', {}, t('state.empty')),
        h('span', { class: 'muted' }, ' Remove a filter, or widen the date range.')));
      const from = this.total ? (this.state.page - 1) * this.state.size + 1 : 0;
      const to = Math.min(this.total, this.state.page * this.state.size);
      const pages = Math.max(1, Math.ceil(this.total / this.state.size));
      this.countEl.textContent = num(this.total);
      this.pageInfo.textContent = `${num(from)}–${num(to)} / ${num(this.total)}`;
      this.pageNo.textContent = `${num(this.state.page)} / ${num(pages)}`;
      this.prevBtn.disabled = this.state.page <= 1;
      this.nextBtn.disabled = this.state.page >= pages;
      this.dispatchEvent(new CustomEvent('state-change', { detail: { ...this.state } }));
      if (this.rows.length) this.dispatchEvent(new CustomEvent('row-focus', { detail: this.rows[0] }));
    } catch (err) {
      if (seq !== this._seq) return;
      this.rows = [];
      this.renderBody();
      this.status.className = 'grid-status';
      this.status.replaceChildren(h('div', { class: 'error' }, h('span', { class: 'tag alert' }, 'Error'), ` ${t('state.error')} `,
        h('span', { class: 'muted mono' }, err.message), ' ', h('button', { class: 'btn', type: 'button', onclick: () => this.reload() }, t('state.retry'))));
    } finally {
      if (seq === this._seq) this.removeAttribute('aria-busy');
    }
  }

  focusRow(i, moveFocus = true) {
    if (i < 0 || i >= this.rows.length) return;
    const trs = this.body.querySelectorAll('tr');
    trs[this.focusIndex]?.setAttribute('tabindex', '-1');
    trs[this.focusIndex]?.setAttribute('aria-selected', 'false');
    this.focusIndex = i;
    trs[i].setAttribute('tabindex', '0');
    trs[i].setAttribute('aria-selected', 'true');
    if (moveFocus) trs[i].focus();
    this.dispatchEvent(new CustomEvent('row-focus', { detail: this.rows[i] }));
  }

  /** Select the row whose rowId equals id (used by charts linked to the table). */
  focusById(id) {
    const i = this.rows.findIndex((r) => this.rowId(r) === id);
    if (i >= 0) {
      this.focusRow(i, false);
      this.body.querySelectorAll('tr')[i].scrollIntoView({ block: 'nearest' });
    }
  }

  open(i) {
    this.dispatchEvent(new CustomEvent('row-open', { detail: this.rows[i] }));
  }

  onKey(e) {
    if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); this.focusRow(this.focusIndex + 1); }
    else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); this.focusRow(this.focusIndex - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); this.open(this.focusIndex); }
  }
}

customElements.define('dx-data-grid', DtDataGrid);
