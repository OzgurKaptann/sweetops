"""
Owner operational dashboard — the read-only aggregate an owner opens each day.

These tests defend the properties that make the dashboard trustworthy:
  * it is protected (OWNER/MANAGER only) and store-scoped from the session;
  * money comes from the collected LEDGER, never from order totals, and refunds
    reduce net;
  * issue / shift / kitchen / inventory figures come from their source-of-truth
    systems, not a re-implementation;
  * it mutates nothing;
  * an empty store/day is all safe zeros/nulls;
  * the attention list severity ordering is deterministic;
  * the response is Cache-Control: no-store.
"""
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client

DASH = "/owner/operational-dashboard"


def _key() -> str:
    return uuid.uuid4().hex


def _collect_all(client, order_id: str, method: str = "CASH") -> dict:
    res = client.post(
        f"/cashier/orders/{order_id}/payments",
        json={"payment_method": method},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    return res.json()


# ── Access control ────────────────────────────────────────────────────────────

def test_owner_can_read_own_store(db, make_store, make_staff):
    store = make_store()
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    res = owner.get(DASH)
    assert res.status_code == 200, res.text
    assert res.json()["store_id"] == store.id


def test_manager_can_read_own_store(db, make_store, make_staff):
    store = make_store()
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    res = mgr.get(DASH)
    assert res.status_code == 200, res.text
    assert res.json()["store_id"] == store.id


def test_cashier_rejected(db, make_store, make_staff):
    store = make_store()
    cashier = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    assert cashier.get(DASH).status_code == 403


def test_kitchen_rejected(db, make_store, make_staff):
    store = make_store()
    kitchen = make_authed_client(db, make_staff("KITCHEN", store_id=store.id))
    assert kitchen.get(DASH).status_code == 403


def test_unauthenticated_rejected(client):
    assert client.get(DASH).status_code == 401


def test_cache_control_no_store(db, make_store, make_staff):
    store = make_store()
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    res = owner.get(DASH)
    assert res.headers.get("Cache-Control") == "no-store"


# ── Empty store/day ───────────────────────────────────────────────────────────

def test_empty_store_returns_safe_zeros(db, make_store, make_staff):
    store = make_store()
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    body = owner.get(DASH).json()

    assert body["orders"]["active_count"] == 0
    assert body["orders"]["completed_today"] == 0
    assert body["payments"]["gross_collected_today"] == "0.00"
    assert body["payments"]["refunds_today"] == "0.00"
    assert body["payments"]["net_collected_today"] == "0.00"
    assert body["payments"]["unpaid_or_partially_paid_orders"] == 0
    assert body["kitchen"]["active_orders"] == 0
    assert body["kitchen"]["average_prep_seconds_today"] is None
    assert body["issues"]["open_count"] == 0
    assert body["shifts"]["open_shift_count"] == 0
    assert body["shifts"]["total_discrepancy_today"] == "0.00"
    assert body["inventory"]["critical_count"] == 0
    assert body["attention"] == []


# ── Money: collected ledger, not order total ──────────────────────────────────

def test_payments_use_collected_ledger_not_order_total(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))

    paid = make_order(store.id, table.id, Decimal("100.00"))
    make_order(store.id, table.id, Decimal("250.00"))  # unpaid — must NOT count as money
    _collect_all(owner, paid.id)

    body = owner.get(DASH).json()
    # Collected = the one paid order's ledger amount, NOT 100+250 order totals.
    assert body["payments"]["gross_collected_today"] == "100.00"
    assert body["payments"]["net_collected_today"] == "100.00"
    # The unpaid order is surfaced as owed money, never as revenue.
    assert body["payments"]["unpaid_or_partially_paid_orders"] == 1


def test_refunds_reduce_net_collected(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))

    order = make_order(store.id, table.id, Decimal("100.00"))
    res = _collect_all(owner, order.id)
    alloc = res["allocations"][0]["id"]
    r = owner.post(
        f"/cashier/allocations/{alloc}/refunds",
        json={"amount": "30.00", "reason": "iade"},
        headers={"Idempotency-Key": _key()},
    )
    assert r.status_code == 200, r.text

    body = owner.get(DASH).json()
    assert body["payments"]["gross_collected_today"] == "100.00"
    assert body["payments"]["refunds_today"] == "30.00"
    assert body["payments"]["net_collected_today"] == "70.00"


# ── Store scoping ─────────────────────────────────────────────────────────────

def test_store_scoping_prevents_cross_store_data(db, make_store, make_table, make_staff, make_order):
    store_a = make_store()
    store_b = make_store()
    table_b = make_table(store_b.id)
    owner_a = make_authed_client(db, make_staff("OWNER", store_id=store_a.id))
    owner_b = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))

    order_b = make_order(store_b.id, table_b.id, Decimal("80.00"))
    _collect_all(owner_b, order_b.id)

    # Store A owner sees none of store B's money; store B owner sees it.
    assert owner_a.get(DASH).json()["payments"]["gross_collected_today"] == "0.00"
    assert owner_b.get(DASH).json()["payments"]["gross_collected_today"] == "80.00"


# ── Order issues ──────────────────────────────────────────────────────────────

def _open_issue(client, order_id: int) -> int:
    res = client.post(
        f"/orders/{order_id}/issues",
        json={"issue_type": "WRONG_ITEM", "reason": "yanlis urun"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_open_issue_count(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    o1 = make_order(store.id, table.id, Decimal("40.00"))
    o2 = make_order(store.id, table.id, Decimal("40.00"))
    _open_issue(owner, o1.id)
    _open_issue(owner, o2.id)

    body = owner.get(DASH).json()
    assert body["issues"]["open_count"] == 2
    assert body["issues"]["resolved_today"] == 0
    codes = [a["code"] for a in body["attention"]]
    assert "OPEN_ISSUES" in codes


def test_resolved_today_issue_count(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("40.00"))
    issue_id = _open_issue(owner, order.id)
    r = owner.post(
        f"/order-issues/{issue_id}/resolve",
        json={"resolution_type": "NO_REFUND", "reason": "musteriyle konusuldu"},
        headers={"Idempotency-Key": _key()},
    )
    assert r.status_code == 200, r.text

    body = owner.get(DASH).json()
    assert body["issues"]["open_count"] == 0
    assert body["issues"]["resolved_today"] == 1
    assert body["issues"]["refund_amount_today"] == "0.00"


# ── Shifts ────────────────────────────────────────────────────────────────────

def test_open_shift_count(db, make_store, make_table, make_staff):
    store = make_store()
    cashier = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    opened = cashier.post(
        "/cashier/shifts/open",
        json={"opening_cash_amount": "100.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert opened.status_code == 200, opened.text

    body = owner.get(DASH).json()
    assert body["shifts"]["open_shift_count"] == 1
    assert "OPEN_SHIFTS" in [a["code"] for a in body["attention"]]


def test_closed_shift_discrepancy_uses_frozen_snapshot(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    cashier_user = make_staff("CASHIER", store_id=store.id)
    cashier = make_authed_client(db, cashier_user)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))

    opened = cashier.post(
        "/cashier/shifts/open",
        json={"opening_cash_amount": "100.00"},
        headers={"Idempotency-Key": _key()},
    )
    shift_id = opened.json()["id"]
    # Collect 50 cash so expected closing = 150; count 140 → -10 discrepancy.
    order = make_order(store.id, table.id, Decimal("50.00"))
    _collect_all(cashier, order.id, method="CASH")
    closed = cashier.post(
        f"/cashier/shifts/{shift_id}/close",
        json={"counted_closing_cash_amount": "140.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert closed.status_code == 200, closed.text
    frozen = closed.json()["cash_discrepancy_amount"]

    body = owner.get(DASH).json()
    assert body["shifts"]["closed_today"] == 1
    assert body["shifts"]["open_shift_count"] == 0
    # Total discrepancy is exactly the frozen snapshot value, not a recomputation.
    assert body["shifts"]["total_discrepancy_today"] == frozen
    assert body["shifts"]["shifts_with_discrepancy_today"] == 1
    assert "SHIFT_DISCREPANCY" in [a["code"] for a in body["attention"]]


# ── Kitchen tempo ─────────────────────────────────────────────────────────────

def test_kitchen_block_matches_timing_summary(db, make_store, make_staff):
    """The dashboard kitchen block is the SAME source as /kitchen/timing/summary."""
    store = make_store()
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))

    dash = owner.get(DASH).json()["kitchen"]
    summary = owner.get("/kitchen/timing/summary").json()
    assert dash["active_orders"] == summary["active_orders"]
    assert dash["delayed_orders"] == summary["delayed_orders"]
    assert dash["average_prep_seconds_today"] == summary["average_prep_seconds_today"]


# ── Inventory thresholds ──────────────────────────────────────────────────────

def test_inventory_counts_come_from_threshold_logic(db, make_store, make_staff, make_table):
    from tests.conftest import make_ingredient, stock_for, cleanup_ingredient

    store = make_store()
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))

    # An ingredient with 0 available in this store → OUT_OF_STOCK by threshold_status.
    ing, _ = make_ingredient(db, on_hand=Decimal("5"), store_id=1)
    stock_for(db, ing, store.id, on_hand=Decimal("0"))

    # A configured LOW ingredient: available (10) <= minimum (20).
    ing2, _ = make_ingredient(db, on_hand=Decimal("5"), store_id=1)
    stock_for(db, ing2, store.id, on_hand=Decimal("10"))
    patched = mgr.patch(
        f"/inventory/stock/{ing2.id}/thresholds",
        json={"minimum_quantity": "20", "reason": "esik"},
        headers={"Idempotency-Key": _key()},
    )
    assert patched.status_code == 200, patched.text

    try:
        body = owner.get(DASH).json()
        assert body["inventory"]["out_of_stock_count"] >= 1
        assert body["inventory"]["low_count"] >= 1
        codes = [a["code"] for a in body["attention"]]
        assert "OUT_OF_STOCK" in codes
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_ingredient(db, ing2.id)


# ── Attention list determinism ────────────────────────────────────────────────

def test_attention_severity_is_deterministic(db, make_store, make_table, make_staff, make_order):
    from tests.conftest import make_ingredient, stock_for, cleanup_ingredient

    store = make_store()
    table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    cashier = make_authed_client(db, make_staff("CASHIER", store_id=store.id))

    # Trigger an open issue (warning), an open shift (info) and out-of-stock (critical).
    order = make_order(store.id, table.id, Decimal("40.00"))
    _open_issue(owner, order.id)
    cashier.post("/cashier/shifts/open", json={"opening_cash_amount": "0.00"},
                 headers={"Idempotency-Key": _key()})
    ing, _ = make_ingredient(db, on_hand=Decimal("5"), store_id=1)
    stock_for(db, ing, store.id, on_hand=Decimal("0"))

    try:
        attention = owner.get(DASH).json()["attention"]
        ranks = {"critical": 3, "warning": 2, "info": 1}
        seq = [ranks[a["severity"]] for a in attention]
        # Deterministic: non-increasing severity, most urgent first.
        assert seq == sorted(seq, reverse=True)
        assert attention[0]["code"] == "OUT_OF_STOCK"  # critical always leads
    finally:
        cleanup_ingredient(db, ing.id)


# ── No mutation ───────────────────────────────────────────────────────────────

def test_dashboard_does_not_mutate(db, make_store, make_table, make_staff, make_order):
    from sqlalchemy import text

    store = make_store()
    table = make_table(store.id)
    owner = make_authed_client(db, make_staff("OWNER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"))
    _collect_all(owner, order.id)

    def _counts():
        return {
            "settlements": db.execute(
                text("SELECT COUNT(*) FROM payment_settlements WHERE store_id=:s"),
                {"s": store.id}).scalar(),
            "refunds": db.execute(
                text("SELECT COUNT(*) FROM payment_refunds WHERE store_id=:s"),
                {"s": store.id}).scalar(),
            "orders": db.execute(
                text("SELECT COUNT(*) FROM orders WHERE store_id=:s"),
                {"s": store.id}).scalar(),
            "shifts": db.execute(
                text("SELECT COUNT(*) FROM cashier_shifts WHERE store_id=:s"),
                {"s": store.id}).scalar(),
        }

    before = _counts()
    for _ in range(3):
        assert owner.get(DASH).status_code == 200
    db.expire_all()
    after = _counts()
    assert before == after
