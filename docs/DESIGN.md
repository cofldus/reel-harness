# Fable Studio — UI v1

Frozen. From here, only clear usability defects get changed; taste
revisions do not. If something below turns out to be wrong, change it
here first and let the code follow.

The single source of truth is `reel_harness/web/static/app.css`. Nothing
in a template picks a value that is not on a scale defined there.

---

## What this replaced, and why

The app read as a data-review console. Every section was a card of the
same weight, so "what do I do next" had to be inferred by scanning all of
them; the paid-generation panel sat in a sidebar that outshouted the
work; and the colour, radius and type choices were a management tool's,
not a filmmaking tool's.

Three decisions drive everything:

1. **One purpose per screen.** The current step's action is a hero block
   at the top (`.now`), resolved on the server by `build_next_action`.
   Everything below it is reference material for making that one
   decision, and is styled quieter — not equal. Hierarchy is the feature.
2. **Money rides on the action.** The cost of a thing belongs on the
   button that does it. The budget is a collapsible bar, not a column,
   because it is checked occasionally rather than read continuously.
3. **Cinematic, not neon.** Ink charcoal over pure black (black crushes
   next to video and reads cheap), champagne gold over signal yellow.

---

## Name

**Fable Studio** — "Cinematic Story-to-Video Studio".

The header brand is a template block, so Fable owns its identity inside
its own section while the job queue and publish log keep the console's.

---

## Colour

Dark is primary: this tool is used beside footage, where bright chrome is
both fatiguing and misleading about colour. Light is a deliberate
variant on warm paper, not an inversion.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0b0d11` | `#faf8f4` | page ground |
| `--surface` | `#12161c` | `#ffffff` | cards, fields |
| `--surface-2` | `#171c23` | `#f5f2ec` | recessed areas |
| `--surface-3` | `#1e242d` | `#ebe6dc` | tracks, covers |
| `--border` | `#2a313c` | `#e2ddd2` | hairlines |
| `--border-strong` | `#3a4350` | `#c9c2b4` | interactive edges |
| `--text` | `#f5f1e8` | `#16181d` | body |
| `--text-2` | `#b6b0a3` | `#565b66` | secondary |
| `--muted` | `#7e7a72` | `#7e7a72` | labels, meta |
| `--accent` | `#d9a63a` | `#8a6512` | see below |
| `--ai` | `#7c5cfa` | `#5240b8` | see below |
| `--ok` | `#38c172` | `#24794a` | approved, selected |
| `--warn` | `#e3a63b` | `#8a6212` | attention |
| `--danger` | `#e15a5a` | `#b23c3c` | failure, destructive |

**Gold means exactly three things** — the primary action, the current
step, and "selected". Used anywhere else it stops meaning anything. Never
fill a card with it; it belongs on a hairline, a chip, a rim, a meter.

**Violet is not a second brand colour, it is a second meaning:** content
the machine authored. The AI-refinement button and its proposal panel,
and nothing else. This is what lets a user tell their own work from a
suggestion at a glance without reading a label.

Light mode darkens the same gold hue rather than substituting a different
colour, so the brand survives the scheme change while text stays legible.

---

## Type

Two weights of one voice, not two voices. Both self-hosted (see
`static/fonts/NOTICE.md`) — a CDN font breaks offline use and leaks
usage — and both variable, so weights are real axis positions.

**SUITE** (display) — the wordmark, page titles, section headings. About
a fifth of the text on screen. It carries more personality than a
neutral UI grotesque, which is what stops the product reading as a
dashboard. An earlier draft set headlines in a Korean serif; it looked
like a literary magazine rather than a production tool.

**SUIT** (text) — everything else.

| Role | Family | Weight | Size |
|---|---|---|---|
| Wordmark | SUITE | 800 | 17px, tracking 0.08em |
| Page title (`.display`) | SUITE | 750 | 32–40px |
| Section heading | SUITE | 700 | 19px |
| Card title | SUIT | 700 | 17px |
| Body | SUIT | 400 | 15px |
| Button | SUIT | 650 | 14px |
| Badge, meta, shot grammar | SUIT | 550 | 12px |

Rules that are not negotiable:

- `word-break: keep-all` globally. Without it Korean splits mid-word
  (나옵니다 breaks across two lines). This is the single most important
  line in the stylesheet for Korean.
- `tabular-nums` on anything compared or stacked: costs, budgets,
  durations, take numbers.
- No monospace on badges or shot metadata. No uppercase-plus-wide-
  tracking except `.eyebrow`. Nothing below 11px.

---

## Scales

- **Space** `--s1..--s8` = 4, 8, 12, 16, 24, 32, 48, 64. Nothing off-ramp.
- **Radius** 4 / 8 / 12 / 16 / pill.
- **Shadow** three levels, all soft; glow is not used.

---

## Components

**Buttons** — `.btn` plus one of `.btn-primary` (gold, one per screen),
`.btn-danger` (destructive), `.btn-ghost` (tertiary), `.btn-assist`
(violet, AI actions). Sizes `.btn-lg` / default / `.btn-sm`.

**Badges** — `.badge` plus `-ok` / `-pending` / `-error` / `-action`.
`.badge-status` is deliberately neutral: it carries the status *text*, so
colouring it would double-encode the same fact.

**Cards** — `.project-card` (media summary), `.shot-card` (list row),
`.cast-card` (portrait + detail), `.take` (selectable media).

**Inputs** — default / hover / focus (`--focus` ring) / error
(`.has-error`) / disabled. Forms are disabled, never removed: a page that
hides a whole form also hides its CSRF field.

**Choice controls are chips, never `<select>`.** A native select's popup
list is painted by the operating system; CSS reaches the closed control
and stops. (Chromium 135 added `appearance: base-select`, but one engine
is not a design.) For a handful of options a dropdown was the wrong
control anyway: every choice is visible at once and picking one is a
single tap. Real radios underneath, so field names, POST bodies,
arrow-key navigation and screen-reader semantics are the browser's job.

---

## Layout

Desktop is a single measured column, max 1000px (1240px on `.wide`).
There is no permanent sidebar; the budget collapses inline instead.

Mobile (≤760px): the header's nav hides and four workflow destinations
become a fixed tab bar; the primary action also rides in a sticky footer
above it; the storyboard's take strip scrolls horizontally inside its own
row rather than making the page scroll.

---

## Hard constraints

**No inline styles, anywhere.** The app sends `style-src 'self'` with no
`'unsafe-inline'`, so a `style=""` attribute is silently discarded by the
browser. The progress bars and the budget meter both shipped broken this
way and read empty for their entire life. Per-element values are
expressed as classes (`.fill-N`, 5% steps). Two tests guard this: one
that no template carries an inline style, one that the CSP keeps
forbidding them.

**Screenshots before "done".** The placeholder collapse, the Korean
word-break, the CSP bug and the baked-in letterbox on fake takes were all
found by looking, never by reading source. `tests/e2e/test_fable_ui_responsive.py`
covers the layout invariants; the rest is eyes.
