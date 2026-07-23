---
name: release-verification
description: Run the full SweetOps release/readiness verification sweep — git status and diff --check, compileall, frontend tests and builds, Alembic heads and upgrade, the complete pytest suite, the demo seed run twice, verify_release_readiness.py, and all four reconcilers — reporting PASS/FAIL per command. Use when asked to verify a release, confirm the repo is release-ready, or run final checks before merging. It never wipes databases or deletes Docker volumes.
---

# SweetOps Release Verification

Execute the final verification sweep and report a per-command PASS/FAIL. Every
result must come from a command actually run in this session.

Authoritative companion: `docs/RELEASE_CHECKLIST.md` (and
`docs/PRODUCTION_READINESS.md` for why each step exists,
`docs/OPERATIONS_RUNBOOK.md` for recovery). Pre-PR scope review belongs to
[pr-readiness-review](../pr-readiness-review/SKILL.md).

## SweetOps context

FastAPI + PostgreSQL + Alembic in `apps/api`; four Next.js apps
(`customer-web` 3001, `kitchen-web` 3002, `owner-web` 3003, `cashier-web` 3004);
shared `packages/types` and `packages/ui`; scripts in `scripts/`. Core product:
QR ordering · kitchen flow · kitchen timing · cashier payments/refunds · cashier
shifts · order issues/refunds · inventory lifecycle · store-scoped stock ·
transfers · physical counts · threshold alerts · owner operational dashboard ·
demo seed · production readiness docs. User-facing copy is Turkish.

## Safety rules — non-negotiable

- **Never** run `docker-compose down -v`, drop a database, truncate a table,
  delete a volume, or run any "clean slate" DB reset. Local recovery is
  documented in `docs/OPERATIONS_RUNBOOK.md`; a dirty database is diagnosed, not
  destroyed.
- **Never** edit a ledger to make a reconciler pass.
- **Never** delete, skip, or `xfail` a test to reach green.
- **No background processes left running.** Anything started for verification
  (dev servers, watchers, the API) is stopped before the report. Docker
  containers started deliberately may stay up — say so explicitly.
- Verification is read-only apart from migrations and the demo seed, both of
  which are intended, documented, and non-destructive.

Do not implement anything during verification. If a check fails, diagnose and
report — fixing is a separate, explicitly requested step.

## Ordering rule

Run the backend test suite **before** seeding demo data. Tests and local
development share one database, and resident demo data makes roughly two dozen
tests fail for reasons that are not regressions. Getting this order wrong
produces a false FAIL.

## Sequence

### 1. Git state

```bash
git branch --show-current
git status --short
git diff --check
git log -5 --oneline --decorate
```

`git status --short` should print nothing; any `git diff --check` output
(whitespace damage, conflict markers) is a FAIL.

### 2. Static readiness

```bash
python scripts/verify_release_readiness.py
```

Read-only, offline, stdlib-only. Exit `0` required. It checks required docs and
scripts, a single Alembic head, doc links, merge markers, and committed secrets.
Re-run it after any conflict resolution.

### 3. Python compile

```bash
python -m compileall apps/api/app scripts
```

### 4. Frontend builds and tests

Shared packages first — the apps consume `@sweetops/types` and `@sweetops/ui`
from `dist/`, so a stale build hides type errors.

```bash
npm run build:types
npm run build:ui
npm run build --workspace=customer-web
npm run build --workspace=kitchen-web
npm run build --workspace=owner-web
npm run build --workspace=cashier-web
npm run test --workspace=customer-web
npm run test --workspace=cashier-web
npm run test --workspace=owner-web
npm run test --workspace=kitchen-web
```

### 5. Alembic heads and upgrade

```bash
cd apps/api && python -m alembic heads && python -m alembic current && python -m alembic upgrade head && cd ../..
```

Exactly one head. `upgrade head` must run clean. Requires the database up
(`docker-compose up -d`) — note it if the stack had to be started.

### 6. Full backend test suite

From `apps/api`, before seeding:

```bash
python -m pytest -q --collect-only
python -m pytest -q
```

`0 failed` required. Collection errors surface in the first command. Diagnose
any failure to a cause — real defect versus database state — and report the
cause, never a re-run count.

### 7. Demo seed, twice

From the repo root, after the tests:

```bash
python scripts/seed_demo_data.py
python scripts/seed_demo_data.py
```

The second run proves idempotence: nothing duplicated, no error. The seed is
deterministic, demo-scoped, and non-destructive — non-demo stores (store 1) must
be untouched. Demo accounts and password must match `docs/DEMO_SEED_DATA.md`.

### 8. Reconcilers

All four are read-only and must exit `0`:

```bash
python scripts/reconcile_kitchen_timing.py
python scripts/reconcile_payments.py
python scripts/reconcile_inventory.py
python scripts/reconcile_order_issues.py
```

Run them **after** seeding as well — the seed drives the real services, so a
mismatch means the seed bypassed a ledger. Investigate any mismatch; never
"correct" it.

### 9. Post-run hygiene

- No unsafe DB cleanup was performed — state this explicitly.
- List every process started during verification and confirm each was stopped.
- List any Docker container intentionally left running.
- `git status --short` clean again (the seed must not have dirtied the tree).

## Report format

A table of every command with PASS / FAIL / NOT RUN and the decisive output
line. Then:

- **Failures** — command, output, diagnosis, whether it is a real defect or
  environment/data state.
- **Not run** — with the reason (no database, no Docker, not applicable).
- **Environment** — what was started, what was left running, what was stopped.
- **Safety confirmation** — no database wiped, no volume deleted, no ledger
  edited, no test disabled, no background process left running.

End with exactly one verdict:

- **RELEASE VERIFIED** — every command run and PASS.
- **RELEASE NOT VERIFIED** — any FAIL or NOT RUN, with the shortest list of
  actions that would change the verdict.

Never report PASS for a command that was not run, and never let a partial sweep
be summarised as a clean one.
