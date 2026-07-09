# Staff Authentication & Store-Scoped RBAC

Production-grade staff authentication, role-based authorization, and store-scoped
data access for SweetOps (a Turkish waffle shop). Internal identifiers, logs and
this document are English; all new customer/operator-facing UI text is Turkish.

---

## 1. Original exposure

Before this branch the staff surface was effectively public and trusted the
client for identity and scope:

| Area | Problem |
|------|---------|
| `/owner/*` | All owner analytics, insights, metrics, decisions were unauthenticated. |
| `/kitchen/orders/` | Unauthenticated; store came from a `store_id` query param (default `1`). |
| `PATCH /kitchen/orders/{id}/status` | Unauthenticated; audit actor came from the `X-Actor-Id` header. |
| `PATCH /owner/decisions/{id}` | Unauthenticated; lifecycle actor came from a client `actor_id` body field. |
| `/ws/kitchen` | Unauthenticated; store came from a `store_id` query param; a single global broadcast set delivered every store's orders to every socket. |
| Owner analytics/metrics/decision SQL | Queried **all** stores' orders. |
| `owner_decisions` | Keyed by `decision_id` only — decisions could collide across stores. |
| `users` / `roles` | Existed but were not wired to request authentication; no login, logout, session revocation, lockout, or disablement. |

## 2. Threat model

Adversaries considered:

- **Unauthenticated network client** hitting staff endpoints/WebSocket directly.
- **Cross-store staff** trying to read or mutate another store's orders, analytics or decisions.
- **Identity spoofing** via `X-Actor-Id` header or request-body `actor_id`.
- **CSRF** — because auth is cookie-based, a malicious site trying to drive authenticated state changes.
- **Login CSRF** — a malicious origin submitting a login to fixate a session.
- **Credential/session theft** via logs, browser storage, or URL leakage.
- **Brute force** against a single account.

Out of scope for this branch (documented deferrals in §12): infrastructure rate
limiting, store-scoped inventory, payment settlement.

## 3. Opaque session architecture

Authentication uses **database-backed opaque sessions in secure cookies**, not
JWTs in browser storage. On login the server generates a cryptographically strong
random token (`secrets.token_urlsafe(32)`) and stores only its **SHA-256 hash**
(`auth_sessions.token_hash`). The raw token exists only in the user's HttpOnly
cookie and transiently in memory during session creation; it is never persisted
or logged.

Every protected request re-derives identity, role, and store from **current DB
state** (`auth_service.resolve_session`) — never from values copied into the
cookie — so disablement, role change, store change, and revocation take effect on
the very next request. Validation checks, in order: session exists → not revoked →
not expired (absolute) → not idle-timed-out → user exists → user active → role
exists → operational role has a store.

## 4. Password hashing

- **Argon2id** via `argon2-cffi` (`app/core/security.py`). No hand-rolled hashing.
- Unknown usernames still run a dummy Argon2 verify to equalise timing and avoid
  username disclosure.
- `check_needs_rehash` opportunistically upgrades outdated hashes on successful login.
- Policy (`validate_password`): ≥ 10 chars, no empty/whitespace-only, long
  passphrases allowed, no artificial small maximum. Policy internals are never
  leaked in public login errors.

## 5. Session hashing

`auth_sessions` stores SHA-256 hex digests only:

- `token_hash` — unique + indexed; the lookup key for a presented cookie token.
- `csrf_token_hash` — the hash of the per-session CSRF token.
- Optional `user_agent_hash` — never the raw UA string.

Raw session and CSRF tokens are never stored or logged.

## 6. CSRF model

Double-submit with a server-side hash:

- On login a per-session CSRF token is generated; only its hash is stored.
- The raw CSRF token is delivered in a **non-HttpOnly** cookie (`sweetops_csrf`)
  so the SPA can read it and echo it in the `X-CSRF-Token` header.
- For authenticated `POST/PUT/PATCH/DELETE`, `deps.enforce_csrf` hashes the
  header value and compares it to `session.csrf_token_hash` using
  `hmac.compare_digest` (constant time).
- Login additionally validates request **Origin/Referer** against
  `STAFF_TRUSTED_ORIGINS` (`deps.enforce_origin`) to reduce login-CSRF. An absent
  Origin (non-browser client) is allowed — the CSRF token is the second,
  independent line of defence — while a present-but-untrusted Origin is rejected.
- Public customer QR/menu/order endpoints do **not** inherit staff CSRF.

## 7. Cookie configuration

Set identically everywhere via `app/core/cookies.py`:

| Attribute | Session cookie | CSRF cookie |
|-----------|----------------|-------------|
| HttpOnly | **true** | false (must be JS-readable) |
| Secure | `settings.cookie_secure` (forced **true** in production) | same |
| SameSite | `SESSION_COOKIE_SAMESITE` (default `lax`) | same |
| Path | `/` | `/` |
| Domain | host-only unless `SESSION_COOKIE_DOMAIN` set | same |
| Max-Age | absolute lifetime | same |

Both cookies are cleared on logout and on an invalid/expired session.

## 8. Login & lockout behaviour

- Account-level brute-force protection: after `LOGIN_MAX_FAILED_ATTEMPTS`
  (default 5) failed attempts the account is locked for `LOGIN_LOCKOUT_MINUTES`
  (default 15). Failed count increments atomically.
- Successful login clears the failed count and lock and updates `last_login_at`.
- Unknown user, wrong password, and disabled account all return the **same**
  generic Turkish message (`Kullanıcı adı veya şifre hatalı.`) so the condition
  cannot be distinguished. A locked account returns a distinct Turkish lock
  message (by design).
- Logs use safe identifiers only; passwords and tokens are never logged.
- **Not a substitute for infrastructure rate limiting** — see §12.

Session lifetime: absolute 12h (`SESSION_ABSOLUTE_LIFETIME_HOURS`), idle timeout
120 min (`SESSION_IDLE_TIMEOUT_MINUTES`). `last_seen_at` writes are throttled to
once per `SESSION_LAST_SEEN_THROTTLE_SECONDS` (default 300s) to avoid a write on
every request. No persistent "remember me".

## 9. Role matrix

Canonical roles: `OWNER`, `MANAGER`, `KITCHEN`, `CASHIER`.

| App | Roles allowed to render |
|-----|-------------------------|
| owner-web | OWNER, MANAGER |
| kitchen-web | OWNER, MANAGER, KITCHEN |
| (neither) | CASHIER — created for future payment settlement; no owner/kitchen access in this branch |

Frontend role gating is UX only; the backend remains the security boundary.

## 10. Permission matrix

Named permissions (`app/core/permissions.py`), assigned centrally — no scattered
role-name checks:

| Permission | OWNER | MANAGER | KITCHEN | CASHIER |
|------------|:-----:|:-------:|:-------:|:-------:|
| `owner:read` | ✅ | ✅ | ❌ | ❌ |
| `owner:decisions:write` | ✅ | ✅ | ❌ | ❌ |
| `kitchen:read` | ✅ | ✅ | ✅ | ❌ |
| `kitchen:orders:write` | ✅ | ✅ | ✅ | ❌ |

Endpoints depend on `require_permission("...")`; unauthorized → 401, authenticated
but insufficient → 403 (both structured Turkish errors). CASHIER has no
permissions in this branch (least privilege).

## 11. Store-scoping rules

The authenticated user's `store_id` (from the session) is the only trusted staff
store context. Query params, request bodies, and browser storage are never
trusted.

- **Kitchen orders** (`kitchen_service.get_kitchen_orders`) filter by store.
- **Kitchen status mutation** loads the order and returns a **non-disclosing 404**
  if `order.store_id != session store` (cross-store existence is not revealed).
- **WebSocket** derives store from the session; initial state and broadcasts are
  partitioned per store.

## 12. Owner analytics scoping

Every order-derived query is filtered by the session store:

- `owner_analytics_service`: KPIs, top-ingredients, hourly demand, daily sales,
  ingredient forecast.
- `owner_insights_service`: critical alerts, prep time, trending ingredients,
  popular combos, value summary.
- `metrics_service`: conversion, decisions, kitchen, revenue-protection groups
  (`store_id` threaded into every SQL; `None` only for the public
  operational-context path).
- `decision_engine`: demand-spike, SLA-risk, revenue-anomaly and metric-driven
  signals are store-filtered; persisted decisions are keyed by store.

## 13. Global inventory single-store limitation

`ingredients` / `ingredient_stock` / stock movements are **global** (no
`store_id`) in the current schema. Endpoints and signals that read them
(`/owner/stock-status`, `/owner/insights/critical-alerts`,
`/owner/insights/value-summary`, and the `stock_risk`/`slow_moving` decision
signals) are only trustworthy while a single operational store exists.

`inventory_guard` **fails closed**: when more than one operational store exists
(distinct `users.store_id`), those endpoints return a structured Turkish 409 and
the inventory decision signals are skipped. Order-derived analytics remain
correctly per-store. The proper fix is deferred to
**`refactor/store-scoped-inventory`**.

## 14. Owner decision store integrity

`owner_decisions` now has a composite primary key `(store_id, decision_id)`:

- Every persisted decision belongs to exactly one store.
- The same natural key (e.g. `sla_risk_current`) can recur per store without
  collision.
- Lookup and lifecycle mutation are scoped by the authenticated store; another
  store's decision reports not-found.
- The lifecycle actor is derived from the session; a client-supplied `actor_id`
  is ignored.

Migration backfills existing decisions to the single existing store and **fails
closed** if attribution is ambiguous (multiple stores).

## 15. WebSocket authorization & partitioning

`/ws/kitchen` is part of the authorization boundary. The handshake authorization
sequence is (order matters):

1. **Validate the handshake `Origin`** against the trusted staff origins (CSWSH
   defence — see §15a) → close `4403` on failure.
2. Resolve the HttpOnly session cookie (no query params trusted).
3. Reject missing/expired/revoked session → close `4401`.
4. Load the current user and role from the DB and require `kitchen:read` →
   close `4403` on failure.
5. Derive the store from the current user, join that store's connection group,
   and send `initial_state` for that store only.

- Origin validation is an **additional** boundary; it never replaces session or
  permission validation. All five steps still run.
- The manager is partitioned (`connections_for_store`); a broadcast for store A
  is delivered only to store-A sockets. Order creation and status updates
  broadcast with the server-derived `store_id`.
- No raw session/CSRF token, cookie, or full WS URL is ever logged. A rejected
  origin is logged only as a normalized `scheme://host:port` label
  (`safe_origin_label`), or `<missing>` / `<invalid>`.

### 15a. Cross-Site WebSocket Hijacking (CSWSH)

**The risk.** The kitchen WebSocket authenticates via the staff session cookie.
The browser attaches that cookie to **any** WebSocket handshake to the SweetOps
origin — including one opened by JavaScript on a *malicious, unrelated page* a
logged-in staff member happens to visit. WebSocket handshakes are **not** subject
to the same-origin policy the way `fetch`/XHR are, and CORS does not gate them:
`allow_origins` on `CORSMiddleware` protects HTTP responses, **not** the WS
upgrade. So without an explicit check, `evil.example.com` could open
`wss://.../ws/kitchen` from the victim's browser, ride the session cookie, and
read live kitchen data for the victim's store. This is Cross-Site WebSocket
Hijacking. **Cookies alone are insufficient** — an Origin check is required.

**Exact allowlisting.** The handshake `Origin` is compared against
`STAFF_TRUSTED_ORIGINS` by **canonicalizing** both sides to
`(scheme, host, port)` and requiring structural equality — never substring or
prefix matching (`canonical_origin` / `is_trusted_origin` in `app/core/deps.py`).
Port is normalized to the scheme default (`http`→80, `https`→443) when implicit.
Consequences:

- Trusting `https://kitchen.example.com` does **not** trust
  `https://kitchen.example.com.attacker.test` (host is a different label).
- `null` (opaque origin), `*` (wildcard), embedded credentials
  (`http://user:pass@host`), non-`http(s)` schemes, and origins carrying a
  path/query/fragment are all rejected as malformed.

**Missing-Origin production policy.** Browsers always send `Origin` on a WS
handshake, so an absent `Origin` implies a non-browser client (tests, CLI,
server-to-server). Production **rejects missing Origin by default**:
`ALLOW_MISSING_WEBSOCKET_ORIGIN` defaults to `false` and is **never inferred from
the hostname**. It may be enabled **only** in an isolated test/dev configuration
for non-browser clients. The test suite passes an explicit trusted `Origin`
rather than weakening this default.

**Rejection behaviour.** Any failed Origin check closes the handshake with
`4403` before the session cookie is even read, revealing no authentication
detail. A valid session cannot rescue an untrusted origin.

**Store-partitioned authorization** is unchanged and still enforced after the
Origin check: store comes from the session, and store A's broadcasts never reach
store B.

## 16. Audit actor integrity

- The audit/lifecycle actor is always the authenticated user id
  (`actor_type="STAFF"`, `actor_id=str(user.id)`).
- `X-Actor-Id` is neither read nor trusted for kitchen mutations.
- A client `actor_id` in a decision PATCH body is ignored.
- No password, session token, CSRF token, or token hash is ever written to an
  audit payload. Login/logout/logout-all are audited.

## 17. CLI user administration

`scripts/manage_staff_users.py` (the only supported staff-admin path until a UI
exists):

| Command | Effect |
|---------|--------|
| `ensure-roles` | Create the canonical roles idempotently. |
| `create --username --role --store-id` | Create a user; password via `getpass`; validates role/store/uniqueness/policy/operational-store. |
| `list` | Safe fields only (id, username, role, store, active, locked, last login, active session count) — never hashes. |
| `disable --user-id` | Disable and revoke all sessions atomically. |
| `enable --user-id` | Re-enable and clear lock. |
| `reset-password --user-id` | New hash via `getpass`, update `password_changed_at`, clear lock, revoke all sessions. |
| `revoke-sessions --user-id` | Revoke all active sessions. |

Passwords are read via `getpass` (never argv/shell history). No web API creates
or edits users.

## 18. Turkish owner & kitchen UX

Login screen: `Kullanıcı adı`, `Şifre`, `Giriş Yap`. States: `Giriş yapılıyor…`,
`Kullanıcı adı veya şifre hatalı.`, `Hesabın geçici olarak kilitlendi. Lütfen
daha sonra tekrar dene.`, `Oturum açılamadı. Lütfen tekrar dene.`. Session expiry:
`Oturumun sona erdi. Lütfen yeniden giriş yap.`. Forbidden role:
`Bu alana erişim yetkin yok.`. Logout: `Çıkış Yap`. Loading: `Yükleniyor…`.
On load each app calls `/auth/me` with `credentials: "include"`, shows a loading
state, then renders login or the role-gated app. Protected requests use
`credentials: "include"`; mutations send `X-CSRF-Token`. The session token is
never placed in localStorage/sessionStorage/URL.

## 19. CORS & trusted origins

- `allow_credentials=true`, never a wildcard origin (a credentialed `*` is both
  invalid and unsafe; the allow-list is always explicit).
- `STAFF_TRUSTED_ORIGINS` (owner + kitchen) and `PUBLIC_TRUSTED_ORIGINS`
  (customer) are configurable; CORS allows their union.
- Login and authenticated `POST/PUT/PATCH/DELETE` validate Origin/Referer against
  the staff origins; public QR/menu/order endpoints stay usable and do not
  inherit staff origin rules.
- The **WebSocket handshake** validates `Origin` against the same
  `STAFF_TRUSTED_ORIGINS` (see §15a) — CORS does not cover the WS upgrade, so
  this is a separate, explicit check.
- Production values come from the environment; no production domains are
  hardcoded. See `apps/api/.env.example`.

## 20. Deployment environment variables

See `apps/api/.env.example`. Key values: `ENVIRONMENT=production` (forces Secure
cookies), `STAFF_TRUSTED_ORIGINS`, `PUBLIC_TRUSTED_ORIGINS`,
`ALLOW_MISSING_WEBSOCKET_ORIGIN` (default `false` — must stay `false` in
production so a missing WS `Origin` is rejected), cookie name/samesite, session
lifetimes, lockout thresholds, password policy, `DATABASE_URL`.

| Setting | Default | Meaning |
|---------|---------|---------|
| `STAFF_TRUSTED_ORIGINS` | `http://localhost:3001,http://localhost:3002` | Exact staff origins for CORS, HTTP origin checks, and the WS handshake. Never `*`. |
| `PUBLIC_TRUSTED_ORIGINS` | `http://localhost:3000` | Public customer origin(s); never inherit staff CSRF/origin rules. |
| `ALLOW_MISSING_WEBSOCKET_ORIGIN` | `false` | Allow a WS handshake with no `Origin` (non-browser clients). Keep `false` in production. |

## 21. Initial staff-account setup

Safe first deployment:

1. Back up PostgreSQL.
2. Configure staff origins and cookie security (`ENVIRONMENT=production`, HTTPS).
3. `alembic upgrade head`.
4. `python scripts/manage_staff_users.py ensure-roles`.
5. `... create --username owner01 --role OWNER --store-id <id>` (password via getpass).
6. Create KITCHEN users similarly.
7. Optionally create a CASHIER user (no payment access yet).
8. Verify no default password exists (none is created by migration/CLI).
9. Verify owner login, kitchen login, cross-role denial, store-scoped orders,
   WebSocket updates. Revoke test sessions. Confirm public QR ordering still works.
10. **CSWSH check:** while logged in, attempt a WebSocket connection to
    `/ws/kitchen` from an **untrusted origin** (e.g. a browser tab on a different
    site, or `wscat`/DevTools sending `Origin: https://evil.example.com`) and
    verify the handshake is **rejected** (close `4403`, no `initial_state`).
    Confirm `ALLOW_MISSING_WEBSOCKET_ORIGIN=false` in the deployed environment.
11. Do not claim rollout complete without real-device/real-network testing.

## 22. Session revocation workflow

- Logout revokes the current session; logout-all revokes every session for the
  user. `disable`, `reset-password`, and `revoke-sessions` CLI commands revoke all
  sessions. Revoked rows are retained (with `revoked_at`/`revoked_reason`) for
  short-term forensics.

## 23. Password-reset workflow

`reset-password --user-id <id>` replaces the hash, sets `password_changed_at`,
clears lock state, and revokes all existing sessions — the user must log in again
everywhere.

## 24. Migration & rollback

Revision `a7d3f9b21c05` (down_revision `f7a1c2b9d8e3`) adds: user security
columns; a case-insensitive unique index on `lower(username)` (preceded by a
collision preflight); the `auth_sessions` table (unique `token_hash`, indexed
`user_id`/`expires_at`, FK `users` ON DELETE CASCADE); `owner_decisions.store_id`
with composite PK `(store_id, decision_id)`, FK to `stores`, and index. It
preserves existing users/orders/decisions, inserts no credentials, and downgrades
cleanly (verified upgrade → downgrade → upgrade with data preserved). Alembic
remains single-head.

## 25. Tests & verification

New suites: `test_auth.py` (passwords/login/lockout/sessions/CSRF/logout),
`test_rbac.py` (permission matrix, 401 vs 403), `test_store_isolation.py`
(two-store kitchen/owner/decision isolation, client-store override, inventory
fail-closed), `test_actor_integrity.py` (authenticated actor, header/body actor
ignored), `test_ws_auth.py` (WebSocket Origin/CSWSH allowlisting, missing-origin policy,
auth + partitioning + revoked reconnect, credentialed-CORS no-wildcard, frontend
WS URL has no credential/store query).
Existing suites updated to authenticate. See §14 of the task report for the full
verification matrix.

## 26. Future staff-management UI

A later authenticated management feature will replace the CLI for day-to-day user
administration. MANAGER is intentionally **not** granted user-management
capability in this branch.

## 27. Future store-scoped inventory

`refactor/store-scoped-inventory`: add `store_id` to `ingredients`,
`ingredient_stock`, and stock movements so inventory analytics and stock-risk
signals are per-store, removing the single-store fail-closed constraint.

## 28. Future payment settlement workflow

`feat/payment-settlement-workflow`: the CASHIER role and session/CSRF system are
already in place. A future `cashier_user_id` will record who settled an order.
Payment status will be a separate concern — preparation status is **not**
overloaded with payment state.
