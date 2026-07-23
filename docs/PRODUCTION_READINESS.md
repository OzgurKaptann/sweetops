# SweetOps Production Readiness

> **Status: release-candidate readiness.** SweetOps is **not yet a hosted
> production deployment**. This document is a *production-readiness checklist*
> for a system that runs, verifies, and demos reliably on a developer machine —
> not a record of a live deployment, and not a formal security audit.

---

## 1. Purpose and Scope

### Purpose

This document is the single place a reviewer, a new contributor, or a future
operator can go to answer one question:

> *Can I run this system, prove it works, demo it, and understand what would
> still be required before putting it in front of real money and real stock?*

It gathers the setup, migration, seeding, verification, reconciliation, security,
deployment and rollback procedures that were previously spread across the README
and the per-subsystem workflow docs, and it states plainly what is **not** done.

### Scope

**In scope:** everything needed to run and verify the repository locally —
environment configuration, the migration workflow, demo data, the test suite, the
reconcilers, and a practical review of the repository's security posture.

**Out of scope:** hosting, TLS termination, managed database provisioning,
secret management infrastructure, CI/CD pipelines, monitoring/alerting, backup
scheduling, load testing, and formal security certification. None of these exist
in the repository, and this document does not pretend otherwise — see
[§13 Known Non-Production Limitations](#13-known-non-production-limitations).

### Related documents

| Document | Use it for |
| --- | --- |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | The tick-list to work through before merging a release branch |
| [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md) | Step-by-step commands for day-to-day operation and recovery |
| [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md) | What is implemented, what is next, what is deferred |
| [TEST_SUITE_BASELINE.md](TEST_SUITE_BASELINE.md) | What the backend suite covers and why it is shaped that way |
| [DEMO_SEED_DATA.md](DEMO_SEED_DATA.md) | The demo dataset, its accounts, and its safety model |

---

## 2. Current Release State

### What exists

SweetOps is a store-scoped restaurant operations system. Every capability below
maps to code, migrations, and tests in this repository:

- Secure QR table context; idempotent customer order creation.
- Kitchen order lifecycle with real-time WebSocket updates, and kitchen
  preparation timing metrics derived from the order status-event log.
- Cookie-based staff authentication with role-based access control.
- Payment settlement ledger with allocations and bounded, traceable refunds.
- Order issue workflow with controlled, issue-driven refunds.
- Cashier shift opening and closing with auditable, frozen snapshots.
- Store-scoped inventory across its full lifecycle: reservation/consumption,
  purchase receipts, waste, manual adjustments, inter-store transfers, physical
  stock counts, and threshold alerts.
- Owner operational dashboard — a read-only aggregate of today's operation.
- Turkish customer- and staff-facing copy throughout.
- Deterministic, idempotent, demo-scoped seed data.
- Four read-only reconcilers (payments, inventory, order issues, kitchen timing).

### What "release candidate" means here

| Dimension | State |
| --- | --- |
| Functional completeness (MVP scope) | Complete |
| Backend automated tests | Comprehensive; full suite green on a clean database |
| Migration history | Single head, forward-and-back tested |
| Frontend builds | All four Next.js apps build |
| Data integrity | Append-only ledgers with database-level immutability triggers; four independent reconcilers |
| Reproducible demo | One command, deterministic and idempotent |
| Documentation | This document, a release checklist, an operations runbook, and per-subsystem workflow docs |
| Hosted deployment | **Does not exist** |
| CI pipeline | **Does not exist** — verification is run manually (§9) |
| Monitoring / alerting / backups | **Do not exist** |
| Secret management | **Local `.env` files only** |

### Verification entry points

```bash
# Repository-state check — read-only, no database, no network.
python scripts/verify_release_readiness.py

# The real verification suite — see §9.
cd apps/api && python -m pytest -q
```

`scripts/verify_release_readiness.py` checks that the repository itself is
reviewable: the required docs and scripts exist, the package scripts the README
advertises exist, the Alembic history has exactly one head, every relative
markdown link resolves, no merge conflict marker survived a merge, and no obvious
secret was committed. It **does not replace** pytest, the migrations, the
frontend builds, or the reconcilers — it is a cheap pre-flight check, nothing
more. See [§5 Verification Script](#5-verification-script) below and
[OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md).

---

## 3. Local Development Setup

**Prerequisites:** Docker + Docker Compose, Node.js with npm workspaces, Python
3.12 with the backend requirements installed (`apps/api/requirements.txt`).

```bash
# 1. Start PostgreSQL (and the rest of the local stack).
docker-compose up -d

# 2. Install JavaScript workspace dependencies.
npm install

# 3. Configure the backend.
cp .env.example .env                       # optional: documents the stack
cp apps/api/.env.example apps/api/.env     # recommended

# 4. Apply migrations.
cd apps/api && python -m alembic upgrade head && cd ../..

# 5. Seed the demo dataset (recommended — see §4).
npm run seed:demo

# 6. Start the surfaces you need, each in its own terminal.
npm run dev:customer   # http://localhost:3001
npm run dev:kitchen    # http://localhost:3002
npm run dev:owner      # http://localhost:3003
npm run dev:cashier    # http://localhost:3004
```

The API is served at `http://localhost:8000` by the `api` Compose service.

> **Order matters.** Run migrations before seeding, and — importantly — run the
> **backend test suite before seeding**, not after. See the warning in §9.

---

## 4. Demo Setup

```bash
npm run seed:demo          # or: python scripts/seed_demo_data.py
```

One command populates a coherent Turkish waffle-shop demo so that every surface
is meaningful immediately: active/waiting/in-prep/ready/completed and cancelled
orders, live kitchen-timing warnings, cash/card/partial payments, direct and
issue-driven refunds, open and resolved issues, open and closed shifts (one with
a deliberate discrepancy), every inventory threshold state, and stock
receipts/waste/adjustments/transfers/counts.

Properties, in full detail in [DEMO_SEED_DATA.md](DEMO_SEED_DATA.md):

- **Deterministic** — no random values.
- **Idempotent** — safe to run repeatedly; a rerun replays rather than duplicates.
- **Demo-scoped** — confined to the demo stores; store 1 and any other non-demo
  store are never mutated.
- **Non-destructive** — it only creates and upserts; it deletes nothing.
- **Ledger-honest** — every stock change goes through the inventory service and
  every money movement through the payment ledger, so the reconcilers stay green.

> ⚠️ **Development/demo only.** The demo staff accounts share one published
> password (`demo1234`, documented in [DEMO_SEED_DATA.md](DEMO_SEED_DATA.md)).
> They are local demo credentials, not secrets, and the script must never be run
> against a production database.

The manual browser smoke checks that exercise this dataset are in
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

---

## 5. Verification Script

`scripts/verify_release_readiness.py` is read-only: it opens files for reading
and does nothing else — no writes, no database connection, no network, no Docker,
and no new dependency (Python standard library only).

| Check | What it proves |
| --- | --- |
| `git` | Reports the current branch; falls back to a filesystem scan when the root is not a git checkout (never fails the run) |
| `required docs` | The runbooks and workflow docs a reviewer is pointed at all exist |
| `required scripts` | The seed, reconcilers, and management CLIs the README advertises all exist |
| `env examples` | `.env.example` and `apps/api/.env.example` are present |
| `package scripts` | Every `npm run` command the README advertises is defined in `package.json` |
| `alembic single head` | The revision graph in `apps/api/alembic/versions/` has exactly one head |
| `doc links` | Every relative markdown link in `README.md` and `docs/*.md` resolves on disk |
| `merge markers` | No unresolved conflict marker survived a merge |
| `committed secrets` | No high-signal credential shape, and no non-placeholder literal assigned to a secret-ish name |

Exit code `0` when every check passes, `1` on any failure. `--json` gives
machine-readable output; `--root PATH` verifies a different directory.

**The Alembic check is static.** It rebuilds the revision graph from the
migration files with Python's `ast` module rather than running `alembic heads`,
so it needs no database and no configuration and catches a second head the moment
it is committed. `python -m alembic heads` run from `apps/api` against a live
database remains the authoritative check and is part of §9.

**The secret scanner is placeholder-aware.** It only reports *literal* values —
a quoted string, or a bare value that runs to the end of the line as in `.env`
and YAML — because a secret can only be committed as a literal; expressions like
`password_hash=hash_password(pw)` cannot carry one. Values documented as
local-only placeholders (the Compose Postgres password, the published demo
password, the test-fixture password) are listed explicitly in
`PLACEHOLDER_VALUES` in the script. If a legitimate new placeholder is
introduced, add it there **with a comment saying why**; never widen the scanner
to silence a real finding.

It scans tracked files **and** untracked-but-not-ignored ones, because a secret
is most dangerous in the window before it is committed.

**The `readiness-scan: allow` pragma** is the line-scoped escape hatch, in the
tradition of `# noqa` / `# nosec`. A scanner has to be able to write down the
shapes it detects: its own tests need a credential-shaped fixture, and
documentation needs to show what a bad value looks like. The pragma exempts
**one line** and must state why:

```python
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"  # readiness-scan: allow — published example key
```

Use it only for a value that is demonstrably not a credential. **Never use it to
silence a real finding** — the fix for a committed secret is to rotate and remove
it. Every use is auditable with `git grep "readiness-scan: allow"`, and
`test_allow_pragma_exempts_only_its_own_line` proves the exemption cannot leak
past its line.

---

## 6. Environment Variables Checklist

Only variables the repository actually reads are listed. There is no
`SECRET_KEY` or `SESSION_SECRET`: session and CSRF tokens are generated randomly
per session and persisted only as SHA-256 hashes
(see [STAFF_AUTH_RBAC.md](STAFF_AUTH_RBAC.md)).

### Backend — `apps/api/.env` (from `apps/api/.env.example`)

| Variable | Local default | Must be reviewed before any real deployment |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | **Yes** — set to `production`; this forces Secure cookies on |
| `BUSINESS_TIMEZONE` | `Europe/Istanbul` | **Yes if the shop is not in Türkiye** — decides where the business day starts and which hour a bucket belongs to. Reporting only; storage stays UTC. An unknown zone fails at startup (slim images need `tzdata`) |
| `DATABASE_URL` | local Compose Postgres | **Yes** — managed instance, credentials from a secret store |
| `REDIS_URL` | `redis://localhost:6379/0` | Yes |
| `CUSTOMER_WEB_BASE_URL` | `http://localhost:3000` in code | **Yes** — see the note in §13 |
| `ALLOW_LEGACY_ORDER_CONTEXT` | `false` | **Must stay `false`** — `true` would trust client-supplied store/table context |
| `SESSION_COOKIE_NAME` / `CSRF_COOKIE_NAME` | `sweetops_session` / `sweetops_csrf` | No |
| `SESSION_COOKIE_PATH` | `/` | No |
| `SESSION_COOKIE_DOMAIN` | empty (host-only cookie) | Leave empty unless a parent domain is genuinely required |
| `SESSION_COOKIE_SAMESITE` | `lax` | Review with your deployment topology |
| `SESSION_COOKIE_SECURE` | `false` | **Yes** — forced `true` when `ENVIRONMENT=production` |
| `SESSION_ABSOLUTE_LIFETIME_HOURS` | `12` | Review |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `120` | Review |
| `SESSION_LAST_SEEN_THROTTLE_SECONDS` | `300` | No |
| `LOGIN_MAX_FAILED_ATTEMPTS` | `5` | Review |
| `LOGIN_LOCKOUT_MINUTES` | `15` | Review |
| `PASSWORD_MIN_LENGTH` / `PASSWORD_MAX_LENGTH` | `10` / `1024` | Review |
| `STAFF_TRUSTED_ORIGINS` | localhost 3001–3004 | **Yes** — exact origins only; never `*` |
| `PUBLIC_TRUSTED_ORIGINS` | `http://localhost:3000` in code | **Yes** — exact origins only; never `*` |
| `ALLOW_MISSING_WEBSOCKET_ORIGIN` | `false` | **Must stay `false`** — `true` accepts non-browser WebSocket clients |

### Local stack — `.env` (from `.env.example`)

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` — must match
`docker-compose.yml`. Local development credentials only.

### Frontend — per app, not from the repo root

Next.js loads env files per application. Both variables have working localhost
defaults compiled in, so a standard local run needs no file at all. To override,
create the git-ignored `apps/<app>/.env.local`:

| Variable | Default | Read by |
| --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | customer-web, kitchen-web, cashier-web, owner-web |
| `NEXT_PUBLIC_WS_URL` | `ws://localhost:8000/ws/kitchen` | kitchen-web, owner-web |

> Changing either to a non-localhost origin **requires** adding that origin to
> `STAFF_TRUSTED_ORIGINS` / `PUBLIC_TRUSTED_ORIGINS`. Cookie auth uses an
> explicit credentialed allow-list and the WebSocket handshake validates the
> browser `Origin`; neither ever accepts `*`.

### Secret handling rules

1. `.env`, `.env.local` and `.env.*` are git-ignored; only `*.env.example` files
   are tracked (see `.gitignore`).
2. Example files contain **placeholders only**. No real credential has been
   committed to this repository.
3. The only credential-shaped values in tracked files are the local Compose
   Postgres password (`sweetops_password`, also used by `data/dbt/profiles.yml`),
   the published demo password (`demo1234`), and the backend test-fixture
   password (`testpassw0rd`). All three are local-only by construction and are
   listed as known placeholders in `scripts/verify_release_readiness.py`.

---

## 7. Database Migration Workflow

Schema is managed with Alembic; revisions live in `apps/api/alembic/versions/`.
The history is maintained as a **single head** — see
[ALEMBIC_SINGLE_HEAD_RESOLUTION.md](ALEMBIC_SINGLE_HEAD_RESOLUTION.md) for how a
previous divergence was reconciled.

```bash
cd apps/api

python -m alembic heads      # expect exactly ONE line ending in "(head)"
python -m alembic current    # what this database is actually at
python -m alembic upgrade head
python -m alembic history --verbose
```

From the repository root, the static equivalent of the heads check (no database
required) is `python scripts/verify_release_readiness.py`.

### Rules

- **Never edit a migration that has been merged.** Add a new revision instead.
- **Never bypass migrations** by hand-editing the schema or by letting SQLAlchemy
  create tables. The migration history is the schema's source of truth.
- If two branches each add a revision, resolve the divergence with an explicit
  merge revision — do not leave two heads.
- This branch adds **no migration**. None was needed.

### Downgrade caveats — read before you downgrade

The downgrade paths are implemented and tested, but they are deliberately
**defensive rather than silent**:

- Downgrading `store_scoped_inventory` **refuses** while more than one store
  holds stock, because collapsing store-scoped stock back to global stock would
  have to invent a single quantity. This is correct behaviour, not a bug.
- Downgrading the stock-count, threshold, transfer, and order-issue branches
  **refuses** while rows exist that the older schema cannot represent.
- The append-only ledger triggers are reinstalled on re-upgrade; the migration
  round-trip is covered by `test_payment_migration.py` and the
  `test_*_migration.py` suites.

Consequence: a downgrade is a **data-loss-avoidance mechanism, not a routine
rollback path**. Plan rollbacks around forward-fix and database restore — see
[§12 Rollback Checklist](#12-rollback-checklist).

---

## 8. Seed Demo Data Workflow

```bash
npm run seed:demo          # or: python scripts/seed_demo_data.py
npm run seed:demo          # run it twice — the second run must duplicate nothing
```

Running it twice is itself the idempotency check and is part of the release
verification in §9. After seeding, all four reconcilers must still report clean —
the seed drives the same services the API does, so it cannot create ledger drift.

**Never run this against a production database.** It creates staff accounts with
a published password.

---

## 9. Test Verification Workflow

> ### ⚠️ Run the backend suite BEFORE seeding demo data
>
> The backend tests and the local development database are the **same
> PostgreSQL database**. Several tests assert properties that only hold when no
> demo data is resident — in particular the migration downgrade tests, which
> correctly refuse to downgrade store-scoped inventory while a second store holds
> stock.
>
> With demo data resident at the start of a run, roughly **two dozen tests fail**
> in the files that sort alphabetically before `test_seed_demo_data.py`, and that
> test's own teardown then removes the demo data mid-run so everything after it
> passes. The failures are an artefact of database state, not a regression.
>
> **Correct order: migrate → test → seed → demo.** If you have already seeded,
> see "recovering a dirty local database" in
> [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md).

### Full verification, from the repository root

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

### Backend, from `apps/api`

```bash
python -m alembic heads          # exactly one head
python -m alembic upgrade head
python -m pytest -q --collect-only
python -m pytest -q              # expect: 0 failed
```

### Then the demo and the reconcilers, from the repository root

```bash
python scripts/seed_demo_data.py
python scripts/seed_demo_data.py     # idempotency

python scripts/reconcile_kitchen_timing.py
python scripts/reconcile_payments.py
python scripts/reconcile_inventory.py
python scripts/reconcile_order_issues.py
```

**Never mark a failing test `xfail`, skip it, or delete it to make the suite
pass.** A failure is either a real defect or a database-state problem with a
known cause; both have to be understood, not silenced.

---

## 10. Reconciliation Workflow

Four independent, **read-only** scripts re-derive stored summaries from the
append-only ledgers and report drift. None of them writes, and none of them can
"fix" anything — reconciliation must never rewrite financial or stock history.

| Script | Re-derives |
| --- | --- |
| `scripts/reconcile_payments.py` | Order paid/refunded summaries vs. the payment ledger, **and** each closed cashier shift's frozen snapshot vs. a fresh re-derivation of its own window |
| `scripts/reconcile_inventory.py` | On-hand and reserved stock vs. the movement ledger |
| `scripts/reconcile_order_issues.py` | Order issues vs. their bounded refunds |
| `scripts/reconcile_kitchen_timing.py` | Prep timings vs. the order status-event log |

```bash
python scripts/reconcile_payments.py            # all stores
python scripts/reconcile_payments.py --store 1  # one store
python scripts/reconcile_payments.py --json     # machine-readable
```

Exit code `0` means everything reconciles; `1` means at least one mismatch. A
mismatch is significant: the immutability triggers make drift unrepresentable
through the application, so it indicates direct-SQL tampering or a maths
regression. **Investigate — do not "correct" the ledger.**

No credentials, tokens or card data are ever printed by any reconciler.

---

## 11. Security Checklist

> **This is a practical readiness review, not a formal security audit.** It
> records what the repository demonstrably does today. It is not a penetration
> test, a threat model, or a certification, and no third party has reviewed it.

### Verified in the repository

- [x] **No secrets committed.** No API key, token, or private key appears in a
      tracked file. The only credential-shaped values are the local Compose
      Postgres password, the published demo password, and the test-fixture
      password — all local-only by construction (§6).
- [x] **`.env` files are git-ignored**; only `*.env.example` is tracked, and both
      example files contain placeholders only.
- [x] **CORS uses an explicit credentialed allow-list.** `all_cors_origins` is
      built from `STAFF_TRUSTED_ORIGINS` + `PUBLIC_TRUSTED_ORIGINS`; `*` is never
      used with credentials.
- [x] **CSRF on state-changing requests.** Double-submit: an HttpOnly session
      cookie plus a deliberately JS-readable CSRF cookie echoed in
      `X-CSRF-Token`, compared in constant time, alongside an independent
      trusted-origin check (`app/core/deps.py`).
- [x] **WebSocket hijacking defence.** The kitchen handshake validates the
      browser `Origin` against the trusted staff origins by exact
      scheme/host/port. A missing `Origin` means a non-browser client and is
      rejected by default (`ALLOW_MISSING_WEBSOCKET_ORIGIN=false`).
- [x] **`Cache-Control: no-store` on operational APIs** — auth, cashier,
      inventory, kitchen timing, order issues, owner payments, and the owner
      operational dashboard all set it, so an operational snapshot is never
      cached by an intermediary.
- [x] **Store scoping is server-derived.** The store comes from the
      authenticated staff session or the signed QR token, never from a
      client-supplied `store_id`. `ALLOW_LEGACY_ORDER_CONTEXT` defaults to
      `false` so client-supplied table context is never trusted; the test suite
      opts in explicitly and is the only thing that does.
- [x] **Signed QR table tokens** — a table context cannot be forged.
- [x] **Passwords are hashed with Argon2**; raw session and CSRF tokens are never
      persisted or logged, only their SHA-256 hashes.
- [x] **Login lockout** after `LOGIN_MAX_FAILED_ATTEMPTS`, with identical
      responses for unknown user / wrong password / disabled account.
- [x] **Server-side revocable sessions** with absolute and idle lifetimes;
      `logout`, `logout-all`, password reset, disable, and revoke all kill them.
- [x] **Append-only ledgers with database-level immutability triggers** on
      payments, inventory movements, stock counts, threshold updates, order
      issues, and closed cashier shifts. There is **no runtime bypass** — the
      only escape hatch is ownership-gated DDL, unreachable from application DML
      or an injection path, used solely by test teardown.
- [x] **Reconcilers are read-only** and print no credentials, tokens or card data.
- [x] **Demo credentials are clearly local-only**, published on purpose and
      documented, and the seed is demo-scoped and non-destructive.
- [x] **Downgrade caveats are documented** (§7).
- [x] **No unresolved merge markers, no `TODO`/`FIXME` in application code.**

### Required before any real deployment — NOT done here

- [ ] TLS termination; `ENVIRONMENT=production` (which forces Secure cookies).
- [ ] **Infrastructure rate limiting in front of `/auth/login`.** Application-level
      account lockout is not a substitute — this is called out in
      `apps/api/.env.example` and remains unaddressed.
- [ ] Secrets moved out of `.env` files into a managed secret store, with
      rotation.
- [ ] Real staff accounts created via `scripts/manage_staff_users.py`; **every
      demo account removed**.
- [ ] Trusted origins narrowed to the real deployed origins.
- [ ] Backups, restore rehearsal, monitoring, alerting, and centralised logging.
- [ ] Dependency vulnerability scanning and a patching policy.
- [ ] An independent security review.

---

## 12. Deployment Checklist

> No deployment target exists. This is the checklist that would have to be
> satisfied *first*, recorded so the gap is explicit rather than implied.

1. **Pre-flight** — [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) fully green:
   full verification (§9) passing, one Alembic head, reconcilers clean.
2. **Configuration** — `ENVIRONMENT=production`; `DATABASE_URL` pointing at a
   managed instance; trusted origins set to real origins;
   `ALLOW_LEGACY_ORDER_CONTEXT=false`; `ALLOW_MISSING_WEBSOCKET_ORIGIN=false`;
   every secret sourced from a secret store, never from a tracked file.
3. **Database** — provisioned, reachable, and **backed up before any migration**.
   Verify the backup restores.
4. **Migrate** — `python -m alembic upgrade head`. Confirm `alembic current`
   matches `alembic heads` afterwards.
5. **Accounts** — create real staff via `scripts/manage_staff_users.py`
   (passwords read via `getpass`, never passed on the command line). **Do not run
   the demo seed.** Confirm no demo account exists.
6. **QR tokens** — issue real table tokens via `scripts/manage_qr_tokens.py`
   with `CUSTOMER_WEB_BASE_URL` set to the real customer origin.
7. **Frontends** — build each app with the correct `NEXT_PUBLIC_API_BASE_URL`
   and `NEXT_PUBLIC_WS_URL`; serve over HTTPS/WSS.
8. **Edge** — TLS, and a rate limiter in front of `/auth/login`.
9. **Post-deploy** — smoke-check every surface (the manual list is in
   [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)), then run all four reconcilers
   and confirm they exit `0`.
10. **Observability** — confirm logs are being collected and that someone is
    alerted on failure before any real transaction is taken.

---

## 13. Rollback Checklist

**Prefer forward-fix.** Because several downgrades deliberately refuse when data
exists that an older schema cannot represent (§7), a schema rollback is not a
routine operation.

### Code-only change (no migration)

1. Revert the merge commit; redeploy the previous build.
2. Re-run the reconcilers; confirm all four exit `0`.
3. No data action required.

### Change that included a migration

1. **Stop writes** before anything else.
2. Attempt `python -m alembic downgrade <previous_revision>` **only** if that
   revision's downgrade is known to be safe for the data present. If it refuses,
   it is protecting data — do not force it.
3. If the downgrade refuses and the previous schema is genuinely required,
   **restore from the pre-migration backup**. This is why step 3 of the
   deployment checklist takes a backup first.
4. Re-run all four reconcilers before reopening writes.
5. Record what happened, so the migration can be made reversible next time.

### Local development

Recovery from an interrupted test run or a dirty local database is a separate,
much gentler procedure — see [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md).
Never reach for `docker-compose down -v` as a first response; it destroys the
volume.

---

## 14. Known Non-Production Limitations

Stated plainly, because a readiness document that hides these is worthless.

1. **Not deployed anywhere.** There is no hosting, no TLS, no domain, no managed
   database. Everything runs on `localhost` via Docker Compose.
2. **No CI.** Verification is the manual sequence in §9. Nothing prevents a
   broken commit from being merged except a human running it.
3. **No monitoring, alerting, centralised logging, or backup automation.**
4. **Secrets live in local `.env` files.** No secret store, no rotation.
5. **No infrastructure rate limiting.** Account lockout exists, but a reverse
   proxy / gateway limiter in front of `/auth/login` does not, and the code says
   so explicitly.
6. **`CUSTOMER_WEB_BASE_URL` and `PUBLIC_TRUSTED_ORIGINS` default to port
   `3000`,** while `npm run dev:customer` serves customer-web on port `3001`.
   Both `.env.example` files now set the correct value, but the compiled-in
   defaults in `app/core/config.py` were **deliberately left unchanged** in this
   documentation branch: changing them is an application-behaviour change with no
   test coverage, and it belongs in a change that can carry a test. Until then,
   set both explicitly in `apps/api/.env`.
7. **Tests share the development database.** There is no separate test database
   and no automatic reset, which is what makes the seed-before-test ordering
   hazard in §9 possible.
8. **Interrupted test runs leave debris.** Fixtures clean up on normal teardown,
   but an interrupted run leaves orphaned `user_<hex>` rows and orders behind.
   They are harmless but they accumulate — see the runbook.
9. **The demo seed publishes its password.** Correct for a demo, unacceptable
   anywhere real.
10. **Legacy analytics surface retained.** A legacy `ingredient-forecast`
    endpoint, older `/owner` analytics views, and a dbt project under `data/`
    remain in the repository. They are not the product centre, are not
    maintained as such, and are not covered by the operational guarantees above.
11. **No load or performance testing.** Correctness is tested; capacity is not.
12. **Single-node assumptions.** The WebSocket manager holds connections in
    process memory, so more than one API instance would need a shared broker.
13. **No formal security audit.** §11 is a practical readiness review only.

---

## 15. Post-MVP Backlog

Not committed and not part of the current build. The full breakdown, and the
reasoning for deferring forecasting, is in
[PROJECT_ROADMAP.md](PROJECT_ROADMAP.md).

- Forecasting (demand prediction)
- Supplier management
- Purchase orders
- Automatic reorder
- Scheduled alerts
- Barcode support
- Lot / expiry tracking
- Customer wallet
- Coupons / store credit
- Delivery integration
- Bank reconciliation
- Accounting export
- Chargeback workflow
- POS hardware integration
- Mobile app

Forecasting is deferred deliberately: it depends on a meaningful volume of
reliable operational history, and SweetOps prioritised building the system that
*produces* that history correctly and auditably first.
