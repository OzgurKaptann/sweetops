# SweetOps Operations Runbook

Step-by-step procedures for running, verifying, inspecting, and recovering a
**local** SweetOps stack. SweetOps is **not yet a hosted production deployment**;
everything here targets a developer machine running Docker Compose.

Companions: [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) (why) and
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) (what to tick before merging).

> ### Read this first
>
> **The test suite and your development database are the same PostgreSQL
> database.** Run the backend tests **before** seeding demo data. With demo data
> resident, roughly two dozen tests fail for reasons that are not regressions —
> the migration downgrade tests correctly refuse to downgrade store-scoped
> inventory while a second store holds stock. Correct order:
>
> **migrate → test → seed → demo.**

---

## 1. Start the Local Stack

```bash
docker ps                       # is anything already running?
docker ps -a                    # does the container already exist, stopped?
docker start sweetops_db        # prefer starting the EXISTING container
```

Full stack (API + PostgreSQL + Redis, and the legacy Metabase/dbt services):

```bash
docker-compose up -d
docker-compose ps
docker-compose logs -f api      # follow API logs
```

Services and ports: PostgreSQL `5432`, Redis `6379`, API `8000`, Metabase `3100`.

Stop without destroying anything:

```bash
docker-compose stop             # keeps containers and volumes
docker stop sweetops_db         # or just the database
```

> **Never** use `docker-compose down -v` unless you are deliberately resetting
> local development from scratch. The `-v` flag deletes the `pgdata` volume and
> every row in it.

If Docker Desktop itself is not running, start it and wait for the daemon before
retrying — `docker ps` failing with a pipe/socket error means the daemon, not the
container, is down.

---

## 2. Migrate the Database

```bash
cd apps/api

python -m alembic heads          # expect exactly ONE line ending "(head)"
python -m alembic current        # where this database actually is
python -m alembic upgrade head
python -m alembic history --verbose
```

From the repository root, the static single-head check (no database needed):

```bash
python scripts/verify_release_readiness.py
```

Two heads means the history diverged — resolve it with an explicit merge
revision. See [ALEMBIC_SINGLE_HEAD_RESOLUTION.md](ALEMBIC_SINGLE_HEAD_RESOLUTION.md).

**Never bypass migrations.** Do not hand-edit the schema and do not let
SQLAlchemy create tables; the migration history is the schema's source of truth.

Downgrades deliberately refuse when data exists that the older schema cannot
represent — see PRODUCTION_READINESS §7. That is protection. Do not force it.

---

## 3. Seed Demo Data

```bash
npm run seed:demo                    # or: python scripts/seed_demo_data.py
python scripts/seed_demo_data.py     # run twice — it must duplicate nothing
```

Deterministic, idempotent, demo-scoped, non-destructive. It creates local demo
accounts sharing a published password — see [DEMO_SEED_DATA.md](DEMO_SEED_DATA.md).

**Never run it against a production database.**

---

## 4. Run Each App

```bash
npm install                # once, from the repository root

npm run dev:customer       # customer-web -> http://localhost:3001
npm run dev:kitchen        # kitchen-web  -> http://localhost:3002
npm run dev:owner          # owner-web    -> http://localhost:3003
npm run dev:cashier        # cashier-web  -> http://localhost:3004
```

Each in its own terminal. The API is served at `http://localhost:8000` by the
`api` Compose service.

If a frontend cannot reach the API, check in this order: the API container is
up; `NEXT_PUBLIC_API_BASE_URL`; and — for anything authenticated — that the
frontend's origin is in `STAFF_TRUSTED_ORIGINS` or `PUBLIC_TRUSTED_ORIGINS`.
Cookie auth uses an explicit credentialed allow-list, so a missing origin
produces a CORS or origin-check rejection, not a network error.

---

## 5. Run Full Verification

From the repository root:

```bash
git diff --check
python -m compileall apps/api/app scripts

npm run build:types
npm run build:ui

npm run test --workspace=customer-web
npm run test --workspace=cashier-web
npm run test --workspace=owner-web
npm run test --workspace=kitchen-web

npm run build --workspace=customer-web
npm run build --workspace=kitchen-web
npm run build --workspace=owner-web
npm run build --workspace=cashier-web

python scripts/verify_release_readiness.py
```

From `apps/api` — **before seeding**:

```bash
python -m alembic heads
python -m alembic upgrade head
python -m pytest -q --collect-only
python -m pytest -q                  # expect: 0 failed
```

Then, from the repository root:

```bash
python scripts/seed_demo_data.py
python scripts/seed_demo_data.py     # idempotency

python scripts/reconcile_kitchen_timing.py
python scripts/reconcile_payments.py
python scripts/reconcile_inventory.py
python scripts/reconcile_order_issues.py
```

Useful pytest narrowing while diagnosing:

```bash
python -m pytest -q tests/test_payment_refunds.py          # one file
python -m pytest -q -k "shift and close"                   # by name
python -m pytest -q -x --lf                                # stop at first, last-failed
python -m pytest -q tests/test_main.py::test_public_menu_shape
```

A test that fails in the full run but passes in isolation is almost always
database state, not a defect — see §10.

---

## 6. Run the Reconcilers

All four are **read-only**. They re-derive stored summaries from the append-only
ledgers and report drift. None of them writes, and none can "fix" anything.
Exit `0` = clean, `1` = at least one mismatch.

```bash
python scripts/reconcile_payments.py
python scripts/reconcile_payments.py --store 1
python scripts/reconcile_payments.py --json

python scripts/reconcile_inventory.py
python scripts/reconcile_inventory.py --store-id 2
python scripts/reconcile_inventory.py --ingredient 3
python scripts/reconcile_inventory.py --all        # include matching rows

python scripts/reconcile_order_issues.py --store 1
python scripts/reconcile_kitchen_timing.py --store 1
```

**If a reconciler reports a mismatch:** the immutability triggers make drift
unrepresentable through the application, so a mismatch means direct-SQL tampering
or a maths regression. Investigate. **Do not "correct" the ledger** — that is
exactly the action reconciliation exists to detect.

---

## 7. Inspect Cashier Shifts

Preferred: `python scripts/reconcile_payments.py`, which checks every closed
shift's frozen snapshot against a fresh re-derivation of its own window.

Read-only SQL for a closer look:

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT id, store_id, cashier_user_id, status, opened_at, closed_at,
         opening_amount, counted_amount, expected_cash_amount, discrepancy_amount
  FROM cashier_shifts ORDER BY opened_at DESC LIMIT 20;"
```

Open shifts only:

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT id, store_id, cashier_user_id, opened_at
  FROM cashier_shifts WHERE status = 'OPEN' ORDER BY opened_at;"
```

A closed shift is guarded by a trigger that refuses UPDATE and DELETE. If a
snapshot looks wrong, that is a finding to investigate — not something to edit.
See [CASHIER_SHIFT_CLOSING.md](CASHIER_SHIFT_CLOSING.md).

---

## 8. Inspect Order Issues

Preferred: `python scripts/reconcile_order_issues.py`.

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT id, store_id, order_id, status, refund_id, created_at, resolved_at
  FROM order_issues ORDER BY created_at DESC LIMIT 20;"
```

Issues and their refunds together:

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT i.id AS issue_id, i.status, i.order_id, r.id AS refund_id, r.amount
  FROM order_issues i
  LEFT JOIN payment_refunds r ON r.order_issue_id = i.id
  ORDER BY i.created_at DESC LIMIT 20;"
```

Refunds are bounded by the allocation they are issued against. A resolved issue
is immutable. See [ORDER_ISSUE_REFUND_WORKFLOW.md](ORDER_ISSUE_REFUND_WORKFLOW.md).

---

## 9. Inspect Inventory Mismatches

Preferred, and authoritative:

```bash
python scripts/reconcile_inventory.py --all
python scripts/reconcile_inventory.py --store-id 1 --json
```

It compares each `ingredient_stock` summary row against the sum of its movement
ledger. Read-only cross-checks:

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT store_id, ingredient_id, on_hand_quantity, reserved_quantity,
         available_quantity, reorder_level
  FROM ingredient_stock WHERE store_id = 1 ORDER BY ingredient_id;"
```

Recent movements for one ingredient:

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT id, store_id, ingredient_id, movement_type, quantity, created_at
  FROM ingredient_stock_movements
  WHERE ingredient_id = 3 ORDER BY created_at DESC LIMIT 30;"
```

Stock is **store-scoped**: one catalog ingredient has an independent quantity per
store, so always filter by `store_id`. A quantity that looks wrong for "the"
ingredient is usually the other store's row.

The movement ledger is append-only and trigger-protected. **Never UPDATE or
DELETE a movement to make a summary match** — the summary is derived from the
ledger, not the other way round. See
[INVENTORY_LIFECYCLE.md](INVENTORY_LIFECYCLE.md) and
[STORE_SCOPED_INVENTORY.md](STORE_SCOPED_INVENTORY.md).

---

## 10. Recover From an Interrupted Test Run

Test fixtures clean up after themselves on normal teardown, including on
failure. An **interrupted** run (Ctrl-C, crash, closed terminal) skips teardown
and leaves rows behind.

### Step 1 — make sure nothing is still running

```bash
# Windows
tasklist | findstr /I "pytest python node"
# macOS / Linux
ps aux | grep -E "pytest|next dev"
```

Never diagnose database state while a test process is still writing to it.

### Step 2 — just re-run the suite first

```bash
cd apps/api && python -m pytest -q
```

Most debris is inert: fixtures generate unique names per test, so leftovers do
not collide with the next run. If the suite is green, **stop here and clean
nothing.**

### Step 3 — if tests still fail, identify the actual cause

The single most common cause is not debris at all — it is **resident demo seed
data**. Check:

```bash
docker exec sweetops_db psql -U sweetops -d sweetops_db -c \
  "SELECT id, name FROM stores ORDER BY id;"
```

If you see `SweetOps Demo - Kadıköy` / `SweetOps Demo - Moda`, that is the cause.
Every failure will be in a test file that sorts alphabetically **before**
`test_seed_demo_data.py`, because that test's teardown removes the demo stores
part-way through the run. Re-running the suite once removes the demo data as a
side effect and the second run comes back green.

### Step 4 — inspect debris before touching it

```bash
# How many users look like pytest fixtures vs. anything else?
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT CASE WHEN username ~ '^user_[0-9a-f]{10}$'
              THEN 'pytest-fixture' ELSE 'OTHER' END AS kind, count(*)
  FROM users GROUP BY 1;"

# List anything that is NOT a fixture user — this must be reviewed by hand.
docker exec sweetops_db psql -U sweetops -d sweetops_db -c "
  SELECT id, username, store_id, created_at FROM users
  WHERE username !~ '^user_[0-9a-f]{10}$' ORDER BY id;"
```

`make_staff` in `apps/api/tests/conftest.py` names fixture users
`user_<10 hex chars>`. A row matching that pattern is provably test-created. A
row that does **not** match is not debris — leave it alone.

### Step 5 — clean only what you have proven is test-only

Only if debris is actually blocking a run. Preconditions:

- No test process is running.
- Every row you are about to delete matched the fixture pattern.
- **No demo store is affected** — the demo dataset is not debris.
- You have written down exactly what you are removing.

Deletion must follow foreign-key order (sessions → shifts → users), and the
ledger, inventory, shift, and issue tables are append-only behind triggers with
no runtime bypass. If cleanup requires disabling a trigger, **stop** — that is
the ownership-gated escape hatch that exists for fixture teardown, and reaching
for it by hand means the safer option below is the right one.

### Step 6 — the safe reset, when local data is genuinely disposable

If the local database holds nothing you need — no demo data you care about, no
manual scenario you built — a clean rebuild is safer and faster than
hand-deleting rows:

```bash
docker-compose down -v          # DESTROYS the pgdata volume — deliberate reset only
docker-compose up -d
cd apps/api && python -m alembic upgrade head && cd ../..
cd apps/api && python -m pytest -q && cd ../..
npm run seed:demo
```

This is the **only** situation in which `-v` is appropriate, and it is an
explicit decision, never a reflex.

---

## 11. Handle a Dirty Local Test Database Safely

A decision table:

| Symptom | Do this |
| --- | --- |
| Tests fail in bulk, files sorting before `test_seed_demo_data.py` | Demo data is resident. Re-run the suite once (§10 step 3). |
| One test fails in the full run, passes alone | Ordering/state interaction. Diagnose it — do not skip it. |
| Suite green, thousands of leftover fixture rows | Harmless. Leave them, or reset deliberately (§10 step 6). |
| Reconciler reports a mismatch | Investigate as a real finding (§6). Never edit the ledger. |
| Migration downgrade refuses | Working as designed. Data exists the old schema cannot hold. |
| Database will not start | Check `docker ps -a` and `docker-compose logs postgres`. Start the existing container; do not recreate it. |

Principles:

1. **Inspect before you delete.** Always run a `SELECT` before a `DELETE`.
2. **Prove it is test-only.** A regex-matched fixture name is proof; a hunch is not.
3. **Preserve demo and seed data.** It is a deliverable, not debris.
4. **Never touch non-demo stores.** Store 1 and any store you created by hand are
   off limits to cleanup.
5. **Prefer a deliberate full reset** over surgical deletes across FK-linked,
   trigger-protected tables.
6. **Write down what you removed**, and put it in the PR or the release notes.

---

## 12. What Not To Do

- ❌ **Do not delete Docker volumes** (`docker-compose down -v`, `docker volume rm`)
  unless you are intentionally resetting local development and have accepted the
  data loss.
- ❌ **Do not wipe arbitrary data.** No `TRUNCATE`, no unscoped `DELETE FROM`.
- ❌ **Do not clean non-demo stores.** Store 1 and any hand-built store are not
  debris.
- ❌ **Do not bypass migrations.** No hand-edited schema, no
  `Base.metadata.create_all`, no editing a migration that has already merged.
- ❌ **Do not mark a failing test `xfail`, skip, or delete it to go green.** A
  failure is a real defect or a known state problem; both must be understood.
- ❌ **Do not edit a ledger to make a reconciler pass.** The reconciler is right;
  that is its whole job.
- ❌ **Do not disable an immutability trigger by hand.** The ownership-gated
  escape hatch exists for fixture teardown only.
- ❌ **Do not run the demo seed against a production database.** It creates
  accounts with a published password.
- ❌ **Do not commit a real secret.** Only `*.env.example` files are tracked, and
  they hold placeholders. Run
  `python scripts/verify_release_readiness.py` before pushing.
- ❌ **Do not recreate containers** when the existing one can simply be started.
- ❌ **Do not leave background pytest / npm / uvicorn processes running** — they
  hold database connections and corrupt the next run's state.
- ❌ **Do not set `ALLOW_LEGACY_ORDER_CONTEXT=true`** outside the test suite. It
  makes the API trust client-supplied store/table context.
- ❌ **Do not set `ALLOW_MISSING_WEBSOCKET_ORIGIN=true`** outside an isolated
  non-browser test setup. It removes the cross-site WebSocket hijacking defence.
- ❌ **Do not use `*` in a trusted-origin list.** Cookie auth requires exact
  origins.
