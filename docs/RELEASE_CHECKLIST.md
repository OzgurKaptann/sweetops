# SweetOps Release Checklist

Work through this before merging a release branch. It is a *release-candidate
readiness* checklist — SweetOps is **not yet a hosted production deployment**, so
the deployment and post-merge sections describe what would be required rather
than what an existing pipeline does.

Companion documents: [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) (why each
step exists) and [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md) (how to run and
recover things).

> **Ordering rule that catches people out:** run the backend test suite **before**
> seeding demo data. Tests and local development share one database, and resident
> demo data makes roughly two dozen tests fail for reasons that are not
> regressions. See §5 and PRODUCTION_READINESS §9.

---

## 1. Branch State

- [ ] On the intended branch: `git branch --show-current`
- [ ] Working tree clean: `git status --short` prints nothing
- [ ] Branched from an up-to-date `main`: `git log -12 --oneline --decorate`
- [ ] The expected prior work is present in history (no missing merge)
- [ ] No stray files staged: `git diff --cached --stat`
- [ ] No whitespace or conflict-marker damage: `git diff --check`
- [ ] Scope matches the branch's stated purpose — nothing unrelated smuggled in

---

## 2. Migration State

- [ ] Exactly one head: `cd apps/api && python -m alembic heads`
- [ ] Static confirmation, no database needed:
      `python scripts/verify_release_readiness.py`
- [ ] Database is at that head: `python -m alembic current`
- [ ] `python -m alembic upgrade head` runs clean
- [ ] If this branch adds a migration:
  - [ ] It is a new revision, not an edit to a merged one
  - [ ] `downgrade()` is implemented, or it refuses explicitly and says why
  - [ ] A `test_*_migration.py` covers the round trip
  - [ ] The downgrade caveat is documented in PRODUCTION_READINESS §7
- [ ] If this branch adds **no** migration, confirm none was needed

---

## 3. Environment Config

- [ ] `.env.example` and `apps/api/.env.example` reflect reality — every variable
      the code reads is listed, and nothing invented
- [ ] Example files contain **placeholders only**; no real credential
- [ ] No `.env` / `.env.local` file is tracked: `git ls-files | grep -E '\.env'`
      returns only `*.env.example`
- [ ] New settings added to `app/core/config.py` are documented in
      `apps/api/.env.example` **and** in the PRODUCTION_READINESS §6 table
- [ ] Trusted-origin variables still list exact origins; never `*`
- [ ] `ALLOW_LEGACY_ORDER_CONTEXT` still defaults to `false`
- [ ] `ALLOW_MISSING_WEBSOCKET_ORIGIN` still defaults to `false`

---

## 4. Builds

From the repository root:

- [ ] `python -m compileall apps/api/app scripts`
- [ ] `npm run build:types`
- [ ] `npm run build:ui`
- [ ] `npm run build --workspace=customer-web`
- [ ] `npm run build --workspace=kitchen-web`
- [ ] `npm run build --workspace=owner-web`
- [ ] `npm run build --workspace=cashier-web`

Shared packages build first: the apps consume `@sweetops/types` and
`@sweetops/ui` from `dist/`, so a stale build hides type errors.

---

## 5. Tests

**Backend — run this before seeding.** From `apps/api`:

- [ ] `python -m pytest -q --collect-only` (collection errors surface here first)
- [ ] `python -m pytest -q` → **0 failed**

**Frontend.** From the repository root:

- [ ] `npm run test --workspace=customer-web`
- [ ] `npm run test --workspace=cashier-web`
- [ ] `npm run test --workspace=owner-web`
- [ ] `npm run test --workspace=kitchen-web`

Rules:

- [ ] No test was marked `xfail`, skipped, or deleted to make the suite pass
- [ ] Any new test is deterministic — no wall-clock races, no dependence on a
      local absolute path, no dependence on another test's leftovers
- [ ] A failure was diagnosed to a cause (real defect vs. database state), not
      re-run until green

---

## 6. Seed / Demo Verification

From the repository root, **after** the test suite:

- [ ] `python scripts/seed_demo_data.py` succeeds
- [ ] `python scripts/seed_demo_data.py` **a second time** — idempotent, nothing
      duplicated
- [ ] Non-demo stores untouched (store 1 still intact)
- [ ] Demo accounts and password match [DEMO_SEED_DATA.md](DEMO_SEED_DATA.md)

---

## 7. Reconcilers

All four are read-only and must exit `0`:

- [ ] `python scripts/reconcile_kitchen_timing.py`
- [ ] `python scripts/reconcile_payments.py`
- [ ] `python scripts/reconcile_inventory.py`
- [ ] `python scripts/reconcile_order_issues.py`

- [ ] Run **after** seeding as well — the seed drives the real services, so a
      mismatch here means the seed bypassed a ledger
- [ ] A mismatch was investigated, never "corrected" by editing the ledger

---

## 8. Security Checks

A practical readiness review, not a formal security audit.

- [ ] `python scripts/verify_release_readiness.py` → no `committed secrets`
      finding
- [ ] No new credential, token, or key in the diff: review `git diff main...HEAD`
- [ ] Any new placeholder added to the scanner's `PLACEHOLDER_VALUES` carries a
      comment justifying it
- [ ] New state-changing endpoints go through the CSRF + trusted-origin
      dependency
- [ ] New operational read endpoints set `Cache-Control: no-store`
- [ ] New endpoints derive the store from the session or the signed QR token —
      never from a client-supplied `store_id`
- [ ] No new endpoint weakens RBAC
- [ ] Ledger/audit tables stay append-only; no new runtime trigger bypass
- [ ] No credential, token, or card data added to any log or script output
- [ ] Demo credentials remain clearly local-only and demo-scoped

---

## 9. Manual Smoke Checks

Browser checks against a freshly seeded local stack. No browser automation
exists; these are done by hand.

Start: `docker-compose up -d`, migrate, seed, then run the app(s) under test.

### Customer QR ordering — `http://localhost:3001`
- [ ] A valid QR token resolves to the right store and table
- [ ] Menu renders with Turkish copy and correct prices
- [ ] An order can be placed and reaches the success confirmation
- [ ] Re-submitting the same order does not create a duplicate
- [ ] A tampered/invalid QR token is rejected

### Kitchen board + timing — `http://localhost:3002`
- [ ] Board loads with the seeded orders
- [ ] A new customer order appears live over the WebSocket
- [ ] Status transitions advance and persist through a refresh
- [ ] Timing cards show queue/prep durations; delayed orders are flagged

### Cashier shift open/close — `http://localhost:3004`
- [ ] Open a shift with an opening float
- [ ] The current shift is shown while open
- [ ] Close it with a counted amount; the discrepancy is computed
- [ ] A closed shift is immutable in the UI
- [ ] The seeded shift with a deliberate discrepancy displays correctly

### Cashier payment / refund — `http://localhost:3004`
- [ ] Open tables and bills list correctly
- [ ] Settle an order by cash and by card; the order's payment status updates
- [ ] A partial payment leaves the correct remaining balance
- [ ] Issue a refund against an allocation; it is bounded by the allocated amount
- [ ] An over-refund is refused with a clear Turkish message

### Order issue workflow
- [ ] Raise an issue against an order
- [ ] Resolve it with a controlled refund; the refund is bounded and linked
- [ ] The issue's status and its refund appear in the owner history

### Owner operational dashboard — `http://localhost:3003/`
- [ ] Today's orders, collected and refunded money match the seeded story
- [ ] Kitchen tempo, open issues, shifts, and stock alerts populate
- [ ] The attention list is deterministic across reloads
- [ ] The dashboard is read-only — no action mutates anything

### Owner inventory — `/inventory`
- [ ] Stock is store-scoped; switching store changes the quantities
- [ ] Receipt, waste, and manual adjustment each write a movement
- [ ] A transfer moves stock between stores and leaves a receipt trail
- [ ] A physical count reconciles counted vs. system stock
- [ ] Threshold alerts show every seeded state (ok / low / critical)

### Owner order issues — `/order-issues`
- [ ] History lists open and resolved issues with their refunds

### Owner shifts — `/shifts`
- [ ] Open and closed shifts list with correct totals and discrepancies

### Demo seed data
- [ ] Every surface above was meaningful immediately after seeding — no empty
      screens

---

## 10. PR Review

- [ ] Title and description state the scope, and what was deliberately excluded
- [ ] `git diff --stat main...HEAD` reviewed file by file
- [ ] No debug output, commented-out code, or stray scratch file
- [ ] No new dependency (or one that is justified in the description)
- [ ] No new product feature in a hardening/docs branch
- [ ] Docs updated alongside the code they describe
- [ ] README still accurate — every command it advertises exists and works
- [ ] Verification results pasted into the PR with PASS/FAIL per command
- [ ] Known limitations disclosed rather than glossed over

---

## 11. Merge

- [ ] Every section above is green
- [ ] Branch is up to date with `main`
- [ ] Still exactly one Alembic head after any rebase or merge
- [ ] `git diff --check` clean after any conflict resolution
- [ ] `python scripts/verify_release_readiness.py` re-run post-merge-resolution —
      it catches a conflict marker left in a file
- [ ] Merge with a message that describes the change, not the mechanics

---

## 12. Post-Merge Verification

On `main`, after merging:

- [ ] `git log -3 --oneline --decorate` shows the expected merge
- [ ] `python scripts/verify_release_readiness.py` → exit `0`
- [ ] `cd apps/api && python -m alembic heads` → still one head
- [ ] `python -m pytest -q` → 0 failed
- [ ] All four reconcilers exit `0`
- [ ] A clean-clone rehearsal for anything user-facing: clone fresh, follow the
      README quickstart verbatim, and confirm it works with no undocumented step

---

## 13. Rollback

Decide **before** merging what rollback would look like.

- [ ] Change is code-only → revert the merge commit, redeploy, re-run the
      reconcilers. No data action.
- [ ] Change includes a migration → confirm the downgrade path, **or** confirm a
      restorable backup exists. Several SweetOps downgrades deliberately refuse
      when data exists that the older schema cannot represent
      (PRODUCTION_READINESS §7) — that is protection, not a bug. Do not force it.
- [ ] Rollback trigger is written down: what symptom means "revert now"
- [ ] After any rollback: re-run all four reconcilers before reopening writes
- [ ] Locally, recovery is gentler — see
      [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md). Never reach for
      `docker-compose down -v`; it destroys the volume.
