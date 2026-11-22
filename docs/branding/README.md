# Dosimetrix branding

- **Name:** written *Dosimetrix* in text and UI; `dosimetrix` for the package, command and repository; `DOSIMETRIX` as the default DICOM AE title.
- **Mark:** the "D-curve": a D whose bowl holds a dose curve. A single stroke on a 24-unit grid (`assets/img/logo-mark.svg`, `currentColor`); the favicon puts it on the dark tile (`assets/img/favicon.svg`). Stroke width 2–2.4 depending on size; it stays legible at 16 px.
- **Wordmark:** "Dosimetri**x**" with the final x in the accent colour (#B98CF0 on dark, #6B35B8 on light); the rest in the text colour.
- **Palette "Iodine"** (`assets/css/tokens.css`): violet accent #B98CF0 on warm aubergine-graphite (bg #121016, panel #19161F) in the dark theme; #6B35B8 on white / #EEECF1 in the light theme. Favicon tile #121016. Status stays green / amber / red (WCAG AA on every panel).
- **Chart series:** a fixed 8-colour order (blue, orange, aqua, yellow, magenta, green, violet, red), stepped separately for dark and light; checked with the dataviz palette validator on both panels: adjacent CVD ΔE ≥ 8.4, normal-vision ΔE ≥ 19.3. Three light-mode slots sit below 3:1 on white, so charts keep legends and tooltips. The former series failed the colour-vision check (pink next to teal, ΔE 1.2–3.9).
- `palette-options.png`: the palettes considered (current cyan, Iodine, Phosphor, Instrument), dark overview, dark charts and light overview.
- `logo-options.png`: the three concepts considered (gauge, beam cross, D-curve).
- **Palettes, chosen per browser** (the Appearance menu in the top bar, next to dark / light): *Iodine* (default), *Cherenkov* (the former cyan, a reactor pool's glow), *Phosphor* (chartreuse, a dosimeter's glow; "ok" turns teal) and *Instrument* (graphite and bone; heat maps in blue). Each has dark and light steps in `tokens.css` (`:root[data-palette=…]`); `boot.js` applies the stored choice before the first paint. All four share the validated chart series and the status colours; every text, accent and status token clears WCAG AA on its panel.

