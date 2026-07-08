# customer-web production build fix — `useSearchParams()` Suspense boundary

## 1. Original failure

Command:

```bash
npm run build --workspace=customer-web
```

TypeScript compiled successfully; the build then failed during static
prerendering:

```text
✓ Compiled successfully
  Generating static pages using 26 workers (0/5) ...
⨯ useSearchParams() should be wrapped in a suspense boundary at page "/".
  Read more: https://nextjs.org/docs/messages/missing-suspense-with-csr-bailout
Error occurred prerendering page "/".
Export encountered an error on /page: /, exiting the build.
⨯ Next.js build worker exited with code: 1
```

Affected file/hook: `apps/customer-web/src/app/page.tsx` calling
`useSearchParams()` (and `useRouter()`) from `next/navigation`.

## 2. Root cause

`page.tsx` was the route entry component **and** a Client Component
(`"use client"`) that called `useSearchParams()` at the top level. During
`next build`, Next.js still attempts to statically prerender `/`. When
`useSearchParams()` is read without a Suspense boundary above it, the entire
page must "bail out" to client-side rendering — which is disallowed during
static export and aborts the build.

Next.js requires a `<Suspense>` boundary above any component that reads
`useSearchParams()` so it can render the fallback into the static HTML shell and
resolve the real search params on the client.

## 3. Previous component structure

```text
src/app/page.tsx  ("use client")
  └─ default export Home()   ← calls useSearchParams() + useRouter() directly
       ├─ QuickStartSection / PopularSection / UpsellPanel / IngredientChip
       └─ menu fetch, selection state, order submit
```

The whole page — param reading, data fetching, and all UI helpers — lived in a
single client module with no Suspense boundary.

## 4. New component structure

```text
src/app/page.tsx                          (Server Component)
  └─ <Suspense fallback={<CustomerMenuLoadingFallback/>}>
        └─ <CustomerMenuPageClient/>       ← "use client"
              ├─ useSearchParams() / useRouter()
              ├─ QuickStartSection / PopularSection / UpsellPanel / IngredientChip
              └─ menu fetch, selection state, order submit
```

- `src/app/page.tsx` — now a Server Component. No `"use client"`. It renders a
  `<Suspense>` boundary with a static loading fallback and the client component
  inside it.
- `src/components/CustomerMenuPageClient.tsx` — new Client Component holding the
  **verbatim** previous logic (the former `Home` component and all its helper
  components), renamed to `CustomerMenuPageClient`. Zero behavioral changes.

## 5. Why Suspense is required

`useSearchParams()` depends on request/URL state that is only known on the
client during static export. Next.js opts a subtree that reads it out of static
prerendering by suspending it. Without a boundary the suspension propagates to
the route root and the whole page becomes client-only, which is not allowed for
a statically exported page — hence the build error. A `<Suspense>` boundary
scopes the client-only bailout: the fallback is prerendered statically, and the
param-dependent client subtree resolves after hydration.

## 6. Why this is preferable to forcing dynamic rendering

`export const dynamic = "force-dynamic"` would also silence the error, but it
would opt the route out of static generation and force server rendering on every
request purely to hide a boundary requirement — a heavier, less correct change.
The Suspense + client-split pattern is the officially recommended App Router
approach: it keeps `/` statically prerendered (confirmed by the build output
`○ (Static) prerendered as static content`), preserves the existing
client-side data-fetching flow, and changes nothing about rendering semantics
beyond adding the required boundary.

## 7. URL parameter behavior preserved

`CustomerMenuPageClient` keeps the identical param logic:

```ts
const storeId = Number(searchParams?.get("store") || 1);
const tableId = searchParams?.get("table") ? Number(searchParams.get("table")) : undefined;
```

- **Valid store & table** (`/?store=2&table=5`): `store_id`/`table_id` sent to
  `POST /public/orders/` exactly as before; menu loads via `GET /public/menu/`.
- **Missing store** (`/?table=5`): unchanged existing behavior — falls back to
  `store=1` default that already existed in the original code (no new default
  invented).
- **Missing table** (`/?store=2`): `tableId` stays `undefined` and is sent as
  such; the "Masa {tableId}" header is hidden — unchanged.
- **Missing both** (`/`): same as before — `store` defaults to the pre-existing
  `1`, `table` is `undefined`; page renders the safe loading→menu flow with no
  crash and no malformed request.
- **Client-side URL changes**: still handled by `useSearchParams()` (a client
  hook), so reactive param changes behave exactly as before.

The API request/response contract in `src/lib/api.ts` is untouched.

## 8. Verification commands and results

| Command | Result |
| --- | --- |
| `npm run build --workspace=customer-web` (before fix) | FAIL — reproduced the `useSearchParams()` suspense error |
| `npm run build --workspace=customer-web` (after fix) | PASS — `/` prerendered as static content |
| `npm run build:types` | PASS |
| `npm run build:ui` | PASS |
| `npm run build --workspace=kitchen-web` | PASS |
| `npm run build --workspace=owner-web` | PASS |
| `git grep -n "useSearchParams" -- apps/customer-web` | Only under Suspense (`CustomerMenuPageClient`, and pre-existing `success/page.tsx`) |
| `git diff --check` | Clean |
| `pytest -q --collect-only` (apps/api) | PASS — 267 tests collected, no errors |

## 9. Files changed

- `apps/customer-web/src/app/page.tsx` — converted to a Server Component with a
  Suspense boundary + static fallback.
- `apps/customer-web/src/components/CustomerMenuPageClient.tsx` — new Client
  Component containing the previous page logic verbatim.
- `docs/CUSTOMER_WEB_SUSPENSE_BUILD_FIX.md` — this document.

## 10. Remaining risks

- **No JS component test framework** exists in `customer-web` or the repo root.
  Per scope, none was introduced for this small fix; correctness is covered by
  the passing production build (which now exercises static prerendering of `/`),
  TypeScript, and the targeted `useSearchParams`/`Suspense` greps.
- A **pre-existing** ESLint `react/no-unescaped-entities` error on the literal
  `Waffle'ını` (Turkish apostrophe) moved verbatim from `page.tsx` into
  `CustomerMenuPageClient.tsx`; it also already exists in `success/page.tsx`. It
  does not gate `next build` and is unrelated to this fix, so it was left
  untouched to preserve behavior exactly.
