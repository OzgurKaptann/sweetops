# Store Setup and Menu Provisioning (v1)

**Status:** implemented on `feat/store-setup-and-menu-provisioning`
**Migration:** none — see §8
**Addresses:** [RUNTIME_PRODUCT_GAP_REVIEW.md](RUNTIME_PRODUCT_GAP_REVIEW.md) F-13
(partially — see §9), and the owner-facing half of
[CUSTOMER_MENU_SCOPING.md](CUSTOMER_MENU_SCOPING.md)
**Related:** [SECURE_QR_TABLE_CONTEXT.md](SECURE_QR_TABLE_CONTEXT.md) ·
[STORE_SCOPED_INVENTORY.md](STORE_SCOPED_INVENTORY.md) ·
[REAL_USE_READINESS_ROADMAP.md](REAL_USE_READINESS_ROADMAP.md) P0-E

---

## 1. The problem

Migration `a9e4c7b25d13` made the customer menu **fail closed**: a product reaches
a guest only through a `store_products` publication row, nothing was backfilled,
and an unprovisioned branch serves `"products": []`. That was the right boundary,
and it left the shop with no way through it:

| To do this… | Supported path before this branch |
| --- | --- |
| Put a product on a branch's menu | Edit `scripts/seed_demo_data.py`, or a psql prompt |
| Take one off | Same |
| Mark an item sold out for today | Same |
| Reorder the menu | Same |
| Add a table | Same |
| Give a table a QR sticker | `python scripts/manage_qr_tokens.py issue --table-id 5` on the DB host |
| Find out why the customer menu is empty | Read the source |

The last row is the sharp one. A guest's phone shows the same calm Turkish empty
state whether the branch published nothing or the API is down, so **an owner
cannot tell a correctly fail-closed shop from a broken one.** Only the shop side
can answer that, and there was no shop side.

---

## 2. What this branch adds

An authenticated, role-gated, store-scoped surface — `/owner/setup/*`,
`/owner/menu/*`, `/owner/tables/*` in the API, and `/setup` in owner-web — that
lets an OWNER or MANAGER:

* see a **readiness checklist** that says *why* the customer menu is empty;
* create and edit catalog products (name, category, price, active state);
* **publish** a product to their own branch's menu and **withdraw** it;
* switch a published item **off for the day** and back on;
* set its **menu order**;
* list tables, add a table, rename one;
* **issue** or **rotate** a table's QR sticker, with the link revealed exactly once.

It is *v1*: deliberately not a wizard, not staff management, not per-store pricing,
not ingredient/recipe authoring. §9 states what is still missing, by name.

---

## 3. API

All routes are under the existing `/owner` prefix
(`apps/api/app/routers/owner_setup.py`).

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/owner/setup/status` | `setup:read` |
| GET | `/owner/menu/products` | `setup:read` |
| POST | `/owner/menu/products` | `setup:manage` |
| PATCH | `/owner/menu/products/{product_id}` | `setup:manage` |
| POST | `/owner/menu/products/{product_id}/publish` | `setup:manage` |
| POST | `/owner/menu/products/{product_id}/unpublish` | `setup:manage` |
| PATCH | `/owner/menu/products/{product_id}/availability` | `setup:manage` |
| PATCH | `/owner/menu/products/{product_id}/sort-order` | `setup:manage` |
| GET | `/owner/tables` | `setup:read` |
| POST | `/owner/tables` | `setup:manage` |
| PATCH | `/owner/tables/{table_id}` | `setup:manage` |
| POST | `/owner/tables/{table_id}/qr-token` | `setup:manage` |
| POST | `/owner/tables/{table_id}/rotate-qr` | `setup:manage` |

There is **no** `GET /owner/tables/{id}/qr-link`. See §6.

### Store scope

The branch is always `staff.store_id`, from the authenticated session. Every
request model in `app/schemas/store_setup.py` sets `extra="forbid"`, so a body
carrying `store_id` is **rejected with a 422**, not silently ignored — ignoring it
would leave a client believing it had published onto another branch's menu and
cheerfully told so. There is no store query parameter and no "all branches" view;
the absence of the parameter is the security property, exactly as in
`routers/inventory.py`.

A storeless session is refused. In practice it is refused *earlier* than this
router: `auth_service.resolve_session` already rejects an operational role with no
`store_id`, so the caller gets a 401. The router's own `no_store_assigned` 403 is
kept as a second line for a future role that is allowed to be storeless, and is
tested directly.

### Permissions

Two new named permissions in `app/core/permissions.py`, granted to **OWNER and
MANAGER only**:

* `setup:read` — the readiness checklist, the branch's publication state, its tables.
* `setup:manage` — every mutation above.

Its own authority rather than a reuse of `owner:read` or `inventory:adjust`:
publishing decides what a guest can order and rotating a QR invalidates a printed
sticker, and neither is a stock movement. KITCHEN and CASHIER hold neither, so a
cook cannot rewrite the menu and a cashier cannot kill a sticker.

### Origin, CSRF, Idempotency-Key

Every mutation is a state-changing method, so `require_permission` enforces a
trusted `Origin` and a valid double-submit CSRF token. Tested.

**`Idempotency-Key` is deliberately not required here.** The reason, stated so
nobody has to guess whether it was forgotten:

* The publication routes are **naturally idempotent**. Publishing twice leaves one
  row; withdrawing twice leaves none; setting availability to the value it already
  holds changes nothing. Each answers `200` with `changed: false`, so a
  double-click is a no-op rather than a second red banner.
* **Creation is not idempotent**, and is guarded instead by a **duplicate-name
  check** (409 `product_name_taken`, case-insensitive, chain-wide) and a
  duplicate-table-number check (409 `table_number_taken`, per branch). A
  double-submitted form finds the record that already exists.
* Adding an idempotency-key ledger for *configuration* would mean a migration and
  a second append-only table for data that moves no stock and no money. That
  trade was not worth taking in v1. The residual race is named in §9.

---

## 4. Product model, unchanged

Nothing in this branch changes what the customer menu means. It writes the same
rows `menu_service.list_menu_products` reads:

| Column / table | Grain | Set by |
| --- | --- | --- |
| `products.is_active` | catalog, chain-wide | `PATCH /owner/menu/products/{id}` |
| `store_products` (row exists) | (branch, product) | publish / unpublish |
| `store_products.is_available` | (branch, product) | availability |
| `store_products.sort_order` | (branch, product) | sort-order |

`store_setup_service.on_customer_menu()` is the **one** place the visibility
predicate is written on the setup side, and it is the same three-way condition the
customer menu joins on (published ∧ available ∧ active). The API returns
`on_customer_menu` computed server-side; owner-web renders it and never re-derives
it, so the day a fourth condition joins the predicate a screen cannot quietly
disagree with a guest's phone.

### Behaviours worth stating

* **Creation publishes nothing** unless `publish_to_current_store: true` is sent,
  and that flag can only ever mean the caller's own branch. Automatically
  publishing new catalog rows everywhere is the exact shape that put eight
  `TestWaffle` rows one render away from a customer's phone.
* **Unpublish deletes the offering row.** The product, its price and every past
  order line survive; only this branch's decision to sell it goes. The guest-side
  effect is immediate and total — the menu join has nothing to join to, and
  `order_service` refuses the product at submit time even for a phone still showing
  the old list.
* **Availability keeps the publication.** "Bugün kalmadı" preserves the row *and*
  the branch's menu order, so tomorrow is one toggle rather than a re-publish that
  lands the item at the bottom of the board.
* **Deactivation is chain-wide and beats publication.** An inactive product
  disappears from every branch's menu even where a live, available offering row
  points at it. Tested through the public menu, not merely against the row.
* **Publishing an inactive product is allowed.** The branch has decided to sell it;
  it still will not reach a guest. Refusing would force a manager to reactivate an
  item chain-wide before they could arrange their own menu.
* **New publications append.** `sort_order` = current max + 1, so publishing does
  not silently reorder a board somebody already arranged.

---

## 5. Setup readiness

`GET /owner/setup/status` answers four questions, in the order a shop opens, each
with the count behind the boolean:

| key | Question |
| --- | --- |
| `has_table` | Is there a table at all? |
| `has_table_qr` | Can it be scanned — does every table have a live sticker? |
| `has_published_product` | Has anything been published to *this* branch? |
| `menu_ready` | Is anything on that menu orderable *right now*? |

The last two are separate on purpose: a branch that published five items and
switched all five off for the day passes one and fails the other, and the fixes are
nothing alike. `ready_for_customer_orders` is true only when all four are.

`catalog_active_products` is the one chain-wide number in the response, and it is
there to resolve the specific confusion this screen exists for: *"there are
fourteen products in the system, why is my menu empty?"* It is a count, never a
list, and it reveals nothing about which branch sells what.

Every `label`/`detail` is written server-side in Turkish. owner-web does not keep a
second copy: the backend knows whether two of three tables have a sticker, and a
client-side template would drift the first time the rule changed.

---

## 6. Tables and QR

Table management is thin because the model is: create, rename, list, issue,
rotate.

### The QR link is shown exactly once — and that is the point

`qr_token_service` stores only a **SHA-256 hash** of a raw token and returns the
cleartext value once, at issuance. So:

> **There is no endpoint that can return a scannable link for an existing
> sticker.** That is not a missing feature and not an oversight — it is
> cryptographically impossible, and it is the property that makes a database leak
> useless.

Consequences, all of them deliberate:

* `GET /owner/tables` carries **no** `qr_url`. It carries `token_prefix` — the
  non-secret leading fragment, which lets a manager match a record to the sticker
  physically on the table and cannot be scanned — plus `has_active_qr`,
  `qr_created_at` and `qr_last_used_at` (a table nobody has scanned in weeks is
  usually a sticker that fell off).
* `POST /owner/tables` issues the first token and returns the link **once**, on
  that response.
* `POST /owner/tables/{id}/qr-token` mints a first sticker for a table that has
  none; a table that already has one is refused (409 `qr_token_already_active`)
  and the manager is pointed at rotation.
* `POST /owner/tables/{id}/rotate-qr` replaces the sticker. **The printed code on
  that table stops working immediately.**

The URL format is exactly what `scripts/manage_qr_tokens.py` prints and what
`apps/customer-web/src/lib/qr-session.ts` parses:

```
{CUSTOMER_WEB_BASE_URL}/#qr=<raw token>
```

The token is in the **fragment**, never a query string: a fragment is not sent to
the server on the initial page request, so a long-lived bearer token cannot leak
into web-server, proxy, CDN or platform access logs. A test asserts both that
`/#qr=` is present and that `?qr=` is not, and then resolves the produced link
through `POST /public/qr-context/resolve` to prove the sticker actually works.

### Rotation is supported, because the existing model supports it safely

`qr_token_service.rotate_token` locks the parent table, revokes the current ACTIVE
token, inserts the replacement and links the lineage in one transaction, with a
partial unique index (`uq_table_qr_tokens_one_active_per_table`) as the backstop.
Nothing is deleted, so the record of which sticker was live when survives. No
schema change was needed, so rotation is exposed rather than deferred.

Renaming a table does **not** touch its token: a typo fix must not invalidate a
printed sticker.

### What tables still cannot do

`tables` has no `is_active` column, so a table cannot be closed or retired — only
renamed. Inventing the column here would be a schema decision smuggled in under a
rename. Named as remaining work in §9.

---

## 7. owner-web

New route **`/setup`**, linked from the dashboard header as *"Menü ve Masalar →"*.
Three panels plus two dialogs:

* **`ReadinessChecklist`** — the four checks, each with the word "Tamam"/"Eksik"
  as well as a colour (a tick with no text is unreadable to anyone who cannot
  distinguish the colours, and this screen is used once, under pressure, on
  somebody's first day), plus the empty-menu explanation.
* **`MenuProductsPanel`** — the branch's menu on top, the rest of the catalog
  below. Both halves are needed: a manager who can only see what they already
  published cannot publish anything else, which is the position every branch is in
  the moment the fail-closed menu ships. Per row: availability toggle, unpublish,
  a menu-order input (committed on blur/Enter, so typing "12" does not fire a
  request for position 1 first), and for catalog rows publish and reactivate.
* **`TablesPanel`** — tables, QR state in words, prefix, last scan, and
  issue/rotate. A permanent footnote explains why a link cannot be shown again.
* **`ProductFormModal`** — name, category, price, and an explicit checkbox
  *"Bu ürünü hemen **<branch>** şubesinin menüsüne ekle"* naming the branch. Price
  accepts Turkish input (`129,90`) and is normalised to `129.90` as a string, never
  through a float.
* **`QrLinkModal`** — the one-time link, with the warning first, selectable text as
  well as a copy button (a clipboard write can silently fail), and, after a
  rotation, a red line saying the old sticker is dead.

Three behaviours worth naming:

1. **Every mutation reloads from the server.** Optimistic state would let this
   screen disagree with the guest's phone, which is the one thing it exists to
   prevent.
2. **Only destructive directions confirm** — unpublish, chain-wide deactivation,
   and QR rotation each get their own sentence naming what is lost. Publishing, or
   re-opening a sold-out item, needs no ceremony.
3. **No raw wire value is rendered.** Statuses become Turkish in `setup-view.ts`;
   errors become Turkish in `setup-errors.ts`, which reuses `looksDisplaySafe`
   from the inventory screen rather than duplicating it, so a proxy's HTML 502 or
   an `IntegrityError` string can never reach a manager.

---

## 8. Schema and dependencies

**No migration. No new dependency. No model change.**

Everything is written through the existing `products`, `store_products`, `tables`
and `table_qr_tokens` tables. `verify_release_readiness.py` still reports a single
Alembic head. The only non-router additions are two permission constants, one
schema module, one service module, and the Turkish strings in
`app/core/messages.py`.

`tables.qr_code` — a legacy UNIQUE column that predates the token model and is not
a credential (nothing reads it to authorize anything) — is filled with a
non-secret `store-{id}-{uuid4hex}` value so the constraint is satisfied and no
rename or re-created label can ever collide with a value an earlier table left
behind.

---

## 9. What is NOT addressed

Named so nobody reads a wider claim into this branch.

**Still open, and this branch does not touch them:**

* **No onboarding wizard.** There is no guided first-run flow, no "set up your
  shop" sequence, no progress persistence. There is a checklist that tells you
  what is missing and controls that let you fix it.
* **No staff invitations or staff management.** `scripts/manage_staff_users.py` is
  still the only supported way to create an account or reset a password. It was
  explicitly out of scope.
* **No store creation.** A *store* row still comes from a seed or a psql prompt.
  This surface provisions the branch a manager already belongs to; it cannot bring
  a new branch into existence.
* **No per-store pricing.** Still P1-B. A branch publishes the chain's product at
  the chain's price; `store_products` has no price column and this branch did not
  add one.
* **No ingredient or recipe authoring.** Ingredients, their stock and their
  consumption are untouched. A product created here has no recipe, which means it
  consumes only what the guest selects — the existing behaviour, unchanged.
* **No printable QR sheet.** The link is copyable text; nothing renders a QR image
  or a per-table print layout. Named in P0-E and still unbuilt.
* **No table closing/retirement.** `tables` has no `is_active` column (§6).
* **No production deployment packaging, hosting, TLS, CI, monitoring or backups.**

**Accepted risks in what *is* built:**

* **Duplicate-create race.** The duplicate-name and duplicate-table-number guards
  are application checks, not database constraints. Two genuinely simultaneous
  identical creates could both pass. Worst outcome: a duplicate catalog row a
  manager can retire, or two tables with the same label. No money and no stock is
  involved. Fixing it properly means a unique index — a migration, and one that
  could fail on the existing development database's fourteen product rows — so it
  is named rather than smuggled in.
* **A lost QR link costs a rotation.** If a manager closes the one-time dialog
  without saving the link, the only way to get a scannable code again is to rotate,
  which invalidates whatever is printed on that table. This is inherent to hashing
  the token and is the correct trade; the UI says so in three places.
* **Rotation has no undo.** By design — the revoked token is never resurrectable.
  The confirmation names the consequence in the shop's own terms.
* **The catalog is chain-wide and every branch's manager can edit it.** Renaming or
  retiring a product affects every branch. This is the existing product model, not
  a change made here, but this branch is the first surface from which a manager can
  actually do it. The chain-wide consequence is spelled out in the confirmation
  text; a per-branch authority boundary over the shared catalog is a P1-B question.

---

## 10. Test coverage

**Backend — `apps/api/tests/test_store_setup_and_menu_provisioning.py` (35 tests).**
Nothing matches on a product name, and every publication assertion is checked
*through the public customer menu*, not merely against a row: a publication API
that wrote rows the customer menu did not honour would pass a row-level test and
fail a shop.

* Authorization: unauthenticated 401; KITCHEN and CASHIER 403 on every route;
  MANAGER allowed; a storeless account reaches nothing; the router's own
  `no_store_assigned` guard answers in Turkish.
* Mutation contract: missing CSRF, wrong CSRF, and an untrusted Origin each 403 —
  and nothing was published by any of them.
* Reads: publication state is this branch's only; the list is deterministic.
* Creation: does not leak onto any menu; publishes to the current store only when
  asked; duplicate name refused (case-insensitively); empty name and non-positive
  price refused; a smuggled `store_id` is a 422, not an ignored field.
* Publication: publishing puts it on the guest's menu; publishing twice is a no-op;
  unpublishing removes it from the guest's menu and is idempotent; unavailable
  disappears but keeps its publication *and its sort order*; a deactivated product
  disappears even with a live offering row; availability and sort-order need a
  publication first; sort order drives the guest's list order; negative sort order
  refused; unknown product is a safe 404.
* Cross-store: a manager cannot publish onto another branch's menu and cannot
  withdraw another branch's publication — and the tests say *why*: there is no
  request that can name the other branch.
* Tables/QR: the list is branch-scoped; **no raw token appears anywhere in it**; a
  created table's link resolves through the public QR endpoint and uses `#qr=` not
  `?qr=`; duplicate table number refused; renaming keeps the sticker working;
  another branch's table is a 404 and is left untouched; a second issue is refused;
  rotation kills the old sticker, keeps exactly one ACTIVE token and preserves the
  history row.
* Readiness: a new branch fails every check and each fix flips exactly the check it
  fixed; a table with no sticker is counted; the status is never cached.

**owner-web — 3 new files, 48 tests** (`setup-view.test.ts`, `setup-api.test.ts`,
`setup-errors.test.ts`), taking the suite from 175 to 223. `fetch` is stubbed; no
network access occurs.

* View: the four menu states and why they are not one boolean; no raw enum in any
  rendered string; nameless/price-less rows; the checklist including an
  *unrecognised* check (a checklist that hides what it does not know about hides
  the step nobody has thought about); the screen trusts the server's readiness
  verdict over its own tally; the empty-menu explanation differs between "nothing
  published" and "everything switched off"; table rows carry a prefix and no field
  that could hold a link; the create form's exact body, the opt-in publish flag,
  and rejected prices; each destructive confirmation names its own consequence.
* API: every mutation sends CSRF and credentials; **no body ever carries a
  `store_id`**; verb-only routes send no body at all; each control hits its own
  endpoint (a publish toggle wired to unpublish would pass every view test and take
  a shop's menu down); a dropped connection is *uncertain* on a mutation and a
  plain error on a read; a non-JSON 502 body never leaks.
* Errors: every known code resolves to Turkish; the codes this screen can actually
  produce are all covered; unsafe server strings are replaced, not displayed.
