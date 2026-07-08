# Secure, Revocable QR Store & Table Context

## 1. Original vulnerability

The customer web app previously derived its store and table context from plain,
client-controlled URL query parameters:

```
/?store=1&table=5
```

The frontend read these directly (`?store` defaulted to `1`) and sent them to
the backend, which persisted them verbatim on the order. Nothing validated that
the table belonged to the store, and there was no notion of an inactive table
or store. Consequences:

- A customer could edit the URL to `?store=2&table=99` and submit an order
  against **another store or table** they were never seated at.
- A missing `store` param silently **defaulted to store `1`**, so a bare
  `/` could place real orders.
- The order's analytical dimensions (`store_id`, `table_id`) were therefore
  attacker-controlled, corrupting kitchen routing and owner analytics.

## 2. Product impact

For a physical Turkish waffle shop this is an operational-integrity problem: an
order must be trustworthy about *which table* it belongs to so the kitchen
serves the right customer, and *which store* so revenue and stock are accounted
correctly. Trusting the URL means a mistaken or malicious guest can send waffles
to the wrong table or a store that never received them.

## 3. Architecture decision

Store and table context is now derived **only** from an opaque, server-resolved
QR token. The public URL delivers that token in the URL **fragment** (never a
query string — see §23):

```
https://<customer-host>/#qr=<opaque-token>
```

The token carries no readable store/table identifier. The server hashes it,
looks up the single active token record, and derives store + table from the
table→store relationship. Client-supplied `store_id`/`table_id` are never
trusted for context.

## 4. Why opaque, revocable tokens (not numeric params or a signed payload)

- **Numeric params** are trivially forgeable — rejected outright.
- **A self-contained signed payload** (e.g. a JWT embedding store/table) proves
  authenticity but cannot be **revoked or rotated** without extra
  infrastructure. Physical QR stickers in a shop must be revocable after misuse,
  rotatable after accidental exposure, replaceable without changing table IDs,
  traceable to a specific issued sticker, and disableable when a table is
  inactive. Only a **server-resolved opaque token** with a database record
  gives that operational control (revoke / rotate / lineage / last-used
  tracking) while also keeping zero store/table information in the token itself.

## 5. Data model

New table **`table_qr_tokens`** (model `app/models/table_qr_token.py`):

| Column           | Type                        | Notes |
|------------------|-----------------------------|-------|
| `id`             | Integer PK                  | indexed |
| `table_id`       | Integer FK → `tables.id`    | `ON DELETE CASCADE`, indexed, non-null |
| `token_hash`     | String(64)                  | **unique**, indexed, non-null — SHA-256 hex |
| `token_prefix`   | String(16)                  | non-secret, indexed — support/listing |
| `status`         | String                      | `ACTIVE` / `REVOKED`, indexed, default `ACTIVE` |
| `created_reason` | String, nullable            | audit hint (`issue`/`rotate`/…) |
| `created_at`     | DateTime(tz), server default | timezone-aware |
| `revoked_at`     | DateTime(tz), nullable      | set on revoke/supersede |
| `last_used_at`   | DateTime(tz), nullable      | updated on successful resolution |
| `replaced_by_id` | Integer FK → self, nullable | `ON DELETE SET NULL` — rotation lineage |

**Indexes/constraints:** PK on `id`; unique index on `token_hash`; plain indexes
on `table_id`, `token_prefix`, `status`; FKs on `table_id` (CASCADE) and
`replaced_by_id` (SET NULL); a **CHECK** constraint restricting `status` to
`ACTIVE`/`REVOKED` (§26); and a **partial unique index** on `table_id`
`WHERE status = 'ACTIVE'` enforcing at most one active token per table (§27).

**Cascade rationale (documented):** tables are effectively permanent; if one is
ever physically deleted its tokens are meaningless and cascade away.
Per-table *history* is preserved by keeping `REVOKED` rows (never deleting on
rotation/revoke), not by retaining tokens for deleted tables. Order/analytics
lineage does **not** depend on this table — it uses the server-derived
`orders.store_id` / `orders.table_id`.

## 6. Raw-token handling

The raw token is generated with `secrets.token_urlsafe(32)` (256 bits of
CSPRNG entropy) and is **never stored**. It appears only:

- in the physical QR URL,
- in the customer's live browser request,
- once, on stdout, when issued/rotated by the CLI.

## 7. Hashing

`token_hash = SHA256(raw_token)` (hex, 64 chars), deterministic, compared
server-side. Not password hashing (no deliberate slowness needed — the token
already has full entropy), not reversible encryption, never plaintext. No short
codes, sequential IDs, timestamps, or `Math.random()`.

## 8. Token issuance

CLI `scripts/manage_qr_tokens.py issue --table-id <id>`: verifies the table and
its store exist, row-locks the table, generates one raw token, stores only its
hash + prefix, and prints the raw token, prefix, and full customer URL exactly
once. If the table **already has an ACTIVE token** the command is rejected and
the operator is told to `rotate` instead (§27) — repeated `issue` can never
leave two valid stickers on one table.

## 9. Rotation

`rotate --token-id <id>`: given the database id of the current ACTIVE token,
atomically revokes it and issues a replacement (see §28 for the exact ordering),
links `replaced_by_id` from old → new for lineage, and prints the new raw token
once. History is preserved. The destructive target is an exact primary key, not
a display prefix (§25).

## 10. Revocation

`revoke --token-id <id>`: sets exactly the one token with that primary key to
`REVOKED` with `revoked_at`. Records are never deleted; future resolution is
rejected. An unknown or already-revoked id changes nothing and reports why. The
non-unique `token_prefix` is **never** a destructive selector (§25).

## 11. Public resolution API

`POST /public/qr-context/resolve` with `{ "qr_token": "<raw>" }` →

```json
{ "store": { "id": 1, "name": "SweetOps" },
  "table": { "id": 5, "name": "Masa 5" },
  "context_version": 1 }
```

Rules: hash the token → find exactly one `ACTIVE` record → join table → join
store → verify active (table/store `is_active` honored where present) → update
`last_used_at` → return only public context. Never returns the hash, internal
token id, revoked history, or staff/store metadata.

Errors are safe and Turkish (see §15). Invalid, unknown, malformed and revoked
tokens all return the **same** `404` message so a probing client cannot learn
whether a token ever existed. Inactive table/store returns `409`.

## 12. Menu integration

`POST /public/menu/resolve` with `{ "qr_token": "<token>" }`: the token travels
in the **request body, never the URL** (§23). The backend independently
re-resolves it; an invalid/revoked token returns a Turkish error and **no
menu**. There is deliberately no numeric `store` parameter, so store
manipulation has no effect. The catalog is a single shared waffle menu in the
current data model — the token gates *access*, not content.

The pre-existing ungated `GET /public/menu/` remains for internal callers but
carries **no token** at all; any stray `?qr_token=` query value there is inert
(never resolved), so there is no query-string token transport anywhere.

## 13. Order integration

`POST /public/orders/` accepts `{ "qr_token": "<token>", "items": [...] }`. The
service resolves the token **inside the order transaction with a row lock**
(`SELECT … FOR UPDATE`), derives `store_id`/`table_id` from it, and persists the
server-derived ids. Any client-supplied `store_id`/`table_id` are ignored
whenever a token is present. Quantity accounting, stock `FOR UPDATE` locking,
atomic rollback and idempotency are all preserved unchanged.

**Transition flag:** `settings.ALLOW_LEGACY_ORDER_CONTEXT` (default **`False`**)
gates the old client-supplied path for non-production use only. In production it
is off, so client-supplied context is never trusted. A `qr_token`, when present,
always wins regardless of the flag.

## 14. Idempotency integration

The customer fingerprint (`apps/customer-web/src/lib/order-idempotency.ts`) now
keys on `{ qr_token, items }` instead of `{ store_id, table_id, items }`:

- Same QR token + unchanged cart → same fingerprint → **same** idempotency key
  reused across retries/double-clicks.
- A different QR token (rotated sticker / different table) → different
  fingerprint → **new** logical attempt.

sessionStorage persistence, network-retry classification, the synchronous
double-click guard and success cleanup are all unchanged.

## 15. Turkish customer states

| State | Message |
|-------|---------|
| Loading | `QR kod doğrulanıyor…` |
| Menu ready (header) | `<Mağaza> · Masa <n>` |
| Missing `qr` | `QR kod bilgisi bulunamadı. Lütfen masadaki QR kodu yeniden okut.` |
| Invalid/revoked | `Bu QR kod geçerli değil. Lütfen masadaki güncel QR kodu kullan.` |
| Table/store unavailable | `Bu masa şu anda siparişe açık değil. Lütfen işletme personelinden yardım iste.` |
| Connection failure (retry) | `Bağlantı kurulamadı. Lütfen tekrar dene.` + `Tekrar Dene` |
| Order uncertain | `Sipariş sonucu doğrulanamadı. Tekrar deneyebilirsin; siparişin iki kez oluşturulmayacak.` |
| Order rejected | `Sipariş oluşturulamadı. Lütfen seçimlerini kontrol et.` |

Server-side Turkish messages live in `app/core/messages.py`.

## 16. Logging rules

Raw tokens are never logged. Correlation uses only `token_prefix` (e.g.
`qr_resolve_invalid prefix=Ab12Cd34`). Reviewed (re-verified during hardening —
§24): request/exception logging, API validation errors, CLI output, frontend
`console.*`, tests — none emit a raw token, and nothing interpolates the full
request URL or payload into a log or exception.

## 17. Analytics & data-lineage rules

- Analytics continues to use the trustworthy server-derived dimensions
  `orders.store_id` and `orders.table_id`.
- No raw tokens or token hashes enter dbt marts or owner analytics.
- The token record is a security/operational lookup mechanism, not a business
  dimension. Operationally it may retain `last_used_at`, `status`, and its table
  relationship only.
- **Deferred (not built here):** safe operational metrics such as invalid-QR
  resolution count, revoked-token attempts, inactive-table attempts and QR
  resolution failure rate. These require a lightweight audit sink that does not
  exist for this path yet; adding one is out of scope for this branch.

## 18. Deployment & physical QR-printing workflow

See §"Physical Shop Rollout" below.

## 19. Rollback plan

- Code: revert the branch; the feature is additive.
- Schema: `alembic downgrade -1` drops only `table_qr_tokens` (verified: `orders`
  and all existing tables survive), then re-`upgrade` restores it. Single head
  throughout.
- Because raw tokens are only shown once, a rollback that drops the table
  requires re-issuing tokens (and reprinting stickers) on a subsequent
  re-upgrade — plan a maintenance window if rolling back after go-live.

## 20. Tests & verification

Backend: `apps/api/tests/test_qr_context.py` (token invariants, resolution,
store/table integrity, menu, order) and `apps/api/tests/test_qr_cli.py` (CLI).
Frontend: `apps/customer-web/src/lib/api.test.ts` and `order-idempotency.test.ts`.
See the PR report for the full scenario list and results.

## 21. Deferred: staff-authenticated QR management UI

No public/unauthenticated token-management endpoint is introduced. A staff web
UI for issuing/rotating/revoking tokens must wait until staff authentication and
RBAC exist; until then, management is CLI-only and operator-run.

## 22. Deferred: payment settlement workflow

Payment capture/settlement is untouched and out of scope for this branch.

---

## Physical Shop Rollout

1. Apply the migration: `alembic upgrade head`.
2. For each active table, run `python scripts/manage_qr_tokens.py issue --table-id <id>`.
3. Capture each printed raw customer URL **once** (it is never shown again).
4. Generate and print the physical QR sticker from that URL.
5. Label the sticker with the human table name (e.g. "Masa 5") — **never** the token.
6. Test each QR with a real mobile device.
7. Confirm the displayed store and table are correct.
8. Submit a test order.
9. Confirm the kitchen receives it.
10. Revoke test/incorrect tokens by their database id (find it with `list`):
    `revoke --token-id <id>`.
11. Replace any legacy numeric-parameter (`?store=&table=`) stickers.
12. Disable legacy context mode in production (`ALLOW_LEGACY_ORDER_CONTEXT=false`,
    the default).

> This document describes the process; the rollout is **not** complete until the
> above has been physically tested on real devices in the shop.

## Concurrency & consistency note

Order creation resolves the token with `SELECT … FOR UPDATE`, so a revoke/rotate
that commits *before* the order transaction reads the row causes the order to
see a non-ACTIVE token and be rejected; a revoke attempting to commit *during*
an in-flight order serializes behind the order's row lock. Resolution verifies
`ACTIVE` status at request time, and order creation verifies it **independently**
— frontend resolution alone is never sufficient authorization.

---

# Hardening review (security/database/analytics/product)

The sections below record the production-hardening pass on top of the original
design. They supersede any earlier wording that placed the token in a query
string or selected destructive operations by prefix.

## 23. Why query-string bearer tokens are prohibited; fragment delivery + scrubbing

The QR token is a **long-lived bearer token** attached to a physical sticker.
Putting it in a URL **query string** (`/?qr=<token>`) is unacceptable because a
query string is transmitted to the web server on the very first request and is
therefore captured by infrastructure that application code cannot redact:

- browser history,
- reverse-proxy access logs,
- CDN logs,
- hosting-platform / load-balancer request logs,
- observability and error-monitoring systems,
- copied URLs and screenshots,
- `Referer` headers,
- support diagnostics.

Application-level log redaction does **not** fix infrastructure-level
query-string logging — by the time the request is logged the token is already
in it.

**Fragment delivery.** The physical QR encodes the token in the URL *fragment*:

```
https://<customer-host>/#qr=<raw-token>
```

The fragment is **not sent to the server** on the initial page load, so it never
reaches any of the request-logging surfaces above.

**Immediate URL scrubbing.** On first client load (`src/lib/qr-session.ts`,
driven by `CustomerMenuPageClient`), the app:

1. reads the raw token from `window.location.hash`,
2. validates the expected `#qr=` structure,
3. persists the active token to guarded `sessionStorage`,
4. removes the token from the visible address bar with `history.replaceState`,
5. continues the session from the stored token,
6. never writes the raw token to console, errors, analytics or logs.

After scrubbing, the address bar contains no token.

**No legacy query mode.** `?qr=<token>` is **not** supported. It is not silently
migrated (the initial request may already have been logged by infrastructure),
and no compatibility flag re-enables it. The one URL a token can appear in is the
physical fragment on the sticker; the app removes even that on load.

## 24. Referrer policy and infrastructure logging risk

Customer web sends `Referrer-Policy: no-referrer` on every route
(`next.config.ts` → `src/lib/security-headers.ts`). This is **defense in depth**:
the token already lives only in the fragment and is scrubbed on load, but a
strict no-referrer policy guarantees that even a stray outbound request can never
carry any part of this app's URL to a third party. It does not replace
fragment-based delivery; it backs it up.

The broader infrastructure-logging risk (§23) is the reason **no** token — QR,
menu, resolve or order — is ever placed in a URL. All token transport is in
request bodies:

| Purpose        | Transport                                            |
|----------------|------------------------------------------------------|
| QR resolution  | `POST /public/qr-context/resolve` — body             |
| Menu (gated)   | `POST /public/menu/resolve` — body                   |
| Order creation | `POST /public/orders/` — body                        |

## 25. Token-ID-based destructive operations

`token_prefix` is a human-support **display** value and is **not** guaranteed
unique, so it must never be the sole selector for a destructive operation. All
destructive CLI operations target the token record's database **primary key**:

```
revoke --token-id <database-id>
rotate --token-id <database-id>
```

`list` shows token id, store, table, prefix, status and timestamps so an
operator can find the exact id. The prefix remains display-only. An ambiguous
prefix can never revoke or rotate anything (there is no prefix-based destructive
path in the service at all — `revoke_by_prefix` was removed). Revocation never
requires the raw token.

## 26. Database status constraint (Blocker 3)

A CHECK constraint enforces the status domain at the database level — app
validation alone is not trusted:

```sql
CONSTRAINT ck_table_qr_tokens_status CHECK (status IN ('ACTIVE', 'REVOKED'))
```

It is created inside the `table_qr_tokens` migration (single head preserved) and
mirrored in the SQLAlchemy model `__table_args__`. Verified against a disposable
PostgreSQL: an `INSERT … status='BOGUS'` is rejected.

## 27. One-active-token-per-table invariant (Blocker 4)

Product decision for the physical-shop product: **at most one ACTIVE QR token
per table** — one physical table has one current trusted sticker. This makes
rotation deterministic, prevents accidental repeated `issue` from leaving
several valid historical stickers, and lets staff identify the current QR.

Enforced at the database level with a **partial unique index**:

```sql
CREATE UNIQUE INDEX uq_table_qr_tokens_one_active_per_table
  ON table_qr_tokens (table_id)
  WHERE status = 'ACTIVE';
```

Only ACTIVE rows participate, so a table may keep **unlimited REVOKED history
rows** while never having two active ones. Verified against a disposable
PostgreSQL: a second `ACTIVE` row for one table is rejected; multiple `REVOKED`
rows for one table are allowed.

**Issue** on a table that already has an active token is rejected
(`ActiveTokenExists`); the operator is told to `rotate`.

## 28. Concurrent issue / rotate behaviour

Both `issue` and `rotate` first acquire a `SELECT … FOR UPDATE` row lock on the
parent **table**, so all issue/rotate operations for a table serialize. The
partial unique index is the ultimate backstop; the table lock turns a would-be
`IntegrityError` into deterministic, ordered behaviour.

**Rotation is atomic** and ordered to respect the partial unique index:

1. lock the parent table,
2. flip the currently-active token(s) to `REVOKED` and **flush** — this frees
   the partial unique index *before* the replacement is inserted,
3. insert the replacement ACTIVE token,
4. link lineage (`old.replaced_by_id → new.id`),
5. commit once.

Concurrency guarantees (proved by tests 19–20 against the real database):

- two simultaneous `issue` calls → exactly one succeeds, the other gets
  `ActiveTokenExists`; the table ends with one active token;
- two simultaneous `rotate` calls serialize on the table lock and the table ends
  with exactly one active token.

## 29. Browser token session lifecycle

`src/lib/qr-session.ts` (pure, dependency-injected, unit-tested):

- **Fresh scan** `/#qr=<token>`: token captured client-side, persisted to
  `sessionStorage`, URL scrubbed, QR resolved, menu loaded.
- **Same-tab refresh**: the fragment is already gone; the token is re-read from
  `sessionStorage` and the menu keeps working — no token in the URL.
- **New tab without scanning**: no fragment, no session token → Turkish
  missing-QR screen, no menu request, no order submission.
- **Rotated/revoked token**: resolution returns a definitive *invalid*; the app
  calls `clearQrToken()` so the dead token is removed from `sessionStorage`,
  shows the Turkish invalid-QR state and disables ordering. A same-tab refresh
  then lands on missing/invalid rather than retrying a dead token.
- **Network failure**: the token is **kept** for retry — a transient failure is
  never misclassified as an invalid token.

## 30. Idempotency (unchanged, restated)

The customer idempotency fingerprint still keys on `{ qr_token, items }` and may
hold the raw token in browser memory / `sessionStorage` (same-origin, cleared on
tab close). It is never written to logs, analytics, or a URL. A changed QR token
yields a new fingerprint and therefore a fresh idempotency attempt.

## 31. Analytics & data governance (safe analytics configuration)

Re-confirmed during hardening:

- raw tokens do not enter dbt models (no `token` reference in `data/dbt`),
- token hashes are not business dimensions,
- `order.store_id` / `order.table_id` remain **server-derived**,
- no owner dashboard displays token material,
- no browser analytics event includes the token or the full scanned URL (the app
  emits no client-side analytics and never logs the token).

> **Warning — safe analytics configuration:** Never configure frontend analytics
> to capture URL fragments or `sessionStorage` values. The QR token lives in both
> (by design, same-origin) and must never be exported to a third party. If a
> product analytics SDK is added later, explicitly exclude the URL fragment and
> the `sweetops.qrToken` / `sweetops.pendingOrderAttempt` storage keys.
