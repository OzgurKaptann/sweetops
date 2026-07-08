"""
Secure QR store/table context — token service, resolution, and QR-scoped
menu/order API tests.

Pure (no-DB) tests cover hashing/generation invariants and run anywhere. The
remaining tests require the real PostgreSQL database (like the rest of the
suite) because they assert transactional resolution, revocation, stock and
idempotency behaviour end-to-end.
"""
import uuid
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.order import Order
from app.models.ingredient_stock import IngredientStock
from app.models.table_qr_token import (
    TableQrToken,
    QR_TOKEN_STATUS_ACTIVE,
    QR_TOKEN_STATUS_REVOKED,
)
from app.services import qr_token_service as svc
from tests.conftest import (
    make_ingredient,
    cleanup_ingredient,
    make_store_table,
    make_store_table_token,
    cleanup_store_table,
    qr_order_payload,
)


# ---------------------------------------------------------------------------
# Pure token invariants (no DB)  — scenarios 2, 3
# ---------------------------------------------------------------------------

def test_same_raw_token_hashes_deterministically():
    raw = svc.generate_raw_token()
    assert svc.hash_token(raw) == svc.hash_token(raw)


def test_different_raw_tokens_produce_different_hashes():
    a, b = svc.generate_raw_token(), svc.generate_raw_token()
    assert a != b
    assert svc.hash_token(a) != svc.hash_token(b)


def test_generated_tokens_are_high_entropy_and_urlsafe():
    raw = svc.generate_raw_token()
    # token_urlsafe(32) → ~43 chars, url-safe alphabet only.
    assert len(raw) >= 40
    assert all(c.isalnum() or c in "-_" for c in raw)


def test_hash_is_sha256_hex_64_chars():
    assert len(svc.hash_token("anything")) == 64


def test_malformed_token_resolution_returns_none_without_db(db=None):
    # None / non-str inputs must never raise or leak — they resolve to None.
    # resolve_token short-circuits before any DB access for these inputs.
    assert svc.resolve_token(None, None) is None  # type: ignore[arg-type]
    assert svc.resolve_token(None, 12345) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Token service (DB)  — scenarios 1, 4, 5, 6, 7, 8, 10, 11, 12
# ---------------------------------------------------------------------------

def test_raw_token_is_never_stored(db):
    store, table, record, raw = make_store_table_token(db)
    try:
        # No row anywhere holds the raw token — only its hash.
        assert db.query(TableQrToken).filter(
            TableQrToken.token_hash == raw
        ).first() is None
        stored = db.get(TableQrToken, record.id)
        assert stored.token_hash == svc.hash_token(raw)
        assert stored.token_hash != raw
        assert raw.startswith(stored.token_prefix)
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_token_hash_is_unique(db):
    from sqlalchemy.exc import IntegrityError

    store, table, record, raw = make_store_table_token(db)
    try:
        dup = TableQrToken(
            table_id=table.id,
            token_hash=record.token_hash,  # same hash → must violate uniqueness
            token_prefix=record.token_prefix,
            status=QR_TOKEN_STATUS_ACTIVE,
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_active_token_resolves_to_correct_table_and_store(db):
    store, table, record, raw = make_store_table_token(db)
    try:
        ctx = svc.resolve_token(db, raw)
        assert ctx is not None
        assert ctx.table_id == table.id          # scenario 5
        assert ctx.store_id == store.id          # scenario 6 (via relationship)
        assert ctx.store_name == store.name
        assert ctx.table_name == f"Masa {table.table_number}"
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_revoked_token_does_not_resolve(db):
    store, table, record, raw = make_store_table_token(db)
    try:
        svc.revoke_by_id(db, record.id)
        assert svc.resolve_token(db, raw) is None   # scenario 7
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_unknown_token_does_not_resolve(db):
    assert svc.resolve_token(db, "totally-unknown-token") is None  # scenario 8


def test_last_used_at_updates_after_resolution(db):
    store, table, record, raw = make_store_table_token(db)
    try:
        assert record.last_used_at is None
        svc.resolve_token(db, raw, touch=True)
        db.commit()
        db.refresh(record)
        assert record.last_used_at is not None      # scenario 12
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_rotation_creates_new_active_and_supersedes_prior(db):
    store, table, old_record, old_raw = make_store_table_token(db)
    try:
        new_record, new_raw = svc.rotate_token(db, table.id, created_reason="rotate")
        db.refresh(old_record)

        assert new_raw != old_raw                    # scenario 43
        assert new_record.status == QR_TOKEN_STATUS_ACTIVE   # scenario 10
        assert old_record.status == QR_TOKEN_STATUS_REVOKED  # prior revoked
        assert old_record.revoked_at is not None
        assert old_record.replaced_by_id == new_record.id    # lineage linked

        # Historical record preserved (scenario 11) — old row still present.
        assert db.get(TableQrToken, old_record.id) is not None
        # Old token no longer resolves; new token does.
        assert svc.resolve_token(db, old_raw) is None
        assert svc.resolve_token(db, new_raw) is not None
    finally:
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Store/table integrity  — scenarios 13, 14, 15, 16
# ---------------------------------------------------------------------------

def test_inactive_table_is_rejected(db, monkeypatch):
    store, table, record, raw = make_store_table_token(db)
    try:
        monkeypatch.setattr(svc, "_table_is_active", lambda t: False)
        with pytest.raises(svc.QrTableUnavailable):
            svc.resolve_token(db, raw)               # scenario 14
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_inactive_store_is_rejected(db, monkeypatch):
    store, table, record, raw = make_store_table_token(db)
    try:
        monkeypatch.setattr(svc, "_store_is_active", lambda s: False)
        with pytest.raises(svc.QrTableUnavailable):
            svc.resolve_token(db, raw)               # scenario 15
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_deleting_table_cascades_and_fails_safe(db):
    store, table, record, raw = make_store_table_token(db)
    table_id = table.id
    try:
        # Deleting the table cascades to its tokens; resolution then fails safe.
        db.query(TableQrToken).filter(TableQrToken.table_id == table_id).delete(
            synchronize_session=False
        )
        db.query(type(table)).filter(type(table).id == table_id).delete(
            synchronize_session=False
        )
        db.commit()
        assert svc.resolve_token(db, raw) is None    # scenario 16
    finally:
        cleanup_store_table(db, store.id, table_id)


# ---------------------------------------------------------------------------
# QR resolution API  — scenarios 9, 13(api)
# ---------------------------------------------------------------------------

def test_resolve_endpoint_returns_public_context(client, db):
    store, table, record, raw = make_store_table_token(db)
    try:
        r = client.post("/public/qr-context/resolve", json={"qr_token": raw})
        assert r.status_code == 200
        body = r.json()
        assert body["store"]["id"] == store.id
        assert body["table"]["id"] == table.id
        # No token material leaks into the response.
        assert "token_hash" not in str(body)
        assert raw not in str(body)
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_resolve_endpoint_hides_internal_details_for_invalid_token(client):
    # scenario 9 — malformed/unknown token yields the same generic Turkish msg.
    r = client.post("/public/qr-context/resolve", json={"qr_token": "@@bad@@"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "geçerli değil" in detail
    # No stack trace / token internals leaked.
    assert "token_hash" not in detail.lower()


# ---------------------------------------------------------------------------
# Menu API  — scenarios 17, 18, 19, 20
# The token is transported in the request BODY (POST /public/menu/resolve),
# never in the URL — see Blocker 1. There is no query-string menu-token path.
# ---------------------------------------------------------------------------

def test_valid_qr_loads_menu(client, db):
    store, table, record, raw = make_store_table_token(db)
    try:
        r = client.post("/public/menu/resolve", json={"qr_token": raw})
        assert r.status_code == 200                  # scenario 17
        assert "products" in r.json()
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_invalid_qr_does_not_load_menu(client):
    r = client.post("/public/menu/resolve", json={"qr_token": "not-a-real-token"})
    assert r.status_code == 404                       # scenario 18


def test_menu_token_is_never_in_the_url(client, db):
    # Blocker 1 / scenario 7-8 (frontend): the QR-gated menu route carries the
    # token only in the JSON body — never in its path or query.
    store, table, record, raw = make_store_table_token(db)
    try:
        r = client.post("/public/menu/resolve", json={"qr_token": raw})
        assert r.status_code == 200
        assert raw not in str(r.request.url)
        assert "qr_token" not in str(r.request.url)
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_query_string_token_has_no_gating_effect(client, db):
    # There is no query-string token transport: the ungated GET ignores any
    # ?qr_token entirely (a revoked/invalid value is inert, not a resolution
    # path), so a token can never be meaningfully carried in a menu URL.
    store, table, record, raw = make_store_table_token(db)
    try:
        svc.revoke_by_id(db, record.id)  # even a revoked token is simply ignored
        r = client.get("/public/menu/?qr_token=whatever-invalid")
        assert r.status_code == 200  # ungated content, query token inert
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_missing_qr_does_not_default_to_store_menu(client):
    # scenario 20 — an empty/invalid token never falls back to a default menu.
    r = client.post("/public/menu/resolve", json={"qr_token": ""})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Order API  — scenarios 21-29
# ---------------------------------------------------------------------------

def test_valid_qr_creates_order_with_server_derived_context(client, db):
    store, table, record, raw = make_store_table_token(db)
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
    try:
        payload, headers = qr_order_payload(ing.id, raw, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200                   # scenario 21
        body = r.json()
        assert body["store_id"] == store.id
        assert body["table_id"] == table.id
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


def test_client_supplied_conflicting_ids_are_ignored(client, db):
    store, table, record, raw = make_store_table_token(db)
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
    try:
        payload, headers = qr_order_payload(ing.id, raw, idem_key=uuid.uuid4().hex)
        # Attacker injects conflicting numeric ids alongside a valid token.
        payload["store_id"] = 999999
        payload["table_id"] = 888888
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["store_id"] == store.id           # scenario 22 — token wins
        assert body["table_id"] == table.id
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


def test_revoked_qr_cannot_create_order(client, db):
    store, table, record, raw = make_store_table_token(db)
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
    try:
        svc.revoke_by_id(db, record.id)
        before = db.query(Order).count()
        payload, headers = qr_order_payload(ing.id, raw, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 404                   # scenario 23
        assert db.query(Order).count() == before      # no order created
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


def test_invalid_qr_creates_no_order_and_no_stock_movement(client, db):
    ing, stock = make_ingredient(db, stock_quantity=Decimal("50.00"))
    try:
        before_orders = db.query(Order).count()
        before_stock = db.get(IngredientStock, stock.id).stock_quantity

        payload, headers = qr_order_payload(
            ing.id, "invalid-token", idem_key=uuid.uuid4().hex
        )
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 404                   # scenario 24

        db.expire_all()
        assert db.query(Order).count() == before_orders
        # scenario 25 — no stock movement on an invalid QR.
        assert db.get(IngredientStock, stock.id).stock_quantity == before_stock
    finally:
        cleanup_ingredient(db, ing.id)


def test_valid_qr_preserves_quantity_accounting(client, db):
    store, table, record, raw = make_store_table_token(db)
    # standard_quantity 10 × selected 1 × item 1 = 10 consumed.
    ing, stock = make_ingredient(
        db, stock_quantity=Decimal("50.00"), standard_quantity=Decimal("10.00")
    )
    try:
        before = db.get(IngredientStock, stock.id).stock_quantity
        payload, headers = qr_order_payload(ing.id, raw, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        db.expire_all()
        after = db.get(IngredientStock, stock.id).stock_quantity
        assert before - after == Decimal("10.00")     # scenario 26
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


def test_valid_qr_preserves_stock_atomicity(client, db):
    store, table, record, raw = make_store_table_token(db)
    # Not enough stock: 5 available, needs 10 → 422, nothing deducted.
    ing, stock = make_ingredient(
        db, stock_quantity=Decimal("5.00"), standard_quantity=Decimal("10.00")
    )
    try:
        before_orders = db.query(Order).count()
        payload, headers = qr_order_payload(ing.id, raw, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 422                    # scenario 27
        db.expire_all()
        assert db.get(IngredientStock, stock.id).stock_quantity == Decimal("5.00")
        assert db.query(Order).count() == before_orders
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


def test_valid_qr_preserves_idempotent_retry(client, db):
    store, table, record, raw = make_store_table_token(db)
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
    try:
        idem = uuid.uuid4().hex
        payload, headers = qr_order_payload(ing.id, raw, idem_key=idem)
        r1 = client.post("/public/orders/", json=payload, headers=headers)
        r2 = client.post("/public/orders/", json=payload, headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200
        # scenarios 28 & 29 — same QR + same key returns the same order once.
        assert r1.json()["order_id"] == r2.json()["order_id"]
        assert db.query(Order).filter(Order.idempotency_key == idem).count() == 1
    finally:
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


def test_order_requires_qr_when_legacy_disabled(client, db):
    # Production default: legacy off → a tokenless order is rejected (no
    # trusting client store_id). scenario supporting acceptance #1/#11.
    store, table, record, raw = make_store_table_token(db)
    ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
    original = settings.ALLOW_LEGACY_ORDER_CONTEXT
    settings.ALLOW_LEGACY_ORDER_CONTEXT = False
    try:
        payload = {
            "store_id": 1,
            "table_id": 1,
            "items": [
                {
                    "product_id": 1,
                    "quantity": 1,
                    "ingredients": [{"ingredient_id": ing.id, "quantity": 1}],
                }
            ],
        }
        r = client.post(
            "/public/orders/", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        assert r.status_code == 400
    finally:
        settings.ALLOW_LEGACY_ORDER_CONTEXT = original
        cleanup_ingredient(db, ing.id)
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Database invariants — status CHECK + one-active-token-per-table
#   Blocker 3 (status integrity) and Blocker 4 (single active token).
# ---------------------------------------------------------------------------

def test_invalid_status_is_rejected_by_db(db):
    # Test 15 — the CHECK constraint rejects any status outside the closed set,
    # regardless of application-level validation.
    from sqlalchemy.exc import IntegrityError

    store, table = make_store_table(db)
    try:
        bogus = TableQrToken(
            table_id=table.id,
            token_hash="f" * 64,
            token_prefix="deadbeef",
            status="BOGUS",  # not ACTIVE / REVOKED
        )
        db.add(bogus)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_table_cannot_have_two_active_tokens(db):
    # Test 16 — the partial unique index forbids a second ACTIVE row per table.
    from sqlalchemy.exc import IntegrityError

    store, table, record, raw = make_store_table_token(db)  # 1 ACTIVE already
    try:
        second = TableQrToken(
            table_id=table.id,
            token_hash="a" * 64,
            token_prefix="second00",
            status=QR_TOKEN_STATUS_ACTIVE,
        )
        db.add(second)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_repeated_issue_fails_when_active_exists(db):
    # Test 17 — a second `issue` on a table that already has an ACTIVE token is
    # rejected (operators must rotate instead).
    store, table = make_store_table(db)
    try:
        first, _ = svc.issue_token(db, table.id, created_reason="issue")
        with pytest.raises(svc.ActiveTokenExists) as exc:
            svc.issue_token(db, table.id, created_reason="issue")
        assert exc.value.existing_token_id == first.id
        db.rollback()
        # Still exactly one active row.
        active = _active_count(db, table.id)
        assert active == 1
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_rotation_leaves_exactly_one_active(db):
    # Test 18 — rotation revokes the old and issues one new ACTIVE token.
    store, table, record, raw = make_store_table_token(db)
    try:
        svc.rotate_token(db, table.id, created_reason="rotate")
        db.commit()
        assert _active_count(db, table.id) == 1
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_multiple_revoked_history_rows_are_allowed(db):
    # The partial unique index constrains only ACTIVE rows: a table may keep any
    # number of REVOKED history rows.
    store, table, record, raw = make_store_table_token(db)
    try:
        svc.rotate_token(db, table.id, created_reason="rotate-1")
        svc.rotate_token(db, table.id, created_reason="rotate-2")
        db.commit()
        db.expire_all()
        revoked = (
            db.query(TableQrToken)
            .filter(
                TableQrToken.table_id == table.id,
                TableQrToken.status == QR_TOKEN_STATUS_REVOKED,
            )
            .count()
        )
        assert revoked == 2
        assert _active_count(db, table.id) == 1
    finally:
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Token-ID-targeted destructive operations — Blocker 2
# ---------------------------------------------------------------------------

def test_prefix_ambiguity_cannot_revoke(db):
    # Test 12 — two records deliberately share a display prefix. Revocation is
    # by exact id, so it can never act on the wrong record, and there is no
    # prefix-based revoke path at all.
    store, table = make_store_table(db)
    store2, table2 = make_store_table(db)
    try:
        shared_prefix = "SamePfx0"
        a = TableQrToken(
            table_id=table.id,
            token_hash="1" * 64,
            token_prefix=shared_prefix,
            status=QR_TOKEN_STATUS_ACTIVE,
        )
        b = TableQrToken(
            table_id=table2.id,
            token_hash="2" * 64,
            token_prefix=shared_prefix,  # same prefix, different table
            status=QR_TOKEN_STATUS_ACTIVE,
        )
        db.add_all([a, b])
        db.commit()

        # There is no way to revoke by prefix — the service exposes no such API.
        assert not hasattr(svc, "revoke_by_prefix")

        # Revoke targets exactly the one id given; the other stays ACTIVE.
        svc.revoke_by_id(db, a.id)
        db.commit()
        db.expire_all()
        assert db.get(TableQrToken, a.id).status == QR_TOKEN_STATUS_REVOKED
        assert db.get(TableQrToken, b.id).status == QR_TOKEN_STATUS_ACTIVE
    finally:
        db.query(TableQrToken).filter(
            TableQrToken.table_id.in_([table.id, table2.id])
        ).delete(synchronize_session=False)
        db.commit()
        cleanup_store_table(db, store.id, table.id)
        cleanup_store_table(db, store2.id, table2.id)


def test_revoke_by_id_targets_exactly_one_record(db):
    # Test 13 — revoke by id affects exactly one row.
    store, table, record, raw = make_store_table_token(db)
    try:
        revoked = svc.revoke_by_id(db, record.id)
        db.commit()
        assert revoked.id == record.id
        assert revoked.status == QR_TOKEN_STATUS_REVOKED
        # Revoking an already-revoked / unknown id makes no change and errors.
        with pytest.raises(ValueError):
            svc.revoke_by_id(db, record.id)
        db.rollback()
        with pytest.raises(ValueError):
            svc.revoke_by_id(db, 2_000_000_000)
        db.rollback()
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_rotate_by_id_targets_exactly_one_record(db):
    # Test 14 — rotate by id revokes exactly that token and issues one new one.
    store, table, record, raw = make_store_table_token(db)
    try:
        new_record, new_raw = svc.rotate_by_id(db, record.id)
        db.commit()
        db.refresh(record)
        assert record.status == QR_TOKEN_STATUS_REVOKED
        assert record.replaced_by_id == new_record.id
        assert new_record.status == QR_TOKEN_STATUS_ACTIVE
        assert _active_count(db, table.id) == 1
        # Rotating a non-ACTIVE id is rejected.
        with pytest.raises(ValueError):
            svc.rotate_by_id(db, record.id)
        db.rollback()
    finally:
        cleanup_store_table(db, store.id, table.id)


# ---------------------------------------------------------------------------
# Concurrency — the DB invariant holds under simultaneous issue/rotate
#   Tests 19, 20.
# ---------------------------------------------------------------------------

def _active_count(db, table_id: int) -> int:
    db.expire_all()
    return (
        db.query(TableQrToken)
        .filter(
            TableQrToken.table_id == table_id,
            TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
        )
        .count()
    )


def _run_concurrently(fn, n: int = 2) -> list:
    """Run `fn(index)` in n threads, each with its own DB session. Returns a
    list of (result_or_None, exception_or_None) tuples."""
    import threading

    from app.core.db import SessionLocal

    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait()  # maximize the overlap window
            try:
                results[i] = (fn(session, i), None)
            except Exception as exc:  # noqa: BLE001 — recorded for assertions
                session.rollback()
                results[i] = (None, exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_concurrent_issue_leaves_at_most_one_active(db):
    # Test 19 — two simultaneous `issue` calls for the same fresh table must not
    # create two ACTIVE tokens.
    store, table = make_store_table(db)
    try:
        def do_issue(session, _i):
            rec, _raw = svc.issue_token(session, table.id, created_reason="race")
            return rec.id

        results = _run_concurrently(do_issue, n=2)
        successes = [r for (r, e) in results if e is None]
        active_conflicts = [
            e for (_r, e) in results if isinstance(e, svc.ActiveTokenExists)
        ]
        assert len(successes) == 1
        assert len(active_conflicts) == 1
        assert _active_count(db, table.id) == 1
    finally:
        cleanup_store_table(db, store.id, table.id)


def test_concurrent_rotate_leaves_at_most_one_active(db):
    # Test 20 — two simultaneous rotations serialize and end with exactly one
    # ACTIVE token (never two).
    store, table, record, raw = make_store_table_token(db)
    try:
        def do_rotate(session, _i):
            rec, _raw = svc.rotate_token(session, table.id, created_reason="race")
            return rec.id

        _run_concurrently(do_rotate, n=2)
        assert _active_count(db, table.id) == 1
    finally:
        cleanup_store_table(db, store.id, table.id)
