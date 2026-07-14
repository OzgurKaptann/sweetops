# Inventory Threshold Alerts

SweetOps can already say, exactly, what is on every branch's shelves. Reservations,
consumption, waste, manual adjustments, purchase receipts, store-to-store transfers and
physical counts all land in an append-only ledger that reconciles against the summary
to the gram.

What it could not do is tell a manager that the chocolate is running down **while there
is still time to do something about it**. Every existing surface answers "what have we
got?"; none answers "what is about to become a problem?" The branch found out at the
counter, with a customer waiting.

This feature adds the early warning: per-branch threshold configuration, a status for
every ingredient, and an alert screen that says which of them need a decision today.

---

## Why these are not purchase orders

This is the boundary the whole design is built around, so it is worth stating before
anything else.

A threshold alert is **visibility and control**. It tells a human what is happening. It
does not act.

| It does | It does not |
| --- | --- |
| Classify an ingredient as healthy / low / critical / out | Order anything |
| Suggest how much would restore the target level | Name, choose or contact a supplier |
| Show which branch needs attention | Reserve, allocate or commit stock |
| Record who set a warning level, and why | Write to the inventory ledger |

`recommended_restock_quantity` is the one number that could be mistaken for a purchase
instruction, and it deliberately is not one. It is `target − available`, rendered in a
column, read by a manager, and acted on by nobody. Nothing downstream consumes it. The
Turkish copy calls it **"Önerilen tamamlama"** and never "sipariş", for the same reason.

Supplier management, purchase orders, automatic reorder and forecasting are all
deferred (see the end of this document). None of them is a small extension of this
feature — each is a workflow with its own approvals, money and accountability, and
bolting any of them onto an alert screen would mean a warning level quietly becoming a
spending decision.

---

## Threshold definitions

Three levels, configured **per (store, ingredient)**. Store-scoped for the same reason
the quantities are: Kadıköy sells twice the pistachio Beşiktaş does, so "3 kg is low" is
a true statement about one branch and a false one about the other. A chain-wide
threshold would either cry wolf in the quiet branch or stay silent in the busy one.

| Field | Meaning |
| --- | --- |
| `critical_quantity` | At or below this, the branch is **operationally critical** |
| `minimum_quantity` | At or below this, stock is **low** and should be reviewed / reordered |
| `target_quantity` | The level replenishment should aim **back up to** |

`target_quantity` is **not an alert level**. It answers "how much should I buy?", not
"am I in trouble?". A row with only a target configured is therefore `NOT_CONFIGURED` —
nothing has been said about when to warn — even though its recommended top-up is
perfectly computable. Treating a target as an alert level would fire a warning at every
level below the replenishment point, which is almost always.

### Null policy: NULL means NOT CONFIGURED, and NULL is not zero

Every threshold is nullable, and `NULL` means **nobody has said what "low" means for
this ingredient in this branch**.

It emphatically does not mean zero. Zero is a real and useful threshold — *"warn me only
when it is actually gone"* — and a manager who deliberately sets it must not have their
decision rendered as an absence of one. The two are different facts and they get
different representations:

* the alert screen shows `—` for an unconfigured threshold and `0` for a zero one;
* the request body sends `null` for the first and `"0"` for the second;
* the idempotency fingerprint hashes them differently, so a retry of one can never be
  mistaken for the other;
* the audit payload records `null`, never `"0"`.

This is why an unconfigured row reports `NOT_CONFIGURED` rather than `HEALTHY`. An
unconfigured threshold is **missing information**, and turning missing information into
an all-clear is how a monitoring system starts lying to the person relying on it.

Nothing was backfilled. The legacy `reorder_level` column — a coarse hint the customer
menu multiplies by 1.5 to colour a badge, which `seed.py` sets to a flat 15% of opening
stock for every ingredient — was deliberately **not** promoted into `minimum_quantity`.
Turning a seeded guess into an operational alert level would fill the new screen with
warnings nobody chose, and the first thing anybody does with an alert screen they do not
believe is stop reading it. `reorder_level` stays where it is, doing what it did.

### Validation

```
critical_quantity >= 0
minimum_quantity  >= 0
target_quantity   >= 0

critical <= minimum       (when both are set)
minimum  <= target        (when both are set)
critical <= target        (when both are set)
```

The ordering rules are **pairwise**, so that any *partial* configuration is still
checked. Every combination is legitimate — critical alone, minimum + target, all three —
and whichever ones you did configure must make sense together.

`critical <= target` is not redundant: when `minimum` is NULL, it is the only constraint
relating critical to target at all.

Why these are refused rather than merely discouraged:

* **A negative threshold** promises an alert that can never fire — no quantity can fall
  below zero. It is a control that silently does nothing, which is worse than no control
  because the manager believes they are covered by it.
* **critical > minimum** inverts the ladder: the ingredient reaches CRITICAL before it
  ever reaches LOW, so the "go and look at this" warning the manager set up to buy
  themselves time never appears at all.
* **minimum > target** means restocking to target lands the branch straight back into
  LOW — a replenishment that is a warning the moment it arrives.

Each rule is enforced in **three** places: the form (so the manager is told which rule
they broke without a round-trip), the service (so the answer is a Turkish sentence and
not a constraint name), and a `CHECK` constraint (so it is an invariant and not a
convention a refactor can delete).

---

## Status logic

```
BELOW_RESERVED    on_hand < reserved
OUT_OF_STOCK      available <= 0
CRITICAL          available <= critical_quantity      (if configured)
LOW               available <= minimum_quantity       (if configured)
HEALTHY           above every configured alert threshold
NOT_CONFIGURED    neither critical nor minimum is configured
```

Evaluated **in that order**, strongest operational incident first.

### Why available, not on-hand

This is the single most important decision in the feature.

```
available_quantity = on_hand_quantity - reserved_quantity
```

Reserved stock is **already promised to accepted orders**. It is not stock the branch can
still use for new demand — it belongs to a customer who has been told "yes" and is
waiting. A shelf holding 8 kg of chocolate with 7.5 kg promised has 0.5 kg it can
actually do anything with.

Judging the status on on-hand would report that shelf as healthy, and the branch would
cheerfully accept an order it cannot cook. So the threshold is tested against
**available** — the same number order acceptance itself tests against (`check_availability`
in `inventory_service.py`), for exactly the same reason.

The consequence is deliberate and worth stating plainly: **reserved stock alone can push
an ingredient into LOW or CRITICAL while the shelf still looks full.** That is not a bug
in the alert — it is the alert doing its job.

### How reserved stock affects alerts

| Situation | on-hand | reserved | available | Status (minimum = 5) |
| --- | --- | --- | --- | --- |
| Quiet shelf | 10 | 0 | 10 | `HEALTHY` |
| Busy shelf, mostly promised | 10 | 6 | 4 | `LOW` |
| Everything promised | 10 | 10 | 0 | `OUT_OF_STOCK` |
| Empty shelf | 0 | 0 | 0 | `OUT_OF_STOCK` |

The third row is the one that surprises people: the shelf is *not* empty, and the branch
is *out of stock* — because there is nothing left to promise anybody new.

### BELOW_RESERVED

`on_hand < reserved` means the branch has promised stock it does not physically hold.

`ck_stock_reserved_le_on_hand` makes this **unrepresentable**, so it should never appear.
It is classified anyway, and classified *first*, because if physical reality or a future
bug ever produced it, it is not a stock level — it is an incident. A manager who reads
"Stokta yok" goes and orders more; one who reads "Ayrılmış stoktan düşük" goes and looks
at the orders that cannot be fulfilled. Burying the second under the first would hide the
one row that needs a human today.

### OUT_OF_STOCK before NOT_CONFIGURED

An empty shelf is empty whether or not anybody got round to configuring a threshold for
it. A manager does not have to have set a level to be told there is none left.

### Recommended restock quantity

```
recommended_restock_quantity = target_quantity - available_quantity
```

`null` when no target is configured, and `null` when available already meets or exceeds
it — there is nothing to recommend, and a zero would render as a number in a column of
numbers and invite someone to order zero of something.

Measured against **available**, consistently with the status: stock already promised to
accepted orders will not be on the shelf to satisfy tomorrow's demand, so counting it as
if it were is how a branch under-orders.

Turkish: *"Hedef stok seviyesine ulaşmak için önerilen tamamlama miktarı"*. It is a
suggestion, not an order.

---

## API

### `GET /inventory/threshold-alerts`

Requires an authenticated staff session, `inventory:read`, and a store-assigned session.

Returns every **active ingredient this branch stocks** — including the ones nobody has
configured a threshold for, which report `NOT_CONFIGURED`. An alert screen that silently
omits the rows it has no opinion about is an alert screen that hides exactly the
ingredient nobody has thought about yet.

Per item:

```
ingredient_id, ingredient_name, unit
on_hand_quantity, reserved_quantity, available_quantity
critical_quantity, minimum_quantity, target_quantity
status, status_label
recommended_restock_quantity
last_movement_at
threshold_updated_at, threshold_updated_by_user_id
```

Plus a server-computed `summary` (the counts behind the cards, and
`total_recommended_restock`). The summary is computed on the server because it sums
decimal quantities, and a browser adding JSON number strings is how `0.1 + 0.2` ends up
on a stock report.

Optional `?status=CRITICAL` filters the **items**. It deliberately does **not** filter the
summary: the cards describe the *branch*, not the filter. A manager who filters to
"kritik" must still be able to see that four other ingredients are low — otherwise the
cards agree with the filter and hide the very thing they exist to surface.

`last_movement_at` is context, not a threshold: *"critical, and nothing has moved for nine
days"* and *"critical, and it was consumed twice this morning"* are the same status and
very different problems.

### `PATCH /inventory/stock/{ingredient_id}/thresholds`

Requires an authenticated staff session, a trusted Origin, a valid CSRF token,
`inventory:adjust`, a store-assigned session, and an `Idempotency-Key`.

```json
{
  "critical_quantity": "2.000",
  "minimum_quantity": "5.000",
  "target_quantity": "20.000",
  "reason": "Kış sezonu talebi arttı"
}
```

**The body states the COMPLETE threshold configuration, not a patch of it.** An omitted or
explicitly null field means that threshold is NOT CONFIGURED, and clearing one is a real
decision that gets its own log row and its own audit event.

This is deliberately not "update only the fields you mention". Partial-update semantics
would make a `null` ambiguous between *"leave this alone"* and *"clear this"* — and the
request hash that idempotency compares could not tell those two intents apart either, so
a retry could apply the wrong one.

What the request **cannot** carry:

* **`store_id`** — it comes from the session. There is no field for it, and
  `extra="forbid"` means a smuggled one is a **422**, not a silently ignored key. Silently
  ignoring it would leave a client believing it had configured another branch's alerts
  and cheerfully told so.
* **`ingredient_id`** — it comes from the path.
* **`actor_user_id`** — it comes from the session.
* **`status`** — a client does not get to declare an ingredient healthy. The server
  derives it.
* **any stock quantity** — see below.

Rules: `reason` is mandatory; quantities must be non-negative if provided; the ordering
rules hold across whichever are configured.

#### It does not change stock

The endpoint updates threshold columns and nothing else. It does not read a quantity for
writing, does not assign to `on_hand_quantity` or `reserved_quantity`, and **writes no
stock movement** — `update_thresholds()` never calls `_movement()`. `available_quantity`
is a generated column of the two it does not touch, so it cannot move either.

The response echoes the stock quantities back **unchanged**, which is what lets a client
see that nothing moved.

This is structural, not a promise: there is **no threshold movement type**, so a threshold
change is not a movement this schema can express. `ck_movement_type_domain` would refuse
the row even if some future service tried to write one.

---

## Permissions

No new permission. The existing inventory pair is exactly the right shape.

| Role | View alerts | Edit thresholds |
| --- | --- | --- |
| OWNER | ✅ | ✅ |
| MANAGER | ✅ | ✅ |
| KITCHEN | ✅ (`inventory:read`) | ❌ |
| CASHIER | ❌ | ❌ |

KITCHEN sees the shortage it has to cook around and cannot rewrite the levels — the same
line the inventory lifecycle already draws: a cook flagging a shortage is useful, a cook
silently redefining what counts as one is not.

Editing requires `inventory:adjust` — the same authority as waste, adjustment and
transfer. Not because a threshold touches stock (it does not), but because **a threshold
quietly lowered until it stops firing is how a branch walks into a stockout with its eyes
shut**, and that decision carries the same operational weight as writing stock off.

There is no cross-store threshold editing, and no store parameter anywhere. The absence
of the parameter is the security property.

---

## Idempotency

The threshold PATCH requires an `Idempotency-Key`, and it matters even though no stock
moves: **a retried form must not re-log the decision or re-stamp `threshold_updated_at`**.
That timestamp is what an owner reads to ask who moved a warning level and when, and it
is worthless if pressing the button twice moves it.

| Situation | Result |
| --- | --- |
| Same store + same key + same payload | Replays the original result. Nothing is written. |
| Same store + same key + **different** payload | **409** `idempotency_mismatch` |
| **Different store**, same key | Independent. Both succeed. |

The last row is why the uniqueness constraint is `(store_id, idempotency_key_hash)`. Two
branch managers working from the same printed run-book will legitimately send the same
key; that is a coincidence, not a replay, and Beşiktaş's update must never return
Kadıköy's result and quietly configure nothing.

A 409 on a changed payload is the right answer, not pedantry: replaying the original
would tell a manager who has just lowered the critical level to 2 kg that their change
succeeded, while the branch quietly keeps warning at 5.

**The raw key is never stored.** Only a SHA-256 digest of the key and of the canonical
request body, in `inventory_threshold_updates`. Neither is ever echoed back — a stored or
returned key is a replay token.

On replay, **nothing is written**: not the stock row, not the timestamp, and not a second
audit event. The response reports what *that* command did, and it did it once.

---

## Audit

Every applied threshold change writes exactly one `INVENTORY_THRESHOLDS_UPDATED` audit
event.

```
store_id
ingredient_id
old_critical_quantity, old_minimum_quantity, old_target_quantity
new_critical_quantity, new_minimum_quantity, new_target_quantity
actor_user_id
reason
```

The old values are what turn the log into an answer to the question an owner actually
asks: *"who lowered the critical level on chocolate, and what was it before?"*

An unconfigured threshold is recorded as `null`, never as `"0"` — writing zero there would
record a decision the manager did not make.

**Never logged:** the raw idempotency key, the request hash, the CSRF token, the session
token. An audit trail that leaks a replayable credential is a liability, not a control.

A **replay writes no audit event at all**. Two events would say two decisions were made.

The change log (`inventory_threshold_updates`) is **append-only**, guarded by the same
trigger as the ledger and the stock counts. A threshold decision that was got wrong is not
edited — it is superseded by making another one, and both stay on the record. Editing
would let today's manager rewrite what yesterday's manager decided, which is precisely
what somebody quietly disarming an alert would want to do.

---

## Owner UI

The alert panel sits at the **top** of `/inventory`, above the stock table. A manager
opening the screen is asking "what needs me today?", and burying the answer under a full
stock table is how it gets scrolled past.

**Summary cards** — `Kritik stok`, `Düşük stok`, `Stokta yok`, `Eşik tanımlı değil`, plus
`Ayrılmış stoktan düşük` only when it is non-zero (a permanent "0" card for something that
should never happen trains people to read past it). `Stok yeterli` is deliberately *not* a
card: a big number of healthy ingredients is exactly the reassurance that stops someone
reading the row that is not. `Toplam önerilen tamamlama` appears below when there is
anything to suggest.

**Threshold table** — `Malzeme`, `Kullanılabilir stok`, `Durum`, `Kritik eşik`,
`Minimum eşik`, `Hedef stok`, `Önerilen tamamlama`. It shows **available**, not on-hand,
because that is what the status was computed against — a full-looking on-hand figure beside
a "Düşük stok" badge looks like a bug and gets the alert ignored.

**Edit form** (`Eşik düzenle`) — Malzeme, Kritik eşik, Minimum eşik, Hedef stok, Sebep. It
pre-fills the thresholds already in force, because the body states the complete
configuration and a manager who came to change one level must not silently clear the other
two by leaving them blank.

Copy that is load-bearing:

| Situation | Turkish |
| --- | --- |
| Form hint | Eşikler stok uyarıları için kullanılır. **Bu işlem stok miktarını değiştirmez.** |
| Clearing | Boş bıraktığınız eşik tanımsız olur ve o seviye için uyarı verilmez. |
| Success | Stok eşikleri güncellendi. |
| Replay | Bu eşik güncellemesi daha önce kaydedilmiş. |
| Uncertain | Eşik güncellemesinin kaydedilip kaydedilmediği doğrulanamadı. Aynı işlemi tekrar göndermeden önce stok ekranını kontrol edin. |
| Generic failure | Eşikler güncellenemedi. Lütfen tekrar deneyin. |

The hint is the sentence that makes the form usable. A manager who is not certain that
setting a warning level leaves their stock alone will not set one — and an alert system
nobody configures never fires.

The **uncertain** message sends the manager to the **stock screen**, not the movement
ledger. A threshold update writes no movement, so a manager told to check the ledger would
find nothing, conclude it failed, re-enter it by hand, and thereby mint a new key and
re-log the decision.

### Status labels, and no raw enums

| Wire | Turkish |
| --- | --- |
| `BELOW_RESERVED` | Ayrılmış stoktan düşük |
| `OUT_OF_STOCK` | Stokta yok |
| `CRITICAL` | Kritik stok |
| `LOW` | Düşük stok |
| `HEALTHY` | Stok yeterli |
| `NOT_CONFIGURED` | Eşik tanımlı değil |

The wire value stays the stable English contract and is what the app *compares* against;
`thresholdStatusLabel()` in `lib/inventory-view.ts` is the only place it becomes screen
text, and an unrecognised status renders as **"Bilinmiyor"**, never as the raw enum. The
client translates from the status itself rather than trusting the server's `status_label`
string — its own guarantee that a raw enum cannot reach the screen, which holds even if a
future endpoint forgets to send a label.

No SQL constraint name, exception class, status code or stack trace is ever displayed.
Backend messages are shown only when they pass `looksDisplaySafe()`; otherwise the generic
Turkish line is used.

---

## Reconciliation and analytics

**A threshold is configuration, not stock.** It is the level at which a branch wants to be
warned — not a quantity anybody owns.

No threshold column appears anywhere in the reconciliation: not in the summary side, not
in the ledger side, not in the order lines. So a threshold **cannot cause a stock mismatch,
cannot contribute to one, and cannot mask one**. Editing one moves no stock, which is why
the endpoint writes no ledger movement at all.

The exclusions from analytics therefore needed **no special-casing anywhere**. They are a
consequence of the schema, not of anybody remembering:

* not waste,
* not a purchase receipt,
* not a transfer,
* not consumption (so consumption velocity is untouched),
* not a manual adjustment,
* not a physical stock count.

A threshold change is not a movement of **any** type, so it appears in **no** movement-based
report, present or future.

`scripts/reconcile_inventory.py` gained a sixth check, `audit_thresholds()`, which reports
incoherent thresholds (negative, critical > minimum, minimum > target, critical > target).
Every one of these is refused by a `CHECK` constraint, so a healthy database cannot produce
one; the check exists to catch what got in some *other* way — a manual SQL edit, a restore
from an inconsistent backup, a future migration bug.

These are **warnings**, and they deliberately **do not affect the exit code**. The script
exits non-zero when the **stock** is wrong, and a nonsensical warning level does not make
the shop's books wrong — it just makes its alert screen useless. Folding the two together
would mean a mis-set threshold produced a failing reconciliation, and the next person to
see a red build would learn that a red reconciliation does not necessarily mean the stock
is wrong, which is the one thing it must always mean.

Nothing in the reconciler mutates anything.

---

## Deferred

Explicitly **not** built here, and not partially built either:

* **Supplier management** — no vendor entity, no contacts, no lead times, no pricing.
* **Purchase-order management** — nothing is ordered. `recommended_restock_quantity` is a
  number on a screen, not a document, and no system consumes it.
* **Automatic reorder** — nothing acts on a threshold breach. A human reads it and decides.
* **Vendor selection.**
* **Forecasting** — the status is computed from the stock in front of it, not from a
  predicted future one. No demand model, no seasonality, no lead-time maths.
* **Scheduled alerts / background jobs** — statuses are computed on read. Nothing runs on a
  timer.
* **Email / SMS / WhatsApp notification** — the alert lives in the app. Nothing is sent
  anywhere.
* **Barcode scanning.**
* **Lot / expiry tracking** — a threshold is about *how much*, never *how old*.
* **Approval workflow** — a threshold change applies immediately, and is audited.

Each of these is a feature with its own accountability, not an extension of this one. In
particular, the moment a threshold breach can *spend money* without a human, everything
about the authorisation model above has to be re-examined — which is exactly why this
branch stops at telling a person what is happening.

---

## See also

* [INVENTORY_LIFECYCLE.md](INVENTORY_LIFECYCLE.md) — reservation, consumption, the ledger
* [STORE_SCOPED_INVENTORY.md](STORE_SCOPED_INVENTORY.md) — why stock (and thresholds) belong to a branch
* [PHYSICAL_STOCK_COUNT_WORKFLOW.md](PHYSICAL_STOCK_COUNT_WORKFLOW.md) — counting the shelf
* [OWNER_INVENTORY_MANAGEMENT_UI.md](OWNER_INVENTORY_MANAGEMENT_UI.md) — the screen these alerts live on
* [TURKISH_USER_FACING_LOCALIZATION.md](TURKISH_USER_FACING_LOCALIZATION.md) — the fixed vocabulary
