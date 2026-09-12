---
topic: css
triggers: writing or reviewing CSS, responsive layout, a page that scrolls sideways on mobile, theming, focus and motion handling, anything that will be seen by a customer
source: docs/CSS_BEST_PRACTICES.md
verified: 2026-09-11
---

# CSS

Read before writing CSS in a project that already has some. Every rule here is a
failure that happens in real interfaces, not a style preference.

## Before you add a single declaration

Find the styling system that already exists: framework, token file, reset,
utility layer, component library. A second system layered over a first is the
single most common cause of an unmaintainable stylesheet.

Never diagnose the cascade from selector length. Inspect the winning
declaration and its origin, layer, specificity and order in devtools.
Increasing specificity to win a fight you have not diagnosed is how a codebase
ends up with `!important` on 200 lines.

## Cascade and layers

Order is declared once, and declaring it moves nothing into a layer:

```css
@layer reset, tokens, base, layout, components, utilities, overrides;
```

Two facts that trip people up:

- Unlayered author rules beat layered ones. Moving only your new fixes into a
  layer leaves the legacy unlayered CSS winning, which looks like your fix did
  nothing.
- `!important` reverses layer order. The earliest layer wins instead of the last.

`:where(...)` contributes zero specificity, which makes it the right wrapper for
a shared base other rules must override. `:is()`, `:not()` and `:has()` take the
specificity of their most specific argument, so they are not a free lunch.

## Tokens

A token should name a design decision, not rename a number.
`--space-4: 1rem` earns its place. `--gray-427` does not.

```css
:root {
  --space-2: 0.5rem;
  --space-4: 1rem;
  --page-gutter: clamp(1rem, 2.5vw, 2rem);
  --content-max: 80rem;
  --reading-max: 65ch;
}
```

Define complete foreground and background pairs per theme and state. A token
name never proves its contrast ratio. Keep one-off values local until a real
pattern exists.

## The sideways-scroll bug

This is the most common responsive defect and it is almost never solved by the
thing people reach for first.

Grid and flex items have a content-based automatic minimum size, so a long URL,
a filename or a nested control pushes the item wider than its track. Fix the
sizing:

```css
.row        { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; }
.row__body  { min-inline-size: 0; overflow-wrap: anywhere; }
```

`min-inline-size: 0` on the item, `minmax(0, 1fr)` on the track.

Do **not** put `overflow-x: hidden` on the root to make the symptom disappear.
It hides real content and clips focus rings, and the layout bug is still there.

`overflow-wrap: anywhere` breaks unbroken strings. `word-break: break-all`
mangles ordinary prose, so do not reach for it globally.

## Layout

Grid for relationships across rows and columns, flex for one flexible row or
column. Neither is the "mobile" one.

```css
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
  gap: var(--space-4);
}
```

The inner `min()` is what lets a card fit a container narrower than 18rem.
Without it the grid overflows at small widths.

Avoid absolute positioning for ordinary flow, negative margins that conceal a
sizing error, and fixed heights around variable text.

Container queries, when the component's layout depends on its own slot rather
than the viewport:

```css
@supports (container-type: inline-size) {
  .card-container { container: card / inline-size; }
  @container card (min-width: 36rem) {
    .card { grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); }
  }
}
```

The query styles descendants, never the queried container itself. Keep the
stacked base usable without the enhancement.

## Scrolling

Default to document scrolling. Nested scroll areas are for a justified work
area: a transcript pane, a dialog body, a wide table. Identify the scroll owner
and give it a bounded size; a flex or grid child that must scroll usually needs
`min-block-size: 0`.

`overflow: hidden` can create a scroll container. `overflow: clip` cannot.
Setting one axis changes the computed behaviour of the other.

## Type and media

```css
.prose       { max-inline-size: var(--reading-max); line-height: 1.6; }
.page-title  { font-size: clamp(1.75rem, 1.25rem + 2vw, 3rem); line-height: 1.15; }
.numeric     { font-variant-numeric: tabular-nums; }
```

Unitless line height. A `clamp()` cap can block zoom enlargement, so test actual
browser zoom rather than assuming `rem` units make it compliant. Never size
essential text in viewport units alone.

Images need reserved space or they cause layout shift:

```css
.product-image {
  display: block;
  inline-size: 100%;
  block-size: auto;
  aspect-ratio: 1;
  object-fit: contain;
}
```

Set `width`/`height` attributes, make `sizes` describe the real rendered slot,
and never put `loading="lazy"` on the likely LCP image. `contain` when the whole
thing must stay visible (a receipt, packaging, a diagram), `cover` only when
cropping is genuinely acceptable.

## Mobile viewport

```html
<meta name="viewport" content="width=device-width, initial-scale=1">
```

Keep user zoom. `user-scalable=no` is never a layout fix.

| Unit | Use | Catch |
|---|---|---|
| `vh` / `lvh` | Large viewport | Browser chrome can cover content sized to it |
| `svh` | Stable small viewport | Leaves a gap once chrome retracts |
| `dvh` | Tracks chrome as it moves | Resizes during scroll, and is not keyboard avoidance |

```css
.landing   { min-block-size: 100vh; min-block-size: 100svh; }
.app-shell { min-block-size: 100vh; min-block-size: 100dvh; }
```

Safe-area insets are physical sides, not logical ones, and they are not the
keyboard height:

```css
.edge-safe-shell {
  padding-top:    max(1rem, env(safe-area-inset-top, 0px));
  padding-bottom: max(1rem, env(safe-area-inset-bottom, 0px));
  padding-left:   max(1rem, env(safe-area-inset-left, 0px));
  padding-right:  max(1rem, env(safe-area-inset-right, 0px));
}
```

`max()` when the padding and the inset share the space, `calc(base + inset)`
when both are required. In a native wrapper, make sure only one side adds them.

On iOS, an input with a computed font size under 16px triggers focus zoom. Fix
the font size; never disable zoom to hide it.

## Accessibility that is actually checkable

- `:focus-visible` styling on every interactive element, and never
  `outline: none` without a replacement that is visible against every background
  it sits on.
- Hit targets large enough to use, with spacing between adjacent destructive and
  non-destructive actions.
- Colour is never the only carrier of state.
- Honour the preference:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

Truncation is only acceptable when the user has a reliable way to get the full
text. `title` is not that way on touch. Never truncate totals, quantities, or
the description of a destructive action.

## Themes

Define the full light palette on bare `:root`, redefine only the tokens under
`@media (prefers-color-scheme: dark)`, and redefine them again under an explicit
`[data-theme="dark"]` so a manual toggle wins in both directions. A colour whose
only definition lives inside a media query breaks the toggle.

Give `body` an explicit background token. A transparent body borrows whatever is
behind it.

## Never ship these

They read as machine-generated and are a hard requirement on anything a customer
sees:

- A decorative coloured left rail down the side of a card, callout, banner or
  status row. The only exception is a neutral hairline beside a genuine
  quotation of someone else's words.
- One radius, one padding and one card shape repeated across every component.
  Real design varies by role.
- A purple-to-blue gradient, and Inter chosen as the only typographic decision.
- A generic gradient block standing in for a logo, mark or photo.
- Em dashes and en dashes in customer-facing copy.

## Diagnose, do not accumulate

When something looks wrong, find the cause before adding a rule. A stylesheet
grows unmaintainable through overrides stacked on undiagnosed problems, not
through any single bad declaration. If a fix needs `!important`, you have not
found the cause yet.

Verify on real devices and at real browser zoom levels, not only in a resized
desktop window: they disagree about keyboards, safe areas and dynamic viewport
units, which is exactly where these bugs live.
