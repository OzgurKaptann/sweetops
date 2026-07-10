"""
Real-PostgreSQL concurrency tests for payment collection and refunds.

Threads fire simultaneously against the ASGI app; the SELECT ... FOR UPDATE row
locks in payment_service serialise access so two cashiers can never both
collect the same outstanding balance, and concurrent refunds can never exceed
the refundable balance.
"""
import threading
import uuid
from decimal import Decimal

from tests.conftest import make_authed_client


def _key() -> str:
    return uuid.uuid4().hex


def _fire_collect(client, order_id, results, lock, amount=None):
    body = {"payment_method": "CASH"}
    if amount is not None:
        body["amount"] = amount
    r = client.post(f"/cashier/orders/{order_id}/payments", json=body,
                    headers={"Idempotency-Key": _key()})
    with lock:
        results.append(r.status_code)


def test_two_full_payments_collect_once(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    order = make_order(store.id, table.id, Decimal("100.00"))
    # Two distinct cashier clients, two DIFFERENT idempotency keys.
    c1 = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    c2 = make_authed_client(db, make_staff("CASHIER", store_id=store.id))

    results: list[int] = []
    lock = threading.Lock()
    threads = [
        threading.Thread(target=_fire_collect, args=(c1, order.id, results, lock)),
        threading.Thread(target=_fire_collect, args=(c2, order.id, results, lock)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, results
    assert results.count(409) == 1, results

    db.expire_all()
    db.refresh(order)
    assert Decimal(str(order.paid_amount)) == Decimal("100.00")
    assert order.payment_status == "PAID"


def test_concurrent_partials_cannot_exceed_outstanding(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    order = make_order(store.id, table.id, Decimal("100.00"))
    c1 = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    c2 = make_authed_client(db, make_staff("CASHIER", store_id=store.id))

    results: list[int] = []
    lock = threading.Lock()
    # Each tries to collect 70 — together 140 > 100. At most one can be full 70,
    # the other must be rejected (no room for a second 70).
    threads = [
        threading.Thread(target=_fire_collect, args=(c1, order.id, results, lock, "70.00")),
        threading.Thread(target=_fire_collect, args=(c2, order.id, results, lock, "70.00")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, results
    assert results.count(409) == 1, results
    db.expire_all()
    db.refresh(order)
    assert Decimal(str(order.paid_amount)) <= Decimal("100.00")


def test_concurrent_full_table_settlement_no_double_collect(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    o1 = make_order(store.id, table.id, Decimal("30.00"))
    o2 = make_order(store.id, table.id, Decimal("40.00"))
    c1 = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
    c2 = make_authed_client(db, make_staff("CASHIER", store_id=store.id))

    results: list[int] = []
    lock = threading.Lock()

    def fire(client):
        r = client.post("/cashier/settlements",
                        json={"table_id": table.id, "order_ids": [o1.id, o2.id],
                              "payment_method": "CARD"},
                        headers={"Idempotency-Key": _key()})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=fire, args=(c1,)),
               threading.Thread(target=fire, args=(c2,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, results
    assert results.count(409) == 1, results
    db.expire_all()
    db.refresh(o1); db.refresh(o2)
    assert Decimal(str(o1.paid_amount)) == Decimal("30.00")
    assert Decimal(str(o2.paid_amount)) == Decimal("40.00")


def test_concurrent_refunds_cannot_exceed_refundable(db, make_store, make_table, make_staff, make_order):
    store = make_store(); table = make_table(store.id)
    mgr = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    order = make_order(store.id, table.id, Decimal("100.00"))
    res = mgr.post(f"/cashier/orders/{order.id}/payments",
                   json={"payment_method": "CASH"},
                   headers={"Idempotency-Key": _key()})
    alloc = res.json()["allocations"][0]["id"]

    m1 = make_authed_client(db, make_staff("MANAGER", store_id=store.id))
    m2 = make_authed_client(db, make_staff("MANAGER", store_id=store.id))

    results: list[int] = []
    lock = threading.Lock()

    def fire(client):
        r = client.post(f"/cashier/allocations/{alloc}/refunds",
                        json={"amount": "70.00", "reason": "iade"},
                        headers={"Idempotency-Key": _key()})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=fire, args=(m1,)),
               threading.Thread(target=fire, args=(m2,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 70 + 70 > 100 collected → only one refund of 70 fits.
    assert results.count(200) == 1, results
    assert results.count(409) == 1, results
    db.expire_all()
    db.refresh(order)
    assert Decimal(str(order.refunded_amount)) <= Decimal("100.00")
