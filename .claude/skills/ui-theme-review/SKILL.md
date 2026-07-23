---
name: ui-theme-review
description: Review and plan SweetOps visual and theme improvements — staff login screens, input contrast and placeholder readability, form hierarchy, button states, focus rings, card styling, typography, spacing, tablet usability, Turkish UX copy, and consistency across the customer, kitchen, cashier, and owner surfaces. Use when asked about SweetOps look and feel, theme, styling, contrast, accessibility, or "make the UI better". Produces a UI audit and an implementation plan unless implementation is explicitly requested.
---

# SweetOps UI & Theme Review

Review the SweetOps interface and produce an audit plus a concrete
implementation plan. **Default output is documents, not code.** Only edit
components when the user explicitly says to implement.

## SweetOps context

Four Next.js + TypeScript + Tailwind apps sharing workspace packages:

| Surface | Path | Port | Primary device |
| --- | --- | --- | --- |
| `customer-web` | `apps/customer-web` | 3001 | guest phone |
| `kitchen-web` | `apps/kitchen-web` | 3002 | wall-mounted tablet |
| `owner-web` | `apps/owner-web` | 3003 | laptop / tablet |
| `cashier-web` | `apps/cashier-web` | 3004 | counter tablet |

Key files:

- Shared primitives: `packages/ui/src/index.tsx` (`Button`, `Card`,
  `StatusBadge`). Apps consume the built `dist/` — rebuild with
  `npm run build:ui` after any change here.
- Per-app theme tokens: `apps/<app>/src/app/globals.css`.
- Staff login and session gating: `apps/owner-web/src/components/AuthGate.tsx`,
  `apps/kitchen-web/src/components/AuthGate.tsx`,
  `apps/cashier-web/src/components/AuthGate.tsx` — three near-duplicate
  `LoginScreen` implementations. Note that the credential field is **username**,
  not email; refer to it correctly in Turkish copy (`Kullanıcı adı`, `Şifre`).

All customer- and staff-facing copy is Turkish — see
`docs/TURKISH_USER_FACING_LOCALIZATION.md`.

## Boundaries

Visual and copy layer only. Do **not** change application logic, API contracts,
schema, tests, or dependencies. Do **not** introduce a CSS framework, component
library, icon package, or font package — a new dependency needs its own branch
and the user's explicit approval. Work with Tailwind and the existing
`packages/ui` primitives.

Do not casually implement: forecasting · supplier management · purchase orders ·
new schema · new dependencies · payment redesign · inventory redesign · shift
redesign.

## Recommended visual direction

Modern warm waffle-shop SaaS theme:

```text
cream background
cocoa/slate text
amber accent
green success
red danger
clear input borders
strong focus rings
consistent cards
tablet-friendly staff screens
```

Read that as intent, not as a fixed palette. Propose concrete token values, keep
the set small, and define them once as CSS variables in `globals.css` so all
four surfaces can converge. Every proposed pair must pass WCAG AA (4.5:1 body
text, 3:1 large text and UI borders) — state the measured ratio in the audit;
do not assert "accessible" without a number.

The current state to improve on: default Next.js white/near-black tokens plus a
`prefers-color-scheme: dark` block that no surface actually designs for, an
indigo/blue accent in the shared `Button` and login screens, and grey-100/300
borders that read as invisible on a bright tablet.

## Review checklist

For each item: what it looks like now (file + line), why it fails, what to
change, and how much work it is.

### Staff login screens
The first screen every staff member sees, three times duplicated. Check brand
presence, form width on a tablet, vertical centring, error and
session-expired notices, the disabled/submitting button state, and whether the
three copies have drifted apart. Converging them into one shared component is a
legitimate recommendation — flag it as a refactor with its own risk.

### Credential input contrast
Border weight and colour against the card and the page, text colour, disabled
and read-only states, autofill background override, caret visibility, and touch
target height (44px minimum on a tablet).

### Placeholder readability
Placeholders must not be the only label, must not be mistaken for a filled
value, and must clear contrast on the input background. Prefer a visible label
plus a genuinely helpful placeholder, or no placeholder at all.

### Form hierarchy
Label → input → hint → error order, spacing rhythm, required marking, grouping,
and one unmistakable primary action per form.

### Button states
Every variant (`primary`, `secondary`, `danger`, `success`) across default,
hover, active, focus-visible, disabled, and loading. Destructive actions
(refund, waste, cancel, close shift) must be visually distinct from routine
ones and must not sit adjacent to the primary action.

### Focus rings
Keyboard focus must be visible on every interactive element, on every
background, with a ring offset so it is not swallowed by the control's own
border. Never remove an outline without replacing it.

### Card styling
One card treatment: radius, border, shadow, padding, header/body/footer rhythm.
Today `packages/ui` `Card` and per-app ad-hoc `div`s disagree — inventory it.

### Typography
Type scale (page title, section, card title, body, caption, numeric), weights,
line height, and tabular numerals for money and quantities. Turkish text runs
longer than English — check wrapping and truncation with real strings.

### Spacing
A single spacing scale, consistent page gutters, section rhythm, and grid gaps.
Flag one-off pixel values.

### Tablet usability
Staff screens are used standing, in a hurry, sometimes with wet or gloved hands.
Check touch targets, thumb reach, landscape layout at 1024×768 and 1280×800,
scroll traps, sticky headers/action bars, glare-resistant contrast, and that no
critical action hides below the fold.

### Turkish UX copy
Correct Turkish characters and casing (the dotted/dotless i), consistent
terminology for the same concept across surfaces, imperative button verbs,
polite and specific error messages, honest empty states, and number/currency/
date formatting (`tr-TR`, `₺`). Never mix English fragments into user-facing
strings.

### Cross-surface consistency
Same status vocabulary and colour for the same order state everywhere; same
header, logout affordance, loading and error patterns; a shared identity across
customer/kitchen/cashier/owner while allowing each surface its own density —
kitchen is glanceable at distance, cashier is dense and fast, owner is
information-rich, customer is calm and generous.

## Deliverables

Default (no explicit implement request):

1. **`docs/UI_THEME_AUDIT_<YYYY-MM-DD>.md`** — findings per checklist section,
   each with file reference, current state, problem, severity, and proposed
   fix; a proposed token table with contrast ratios; and a screen-by-screen
   inventory of the four surfaces.
2. **`docs/UI_THEME_IMPLEMENTATION_PLAN.md`** — phased plan, each phase a
   candidate branch:
   - Phase 1 — tokens in `globals.css`, no component changes.
   - Phase 2 — shared primitives in `packages/ui` (Button, Card, Input, focus).
   - Phase 3 — staff login convergence and form hierarchy.
   - Phase 4 — per-surface density, tablet layout, copy pass.
   Each phase lists files touched, verification commands, rollback, and an
   explicit "not in this branch" list.

If asked to implement: do one phase per branch, keep the diff to styling, and
verify with

```bash
npm run build:ui
npm run build --workspace=<app>
npm run test --workspace=<app>
```

then re-check the changed screens by hand in a browser. Report what you looked
at and what you did not.
