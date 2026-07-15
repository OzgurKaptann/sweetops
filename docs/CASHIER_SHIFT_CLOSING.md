# Cashier Shift Closing

## Why this exists

SweetOps already records every collection and refund in an append-only payment
ledger (see [PAYMENT_SETTLEMENT_WORKFLOW.md](./PAYMENT_SETTLEMENT_WORKFLOW.md)).
That ledger is financially correct, but it never answers the question a real shop
asks at the end of every day:

```
Kasiyer vardiyaya ne kadar nakitle başladı?
Vardiya boyunca ne kadar nakit aldı? Ne kadar kart aldı? Ne kadar iade yaptı?
Gün sonunda kasada beklenen nakit neydi? Kasiyerin saydığı nakit ne? Eksik/fazla var mı?
```

A **cashier shift** answers exactly that. It is a *reconciliation event* laid over
the existing ledger. It:

- **never** mutates a settled payment,
- **never** mutates a refund,
- **never** alters inventory,
- **never** creates an accounting entry.

It snapshots what the system *expected* at the moment of closing and compares it
with what the cashier physically *counted*.

## Shift lifecycle

```
OPEN  ──(close with a counted-cash figure)──▶  CLOSED   (frozen forever)
```

- **Open** — the cashier records the cash the drawer starts with
  (`opening_cash_amount`). One shift belongs to one cashier at one store.
- **Current** — `GET /cashier/shifts/current` returns the cashier's open shift (or
  `null`).
- **Close** — the cashier records the counted cash. The service computes the
  ledger snapshot for the shift's window, stores it, and freezes the row.

There are exactly two states, `OPEN` and `CLOSED`, enforced by a `CHECK`
constraint. There is no `PAUSED`/`SUSPENDED` fiction — the row and its close
snapshot are written in one transaction.

### One open shift per (store, cashier)

A partial unique index (`uq_cashier_shift_one_open`, on `(store_id,
cashier_user_id) WHERE status='OPEN'`) allows at most one open shift per cashier
per store. Two overlapping open windows would both claim the same windowed
payments — a double count — so a second open returns the existing shift instead of
creating another.

## Opening cash

`opening_cash_amount` is the bozuk para the cashier puts in the drawer at the
start of the shift. It may be **zero** (an empty drawer is valid) but never
negative (`ck_cashier_shift_opening_nonneg`).

## Closing cash count

`counted_closing_cash_amount` is what the cashier physically counts at the end.
Zero is valid; negative is refused (`ck_cashier_shift_counted_nonneg` and a 422 in
the service).

## Payment / refund snapshot

At close time the service computes, from the payment ledger, for the window
`opened_at <= t < closed_at`:

| Field | Derivation |
| --- | --- |
| `cash_payments_amount` | Σ this cashier's CASH settlements in the window |
| `card_payments_amount` | Σ this cashier's CARD settlements in the window |
| `cash_refunds_amount` | Σ refunds of this cashier's CASH settlements in the window |
| `card_refunds_amount` | Σ refunds of this cashier's CARD settlements in the window |
| `gross_payments_amount` | Σ **all** this cashier's settlements (CASH + CARD + any OTHER) |
| `total_refunds_amount` | Σ **all** refunds of this cashier's settlements |
| `net_collected_amount` | `gross_payments_amount − total_refunds_amount` |
| `expected_closing_cash_amount` | `opening_cash_amount + cash_payments_amount − cash_refunds_amount` |
| `cash_discrepancy_amount` | `counted_closing_cash_amount − expected_closing_cash_amount` |

### Attribution rule (documented, and matched by the reconciler)

Totals are derived by **`store_id + cashier_user_id + time window`**:

- **Payments** are the settlements this cashier collected in the window
  (`payment_settlements.cashier_user_id`, `completed_at` in window), classified by
  the settlement's own `payment_method`.
- **Refunds** are refunds of the money **this cashier collected** — joined through
  the settlement (`payment_settlements.cashier_user_id`), with the *refund's*
  `created_at` in the window. Refunds are performed by a MANAGER/OWNER, not the
  cashier, so they are attributed by *whose money was reversed*, not by who pressed
  the refund button. That is the figure the physical drawer actually loses.

`gross_payments_amount` and `total_refunds_amount` include **every** method
(CASH, CARD, and any future OTHER), so a new payment method flows into the money
totals without inventing a UI label for it.

### Discrepancy

`cash_discrepancy_amount = counted − expected`, and it is **signed**:

| Value | Label | Meaning |
| --- | --- | --- |
| `= 0` | **Denk** | the drawer matches |
| `< 0` | **Eksik** | the drawer is short |
| `> 0` | **Fazla** | the drawer has more than expected |

`expected_closing_cash_amount`, `net_collected_amount` and
`cash_discrepancy_amount` are the three snapshot values that may legitimately be
negative and are therefore **not** constrained non-negative. Every pure-sum total
(cash/card payments and refunds, gross, total refunds) *is* constrained
non-negative — a negative there would be corruption, not a real figure.

### Payments do not require an open shift

The existing cashier flow keeps working with or without an open shift. A close
simply *summarises* the ledger for its window. Enforcing "no payment without an
open shift" is a larger operational policy and is deliberately **out of scope**;
cashier-web shows a soft, non-blocking warning when no shift is open.

## Immutability

A `CLOSED` shift is a snapshot and is frozen by a trigger
(`trg_cashier_shifts_guard`) with no application-reachable bypass:

- `DELETE` — always refused (shifts are append-only history).
- `UPDATE` on a `CLOSED` shift — always refused (immutable; **cannot be
  reopened**).
- `UPDATE` on an `OPEN` shift — permitted only as the `OPEN → CLOSED` transition,
  and only if the opening snapshot (store, cashier, `opened_at`,
  `opening_cash_amount`, `open_note`, opened idempotency hashes) is unchanged.

Consequently a payment recorded **after** a close can never retroactively change
what the shift reported. The trigger is hardened exactly like the payment ledger's
(`SECURITY INVOKER`, pinned `search_path`, schema-qualified, no dynamic SQL,
`EXECUTE` revoked from `PUBLIC`).

## Idempotency

Opening and closing each require an `Idempotency-Key` header. Only SHA-256 hashes
of the key and of the canonical payload are stored — never the raw key or body.

- **Open**: same store + same key + same payload → the original shift
  (`idempotent_replay=true`); same key + different payload → `409`. Opening
  uniqueness is store-scoped (`uq_cashier_shift_store_open_idem`).
- **Close**: same shift + same key + same payload → the original close snapshot
  (`idempotent_replay=true`); same key + different payload → `409`; a *different*
  key against an already-closed shift → `409 already_closed`. The close writes onto
  the shift's own row, so its idempotency is inherently shift-scoped.

A replay never writes a second audit event and never recomputes the snapshot.

## Audit

Two audit actions are written, exactly once each (never on replay):

- `CASHIER_SHIFT_OPENED` — payload: `shift_id`, `store_id`, `cashier_user_id`,
  `opening_cash_amount`, `opened_at`.
- `CASHIER_SHIFT_CLOSED` — payload: `shift_id`, `store_id`, `cashier_user_id`,
  `opened_at`, `closed_at`, `opening_cash_amount`, and every computed snapshot
  figure (cash/card payments and refunds, gross, total refunds, net collected,
  expected cash, counted cash, discrepancy).

Never logged: the raw idempotency key, the request hash, the CSRF token, or the
session token.

## Permissions

Reuses the existing payment/owner permissions — no new permission was added.

| Action | Requirement |
| --- | --- |
| Open a shift | `payments:collect` (CASHIER, MANAGER, OWNER) + trusted origin + CSRF + Idempotency-Key |
| Close **own** shift | `payments:collect` |
| Close **another** cashier's shift | `payments:collect` **and** `owner:read` (MANAGER/OWNER only) |
| Read current / list / detail | `payments:read` |
| List/detail scope | OWNER/MANAGER (`owner:read`) see all shifts in their store; a CASHIER sees only their own |

Store is always derived from the session. Cross-store access is a non-disclosing
`404`, never a `403`. `owner:read` (held by OWNER and MANAGER, never by CASHIER) is
the supervisory gate for "see all shifts" and "close someone else's shift".

## API

All responses are `Cache-Control: no-store`.

```
POST /cashier/shifts/open            {opening_cash_amount, open_note?}          -> Shift
GET  /cashier/shifts/current                                                    -> {current_shift: Shift|null}
GET  /cashier/shifts?status&cashier_user_id&date_from&date_to&limit             -> {shifts: Shift[]}
GET  /cashier/shifts/{id}                                                       -> Shift
POST /cashier/shifts/{id}/close      {counted_closing_cash_amount, close_note?} -> Shift
```

Request bodies are strict (`extra="forbid"`): an unknown field is a `422`.
Currency is never accepted from the client (SweetOps is single-currency, TRY).

## Cashier-web behaviour

`ShiftPanel` sits at the top of the cashier screen:

- **No open shift** → shows `Açık vardiya bulunmuyor.` and an open form
  (`Açılış nakdi`, optional note). Opening cash is validated (≥ 0).
- **Open shift** → shows opened time, opening cash and cashier, plus a
  `Vardiya kapat` button that reveals the close form (`Kapanış nakit sayımı`,
  optional note).
- **Closed** → a summary card colour-coded by discrepancy (Denk / Eksik / Fazla)
  showing every snapshot figure.
- Every command sends an `Idempotency-Key`; a network-uncertain **close** shows a
  "check before retrying" message (never `başarısız`).
- The raw status enum is never rendered — `shiftStatusLabel` maps it to
  Açık/Kapalı, with `Bilinmiyor` for anything unknown.
- A missing open shift shows a **soft warning** beside the payment area; it never
  blocks collection.

## Owner-web behaviour

`/shifts` (linked from the dashboard header as `Vardiya →`) renders
`Vardiya geçmişi`: one store-scoped row per shift with Kasiyer, Durum, Açılış,
Kapanış, Beklenen kasa, Sayılan kasa, Eksik/Fazla and Net tahsilat. Money is
`tr-TR`-formatted; open shifts show `—` for close columns; the discrepancy is
labelled and colour-coded; the empty state is Turkish. Owner-web is read-only for
shifts — it never opens or closes one.

## Reconciliation

`scripts/reconcile_payments.py` gained a second, read-only check (alongside the
existing order-summary check): for every **closed** shift it re-derives the totals
from the ledger using the *same* window + attribution rule and compares them to the
frozen snapshot, verifying cash/card payments and refunds, gross, total refunds,
net, expected cash and discrepancy. A mismatch means a snapshot was tampered with
after the fact (the trigger makes that unrepresentable through the app, so this is
defence in depth). The script never writes.

```
python scripts/reconcile_payments.py            # all stores
python scripts/reconcile_payments.py --store 1  # one store
python scripts/reconcile_payments.py --json
```

Exit `0` = every order summary and every closed shift snapshot matches.

## Downgrade safety

The migration (`d5c7b3a91e40`) is purely additive. `downgrade()` removes only this
branch's schema (table, constraints, indexes, trigger, function) and **refuses** to
run while any shift row exists — dropping the table would destroy the record of how
a till was reconciled (counted cash, the discrepancy a manager signed off on) while
the payments it summarised remain. Export the shifts first.

## Deferred (explicitly out of scope)

- accounting export
- bank reconciliation
- POS hardware integration
- cash-drawer hardware integration
- payroll
- shift scheduling / rostering
- employee attendance
- tip management
- expense entry
- purchase orders / supplier management
- multi-currency accounting
- enforcing open-shift-before-payment as a hard policy
