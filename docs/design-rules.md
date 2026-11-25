# Design rules

Where the interface decisions are collected, so a new screen follows them instead of
rediscovering them. It does not replace the sources: `docs/design.md` is the design draft,
`dozimetrix/web/README.md` says how the frontend is built and run. Each rule says when it was
decided. **Add a rule when the owner decides one (date it), and fix the rule, not just the
screen, when the two disagree.** The format follows the Reporion project's rules file; only the
generic rules were taken over.

Sections: [Principles](#principles) · [Buttons](#buttons) · [Confirming a risky action](#confirming-a-risky-action) ·
[Icon-only controls](#icon-only-controls) · [Breakpoints](#breakpoints) · [Long content](#long-content) ·
[Dense data and text size](#dense-data-and-text-size) · [Utilities and inline style](#utilities-and-inline-style)

## Principles

- **Colours and spacing come from tokens** (`assets/css/tokens.css`: `--sp-1…6`, `--fs-*`, `--r`,
  state colours `--ok|--warn|--alert`). A new shade is a derived token, not a hex in a rule.
  Charts read tokens at draw time.
- **No `innerHTML`**: build DOM with `h()` (`core/dom.js`); text goes through `textContent`.
- **Fix the rule, not the symptom**: a breakpoint bug is fixed in the rule that causes it; two
  rules for one component are merged.
- **One control, one look**: the same thing is drawn the same way on every page.

## Sections and headings

Decided 2026-11-25. Every titled block of a page, every page title and every dialog heading is built
the same way, with `section()` / `panel()` from `assets/js/core/dom.js` (never a bare `h1` / `h2`):

```html
<section class="block">
  <hgroup><h2>Title</h2><p>One line: what this block is, or its status</p></hgroup>
  <table class="grid">…</table>        <!-- the main element: table, form, .panel, div… -->
</section>
```
- The title is an `h2` (an `h1` for the page's own title) inside an `hgroup`, followed by the `p`
  subtitle when there is something useful to say (a bare `hgroup` with only the title is fine; do not
  invent one).
- A block with buttons (Add, Save…) wraps them: `<section><header><hgroup>…</hgroup><div class="actions">…</div></header>…`.
- A card is `panel(title, { sub, actions, id }, …body)`: `section.panel > header.panel-head > hgroup`.
- Spacing between blocks comes from `.block` in `components.css`: no `mt-*` on the title.
- `tests/test_web_static.py` fails on `h('h1'` or `h('h2'` outside `dom.js`, and on the old `section-title` class.

## Buttons

Decided 2026-11-23. One `.btn` base, a **role** class and an optional **size** class.

| Class | Look | Use for |
|---|---|---|
| `.btn` | default: panel fill, outline | an action beside others (Check, Export, Compare) |
| `.btn.primary` | accent fill | **the** action of a form, bar or panel: at most one |
| `.btn.ghost` | no fill, no outline | a quiet or dismissive action; never the only way to do something important |
| `.btn.danger` | outline and text in `--alert`, tinted on hover | a destructive action beside ordinary ones (Delete, Remove) |
| `.btn.ghost.danger` | `--alert` text only | the same in a table row |
| `.btn.danger.solid` | solid `--alert` | **only the confirm dialog's** destructive button |

- **Sizes**: normal (28 px) and `.xs` (22 px, for table rows, panel headers and toolbars). There is
  no other size name (`small` was a stray, 2026-11-23).
- A destructive act is an outline in the page and solid only in the dialog that asks.
- A button never wraps its text. `[disabled]` is 50% opacity.

## Confirming a risky action

Decided 2026-11-23. Never `window.confirm()`: use `confirmDialog(message, { title, label, danger })`
from `core/confirm.js`, one native `<dialog>` (focus trap, Escape cancels).

- Deleting or removing something, resetting a workspace, writing to the data, revealing a patient's
  identity: all ask. Reads and previews never do.
- The confirm button names the act (`Delete`, `Remove`, `Write`), not "OK". A destructive one is
  `danger: true`: solid red, and focus starts on **Cancel**.
- The message says what is lost or recorded ("Their audit history stays", "This is recorded in the
  audit log").

## Icon-only controls

Decided 2026-11-23. A button or link that shows only a symbol (×, ←, ‹, an avatar) carries **both**
`aria-label` (for screen readers) and `title` (the tooltip), with the same words. Prefer a word to
a bare symbol when there is room.

## Breakpoints

Decided 2026-11-23, Pico's scale: **576 / 768 / 1024 / 1280 px**. Use only these.
Max-width queries end in `.98` (`575.98px`, `1023.98px`) so a min and a max query of one step never
both match. Today: below 576 the icon rail moves to the bottom and the dashboard lists fold; below
1024 the chart workspace is one column. Print has its own rules (`@media print`).

## Long content

Decided 2026-11-23. A long table or list **scrolls inside its panel** instead of stretching the
page: `.panel-scroll` (max 320 px, sticky header, full length in print). Used for the chart
panels' summary tables and a repeat alert's list of earlier studies, which is also sorted most
recent first. Paged grids (`dx-data-grid`) page instead. Chart panels may limit what they draw
with a **Top** option (the busiest N groups); the summary table and the CSV keep everything.

## Dense data and text size

Decided 2026-11-23, owner: **small text is accepted** (10 to 12 px, `--fs-xs` 11 px) because the
screens must fit much more data, above all numbers in tables. Do not raise it to a "minimum size".

- Numbers in columns use `.mono` (tabular figures) and are right-aligned (`.r`).
- Secondary text is `.muted` or `.small`; what a user must read to act is never only `.muted`.
- Tap targets on a phone are still ≥ 36 px for buttons and inputs.

## Utilities and inline style

Decided 2026-11-23. No inline `style` for a fixed value: use the class in
`assets/css/components.css` ("utilities"): `.r` (right-align), `.small`, `.sans`, `.m0`,
`.mt-1…5` (margin-top by spacing token), `.jc-end`, `.w-70 … .w-320` (fixed widths). Inline style is
for a **computed** value only (a bar's width in %, a CSS variable, an opacity from data). If the
same nudge appears three times it becomes a class here.
