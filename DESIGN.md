# DESIGN.md — City Agent : PG Release Order

A design system for the UI revamp, derived by measuring the **CityAgent Insights**
codebase (`CityAgentWork/bagofwords`, Nuxt 3 + Nuxt UI + Tailwind, 345 components)
rather than by taste. Every rule below is backed by a count from that codebase, so
"what the house style is" is a fact, not an opinion.

---

## 0. What was measured

350 files scanned across `components/`, `pages/`, `layouts/`. Class-frequency
analysis of every `class="…"` attribute.

The headline numbers, because they drive everything else:

| signal | count | what it means |
|---|---:|---|
| `text-xs` | 1,952 | the app's **body size is 12px**, not 14 or 16 |
| `text-sm` | 1,018 | 14px is the *large* size |
| `text-[11px]` / `text-[10px]` | 735 / 547 | 10–11px is normal for labels |
| `font-medium` | 1,177 | **500 is the default weight**, not 400 |
| `border-gray-200` | 619 | structure comes from **hairlines** |
| `shadow-*` | 144 | …almost never from shadows |
| `dark:` | 7,969 | dark mode is **first-class**, not a bolt-on |
| `hover:` | 2,183 | everything interactive responds |
| `focus:` / `ring-` | 867 | keyboard focus is designed, not default |
| `p-2` / `p-3` / `p-1.5` | 1,342 / 835 / 566 | **tight** spacing |
| `UIcon` | 585 | heroicons, everywhere |

**The one-sentence summary:** a dense, quiet, hairline-ruled instrument panel in
small type, where colour is reserved for meaning and every control answers to
hover and focus.

---

## 1. Colour

### The neutral ramp does the work

CityAgent Insights is built almost entirely from grays. Ranked by use:

```
text-gray-400  1412   ← muted / placeholder / icon default
text-gray-500  1359   ← secondary text  (the single most common pairing)
text-gray-700   669   ← body text
text-gray-600   581   ← body text, softer
text-gray-900   405   ← headings / emphasis
bg-white        451   ← surface
bg-gray-50      250   ← sunken / sidebar / table stripe
bg-gray-100     154   ← hover fill
border-gray-200 619   ← every divider
border-gray-100 243   ← softer divider
```

### One accent, and it is blue

```
text-blue-600   148    bg-blue-500    41
text-blue-500   102    bg-blue-600    40
                       bg-blue-50     84   ← selected row / active nav
```

`app.config.ts` pins it deliberately, and the comment explains why:

> *"The app uses blue as its accent throughout (buttons, links, active nav). Nuxt
> UI defaults `primary` to green, which leaked into focus rings on inputs…"*

**Rule: one accent hue. Everything else is gray or semantic.**

### Semantic colours mean one thing each

```
red    → error / destructive     text-red-500 113 · bg-red-50 31
green  → success / approved      text-green-600 61 · bg-green-50 23
amber  → warning / needs review  text-amber-700 47 · bg-amber-50 41
```

Semantic colour is **never** used for decoration. A green pill means approved; it
does not mean "this looks nice in green".

### Our decision: keep the warm identity, adopt the discipline

RO-ED already has a warm palette (cream `#FAF9F5`, clay `#CC785C`) and it is on the
product's own brand. **Do not replace it with cool gray/blue** — that would trade a
considered identity for a borrowed one.

What we adopt is the *structure*: one accent, a proper neutral ramp with five stops,
semantic colours reserved for meaning, and full dark mode.

```css
:root {
  /* ground */
  --bg:            #FAF9F5;   /* page */
  --surface:       #FFFFFF;   /* cards, rows */
  --sunk:          #F3F0E9;   /* sidebar, table head, hover fill */

  /* ink — five stops, mapped to Insights' gray-900/700/500/400 */
  --ink:           #1E1C1A;   /* headings, emphasis      ~gray-900 */
  --ink-2:         #5C5650;   /* body                    ~gray-700 */
  --ink-3:         #8A837B;   /* secondary, labels       ~gray-500 */
  --ink-4:         #ADA79F;   /* muted, placeholder      ~gray-400 */

  /* structure */
  --line:          #E5E0D8;   /* every divider           ~gray-200 */
  --line-soft:     #EFEBE3;   /* inside cards            ~gray-100 */

  /* the one accent */
  --accent:        #CC785C;   /* clay — links, active nav, primary button */
  --accent-weak:   #F7EDE8;   /* selected row            ~blue-50 */

  /* semantic — one meaning each */
  --ok:            #3F7A56;   /* approved, passed */
  --ok-weak:       #EAF2ED;
  --warn:          #B8842B;   /* needs review */
  --warn-weak:     #FAF3E4;
  --err:           #B3452F;   /* failed, destructive */
  --err-weak:      #FBEDEA;
}
```

Dark mode is **required**, not optional — Insights has 7,969 dark variants. Ours:

```css
@media (prefers-color-scheme: dark) { :root { …dark tokens… } }
:root[data-theme="dark"]  { …same… }   /* explicit toggle must win */
:root[data-theme="light"] { …light… }  /* in both directions */
```

```css
/* dark */
--bg:#191817; --surface:#221F1E; --sunk:#1F1D1B;
--ink:#EDE8E0; --ink-2:#B0A89F; --ink-3:#847C74; --ink-4:#6B645C;
--line:#332F2C; --line-soft:#2A2724;
--accent:#E09274; --accent-weak:#2E2320;
--ok:#6FB58C; --warn:#D9A648; --err:#E2705A;
```

---

## 2. Typography

### The app is small and dense

This is the biggest single lesson, and the biggest change for us. RO-ED currently
sets `html { font-size: 15px }` with 15px body. Insights runs at **12px**.

| role | size | weight | colour |
|---|---|---|---|
| page title | 18–20px | 600 | `--ink` |
| section heading | 13px | 600 | `--ink` |
| **body / table cell** | **12px** | **500** | `--ink-2` |
| secondary | 12px | 400 | `--ink-3` |
| label / caption | 11px | 500 | `--ink-3` |
| table header | 10–11px | 500 | `--ink-3`, uppercase, `tracking-wider` |
| micro / badge | 10px | 500 | inherit |

`font-medium` outnumbers `font-normal` **1,177 to 30**. At 11–12px, 400 is too
thin to hold a line; 500 is the default and 600 is emphasis. **Do not use `bold`
for body chrome** — `font-bold` appears only 66 times in 345 files.

### Families

Insights uses the platform stack with no webfont. RO-ED's serif headings
(Source Serif 4) are part of its identity and should stay — but only for **page
titles**, not for panel headers.

```css
--sans:  system-ui, -apple-system, "Segoe UI", sans-serif;   /* everything */
--serif: "Source Serif 4", Georgia, serif;                   /* page titles only */
--mono:  "JetBrains Mono", ui-monospace, SFMono-Regular, monospace;
```

**Mono is for data, and data means data**: declaration numbers, money, dates,
job ids, HS codes, file names. `font-mono` appears 173 times in Insights and
always on a value, never on prose.

Always pair mono figures with `font-variant-numeric: tabular-nums` so columns
line up.

**16px minimum on mobile inputs.** From `mobile.css`, with the reason:

> *"iOS/WebKit auto-zooms (and then scroll-jumps) when focusing an `<input>`
> whose font-size is < 16px."*

```css
@media (max-width: 640px) {
  input:not([type='checkbox']):not([type='radio']), select, textarea { font-size: 16px; }
}
```

---

## 3. Space

Tight, and on a scale. Measured frequencies:

```
p-2  (8px)  1342      gap-2   (8px)  648
p-3 (12px)   835      gap-1   (4px)  430
p-1  (4px)   732      gap-1.5 (6px)  313
p-1.5(6px)   566      gap-3  (12px)  168
p-4 (16px)   502
```

**Scale: 4 · 6 · 8 · 12 · 16 · 24.** Nothing else. `p-6` (24px) is for page
padding and modal bodies only.

Lay groups out with flex/grid + `gap`, never per-child margins — margins collapse
and double in ways that are hard to see.

Row heights: table rows `py-2` (8px), list rows `py-1.5`, dense tool rows `py-1`.

---

## 4. Structure

### Hairlines, not shadows

619 `border-gray-200` against 144 `shadow-*`. Panels are separated by 1px lines.
Shadow is reserved for things that genuinely float — modals, popovers, dropdowns.

```css
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
```

### Radius

```
rounded    (4px)  890    ← the default
rounded-lg (8px)  447    ← panels, cards
rounded-md (6px)  414    ← buttons, inputs
rounded-full      294    ← pills, avatars, badges
```

Never square, never more than 8px except pills. **Never reintroduce the old
brutalist `border-radius: 0` reset.**

### Navigation: sidebar, not top bar

Insights uses a **collapsible left sidebar** — `sm:w-60` expanded, `sm:w-14`
collapsed, off-canvas below `sm` with a hamburger and a backdrop.

RO-ED currently uses a horizontal top nav with 8 items. That works today but does
not scale, and it wastes the full width on a page that needs columns. **Move to a
sidebar**, matching Insights:

- expanded `240px`, collapsed `56px`, preference persisted
- below 640px: off-canvas drawer + backdrop, hamburger in a 48px top bar
- active item: `--accent-weak` fill, `--accent` text, no underline
- icon + label expanded; icon + tooltip collapsed

---

## 5. Components

The most repeated class recipes *are* the component library. These are the ones
to build, with their measured usage:

**Table header** (41×)
```
px-6 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider text-start
```

**Muted caption** (110×) — the single most common recipe in the codebase
```
text-xs text-gray-500 dark:text-gray-400
```

**Row title** (42×)
```
text-sm font-medium text-gray-900 dark:text-white
```

**Field label** (42×)
```
text-sm font-medium text-gray-700 dark:text-gray-300 mb-2
```

**Inline icon** (44×)
```
w-3 h-3 me-1 text-gray-400
```

**Header bar** (75×)
```
flex items-center justify-between
```

**Icon + text** (72×)
```
flex items-center gap-1.5
```

### Buttons

Primary = accent fill, white text. Secondary = surface + hairline. Ghost = text
only, `--sunk` on hover. Destructive = `--err`. All `rounded-md`, `py-1.5 px-3`,
12px, weight 500. Every one needs `hover:` and a visible `focus-visible` ring —
2,183 hover states and 867 focus rules say this is not optional.

### Status

Pills, `rounded-full`, 10px, weight 500, semantic colour on its `-weak` fill:

```
approved   --ok    on --ok-weak
review     --warn  on --warn-weak
failed     --err   on --err-weak
running    --accent on --accent-weak
```

**A status must never be colour alone.** Pair with a word, and for confidence add
a bar so the difference between 88.7% and 55.4% is visible without reading.

### Icons

**Heroicons**, one set, no mixing. 585 uses in Insights; the common ones are
`check-circle`, `x-mark`, `x-circle`, `plus`, `trash`, `arrow-path`,
`exclamation-triangle`, `sparkles`. Default `w-4 h-4`, inline `w-3 h-3`, colour
`--ink-4` unless carrying meaning.

---

## 6. Motion

```css
.fade-in-enter-active { transition: opacity 200ms ease; }
```

Insights' entire transition file is a fade. 540 `transition` uses, nearly all
`transition-colors` on hover.

**Rules:** 150–200ms, `ease`. Colour and opacity only — never layout. Nothing
loops. And:

```css
@media (prefers-reduced-motion: reduce) { *, *::before, *::after {
  animation-duration: .01ms !important; transition-duration: .01ms !important; } }
```

*(We had an infinitely blinking fake terminal cursor that ignored this. It is
gone. Don't add another.)*

---

## 7. Internationalisation and direction

Insights is fully RTL-ready and translated: **logical properties everywhere** —
`start-0`/`end-0`, `ms-`/`me-`, `ps-`/`pe-`, `text-start` — and every user-facing
string goes through `$t()`.

RO-ED serves a Myanmar team and already mixes English and Burmese in its
documents. **Use logical properties from the start.** Retrofitting `left`/`right`
across a codebase is miserable; writing `start`/`end` costs nothing today.

---

## 8. Writing

From the Insights code, phrasing is plain and specific — a control says exactly
what happens, errors say what went wrong and what to do.

- **Sentence case** for everything except tiny labels and table headers, which are
  `UPPERCASE` with `tracking-wider` at 10–11px.
- **No jargon in user-facing text.** The `issues.py` layer already does this:
  *"Products do not add up to the total"*, not *"item-sum reconciliation failed"*.
  Extend that rule to the whole UI.
- Numbers keep their separators and their currency.
- Empty states say what to do next, not "No data".

---

## 9. What this means for our pages

| page | today | change |
|---|---|---|
| **shell** | top nav, 8 items | collapsible sidebar, 240/56px, mobile drawer |
| **Agent** | 3-column workbench ✓ | keep; restyle to 12px, hairlines, tokens |
| **Review** | split view | keep; tighten to the type scale |
| **History / Items / Declarations** | tables | one table recipe, uppercase 10px headers, confidence bars |
| **Checks** | evidence cards | pills + semantic colour, mono for values |
| **Costs** | ECharts | tokenised palette; accent for the series, gray for grid |
| **Settings** | forms | one field recipe: 13px label 500, 12px help `--ink-3` |
| **all** | light only | **add dark mode** — the single biggest gap vs Insights |

### Order of work

1. **Tokens + dark mode** in `app.css` — nothing else can be done properly first.
2. **Type scale** — 15px → 12px base. Touches everything; do it in one pass.
3. **Sidebar shell** — replaces `TopNav.svelte`.
4. **Primitives** — button, pill, table, field, panel, empty state.
5. **Pages** — Agent, Review, then the three table pages, then Settings.

### Do not

- Do not copy Nuxt UI components. Insights is Nuxt/Vue; we are SvelteKit 5 + runes.
  What transfers is the **system**, not the code.
- Do not drop the warm identity for cool gray. Adopt the discipline, keep the brand.
- Do not use raw hex in components — every colour through a token.
- Do not use `bold` for body chrome, or uppercase for anything above 11px.
- Do not reintroduce the brutalist theme: no `border-radius: 0` reset, no
  `4px 4px 0 0` stamp shadow, no neon green, no Space Grotesk.

---

## 10. Honest caveats

- Frequency tells you **what is used**, not what is *good*. `rounded` at 890 is a
  Tailwind default as much as a decision. Where the count is ambiguous I said so.
- 345 non-backup components were counted; the directory also holds ~40 `.bak-*`
  files, excluded.
- Insights has no custom webfont; RO-ED does. That is a real divergence, kept
  deliberately, and it is the one place this document departs from the source.
- The 12px base is measured, but it is a **large** change for a team reading
  Burmese alongside English. Worth a check with a real user on a real screen
  before committing the whole app to it.
