# Dosimetrix web UI

Static frontend: plain HTML, CSS and vanilla JavaScript ES modules with native custom elements.
There is no npm and no build step. The only third-party file is Apache ECharts, vendored in
`assets/vendor/echarts/`. The visual direction is **B "Console"** (see `docs/design.md` §3):
dark-first with a light theme, an icon rail, a Ctrl K command palette and typed query tokens.

## Run it

ES modules do not load from `file://`, so serve the folder over HTTP:

```sh
python3 -m http.server 8765 -d dosimetrix/web
# open http://127.0.0.1:8765/
```

Pages run on the synthetic data in `data/sample/` until the REST API exists.

## Layout

| Path | What |
|---|---|
| `index.html` | Overview: studies per modality, alert queue, device feed health, exam lookup |
| `studies.html` | CT study list: saved queries, query bar, server-paged grid, preview pane, CSV export |
| `study.html` | Study detail (XA): readouts with thresholds, 12-week cumulative, running Ka,r and event chart, event table, print |
| `charts.html` | Workspace: panels sharing one query; dots vs DRL, boxplot, bar, histogram, trend, scatter, workload |
| `devices.html` | Admin: devices grouped by physical unit, inline display names, merge inspector |
| `assets/css/tokens.css` | Design tokens; both themes come from `[data-theme]` on `<html>` |
| `assets/css/base.css` | Base elements, shell, buttons, tags, print rules |
| `assets/css/components.css` | Styles for the components and page patterns |
| `assets/js/boot.js` | Sets theme and language before first paint (classic script) |
| `assets/js/core/` | `api` (data access), `query` (query language), `stats`, `format` (units), `i18n` (EN/RO), `prefs`, `dom` |
| `assets/js/components/` | `dx-shell`, `dx-command-palette`, `dx-query-bar`, `dx-data-grid`, `dx-chart` |
| `assets/js/pages/` | One module per page |
| `data/sample/` | Synthetic data from `tools/make_ui_sample.py` (deterministic) |
| `tests/` | `node --test dosimetrix/web/tests/*.test.mjs` |

## Conventions

The interface rules (buttons, confirm dialog, breakpoints, utilities, dense text) are in
[`docs/design-rules.md`](../../docs/design-rules.md). Build conventions:

- **No `innerHTML`.** Build DOM with `h()` from `core/dom.js`; text always goes through `textContent`.
- **Colours only from tokens.** Charts read tokens at draw time (`chartTheme()`) and redraw when the theme changes and around printing.
- **Fixed storage units:** DAP in Gy·cm², DLP in mGy·cm, CTDIvol in mGy, Ka,r in mGy (events) or Gy (totals). `core/format.js` converts for display; the DAP unit is a user preference.
- **Filter state lives in the URL** (`?q=`), using the query language in `core/query.js`: `key:value`, `> >= < <=`, `a..b`, lists, quotes, `-` to negate. The server will accept the same string.
- **Pages talk only to `core/api.js`.** Set `<html data-api="https://host">` to use the REST API; the endpoints are noted per function.
- **Accessibility:** real buttons and links, visible focus, `aria-sort` on sorted columns, keyboard row navigation (↑/↓, J/K, Enter), a native `<dialog>` for the palette, and status tags that pair colour with words.

## Not done yet

- The REST API and its wiring (sample mode filters, sorts and pages in the browser).
- Persisting display-name edits, merges and alert acknowledgements (sample mode changes the page only).
- Detail pages for CT, DX and MG (sample mode has one XA study).
- DX, RF/XA and MG lists and charts; the skin-dose map.
- Workspaces stored per user on the server, with read-only sharing (now saved in `localStorage`).
- Validating the colour-blind separation of the 8 device colours.
- Full Romanian translation of page bodies (the shell and the main labels are translated).
