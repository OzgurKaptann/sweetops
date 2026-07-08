"""
Tests for the Owner Decision Engine — GET /owner/decisions/ + PATCH /owner/decisions/{id}.

Coverage:
  - Signal detection (stock_risk, demand_spike, slow_moving, sla_risk, revenue_anomaly)
  - Prioritization: decision_score formula, ordering by score DESC + id ASC
  - blocking_vs_non_blocking classification
  - why_now / expected_impact are non-empty and specific
  - Action lifecycle: acknowledge, complete, dismiss — valid and invalid transitions
  - Cooldown / deduplication: completed/dismissed suppression + reset after expiry
  - HTTP endpoint shape and status codes
"""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.ingredient_stock import IngredientStockMovement
from app.models.order import Order
from app.models.owner_decision import OwnerDecision
from app.services.decision_engine import (
    COOLDOWN_HOURS,
    _decision_score,
    _is_blocking,
    _urgency_bonus,
    _why_now,
    _expected_impact,
    _demand_spike_signals,
    _revenue_anomaly_signals,
    _sla_risk_signals,
    _slow_moving_signals,
    _stock_risk_signals,
    apply_decision_action,
    get_owner_decisions,
)
from tests.conftest import cleanup_ingredient, make_ingredient, order_payload

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _backdate(db, order_id: int, minutes_ago: float) -> None:
    target = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.query(Order).filter(Order.id == order_id).update(
        {"created_at": target}, synchronize_session=False
    )
    db.commit()


def _add_movement(
    db,
    ingredient_id: int,
    quantity_delta: float,
    movement_type: str = "ORDER_DEDUCTION",
    hours_ago: float = 1.0,
) -> IngredientStockMovement:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    mv = IngredientStockMovement(
        ingredient_id=ingredient_id,
        movement_type=movement_type,
        quantity_delta=Decimal(str(quantity_delta)),
        unit="g",
        created_at=ts,
    )
    db.add(mv)
    db.commit()
    db.refresh(mv)
    return mv


def _cleanup_movements(db, ingredient_id: int) -> None:
    db.query(IngredientStockMovement).filter(
        IngredientStockMovement.ingredient_id == ingredient_id
    ).delete(synchronize_session=False)
    db.commit()


def _cleanup_decision(db, decision_id: str) -> None:
    db.query(OwnerDecision).filter(
        OwnerDecision.decision_id == decision_id
    ).delete(synchronize_session=False)
    db.commit()


def _cleanup_orders(db, order_ids: list[int]) -> None:
    from app.models.order_item import OrderItem
    from app.models.order_item_ingredient import OrderItemIngredient
    from app.models.order_status_event import OrderStatusEvent

    db.query(OrderStatusEvent).filter(
        OrderStatusEvent.order_id.in_(order_ids)
    ).delete(synchronize_session=False)
    oi_ids = [
        r.id
        for r in db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).all()
    ]
    if oi_ids:
        db.query(OrderItemIngredient).filter(
            OrderItemIngredient.order_item_id.in_(oi_ids)
        ).delete(synchronize_session=False)
        db.query(OrderItem).filter(OrderItem.id.in_(oi_ids)).delete(
            synchronize_session=False
        )
    db.query(Order).filter(Order.id.in_(order_ids)).delete(synchronize_session=False)
    db.commit()


def _seed_decision(db, decision_id: str, status: str = "pending", **kwargs) -> OwnerDecision:
    """Insert a minimal OwnerDecision row directly for lifecycle/cooldown tests."""
    row = OwnerDecision(
        decision_id=decision_id,
        type=kwargs.get("type", "stock_risk"),
        severity=kwargs.get("severity", "high"),
        decision_score=kwargs.get("decision_score", 150.0),
        blocking_vs_non_blocking=kwargs.get("blocking_vs_non_blocking", True),
        title=kwargs.get("title", "Test decision"),
        description=kwargs.get("description", "Test description"),
        impact=kwargs.get("impact", "Test impact"),
        recommended_action=kwargs.get("recommended_action", "Test action"),
        why_now=kwargs.get("why_now", "Because test"),
        expected_impact=kwargs.get("expected_impact", "Better outcome"),
        data=kwargs.get("data", {}),
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# 1. Scoring helpers
# ---------------------------------------------------------------------------


class TestScoringFormula:
    def test_base_scores(self):
        # No urgency, not blocking
        assert _decision_score("high", "slow_moving", {}, False) == 100.0
        assert _decision_score("medium", "slow_moving", {}, False) == 50.0
        assert _decision_score("low", "slow_moving", {}, False) == 20.0

    def test_blocking_bonus_adds_25(self):
        base = _decision_score("high", "slow_moving", {}, False)
        with_block = _decision_score("high", "slow_moving", {}, True)
        assert with_block - base == 25.0

    def test_stock_risk_zero_stock_gets_max_urgency(self):
        bonus = _urgency_bonus("stock_risk", {"hours_to_stockout": 0})
        assert bonus == 30.0

    def test_stock_risk_high_urgency_hours(self):
        # 3h to stockout → (6-3)*5 = 15
        bonus = _urgency_bonus("stock_risk", {"hours_to_stockout": 3.0})
        assert bonus == 15.0

    def test_stock_risk_medium_urgency_hours(self):
        # 9h to stockout → (12-9)*2 = 6
        bonus = _urgency_bonus("stock_risk", {"hours_to_stockout": 9.0})
        assert bonus == 6.0

    def test_stock_risk_no_urgency_beyond_threshold(self):
        bonus = _urgency_bonus("stock_risk", {"hours_to_stockout": 15.0})
        assert bonus == 0.0

    def test_stock_risk_no_stockout_data(self):
        bonus = _urgency_bonus("stock_risk", {"hours_to_stockout": None})
        assert bonus == 0.0

    def test_demand_spike_urgency_capped_at_25(self):
        # ratio=10 → (10-1.5)*10 = 85 but capped at 25
        bonus = _urgency_bonus("demand_spike", {"spike_ratio": 10.0})
        assert bonus == 25.0

    def test_demand_spike_urgency_at_1_5_is_zero(self):
        # ratio=1.5 → (1.5-1.5)*10 = 0
        bonus = _urgency_bonus("demand_spike", {"spike_ratio": 1.5})
        assert bonus == 0.0

    def test_sla_risk_urgency(self):
        # 3 critical, 2 warning → 3*5 + 2*2 = 19
        bonus = _urgency_bonus("sla_risk", {"critical_count": 3, "warning_count": 2})
        assert bonus == 19.0

    def test_sla_risk_urgency_capped_at_30(self):
        bonus = _urgency_bonus("sla_risk", {"critical_count": 10, "warning_count": 10})
        assert bonus == 30.0

    def test_revenue_anomaly_high_drop_urgency(self):
        bonus = _urgency_bonus("revenue_anomaly", {"direction": "drop", "ratio": 0.2})
        assert bonus == 15.0

    def test_revenue_anomaly_medium_drop_urgency(self):
        bonus = _urgency_bonus("revenue_anomaly", {"direction": "drop", "ratio": 0.5})
        assert bonus == 5.0

    def test_revenue_anomaly_spike_no_urgency(self):
        bonus = _urgency_bonus("revenue_anomaly", {"direction": "spike", "ratio": 3.0})
        assert bonus == 0.0

    def test_slow_moving_always_zero_urgency(self):
        bonus = _urgency_bonus("slow_moving", {"anything": True})
        assert bonus == 0.0

    def test_full_score_stock_risk_critical(self):
        # severity=high(100) + zero-stock urgency(30) + blocking(25) = 155
        score = _decision_score(
            "high", "stock_risk",
            {"hours_to_stockout": 0}, True
        )
        assert score == 155.0

    def test_full_score_sla_risk_high(self):
        # severity=high(100) + sla urgency(min(2*5+0,30)=10) + blocking(25) = 135
        score = _decision_score(
            "high", "sla_risk",
            {"critical_count": 2, "warning_count": 0}, True
        )
        assert score == 135.0


# ---------------------------------------------------------------------------
# 2. blocking_vs_non_blocking classification
# ---------------------------------------------------------------------------


class TestBlockingClassification:
    def test_stock_risk_high_is_blocking(self):
        assert _is_blocking("stock_risk", "high", {}) is True

    def test_stock_risk_medium_is_not_blocking(self):
        assert _is_blocking("stock_risk", "medium", {}) is False

    def test_demand_spike_high_is_blocking(self):
        assert _is_blocking("demand_spike", "high", {}) is True

    def test_demand_spike_medium_is_blocking(self):
        assert _is_blocking("demand_spike", "medium", {}) is True

    def test_demand_spike_low_is_not_blocking(self):
        assert _is_blocking("demand_spike", "low", {}) is False

    def test_sla_risk_high_is_blocking(self):
        assert _is_blocking("sla_risk", "high", {}) is True

    def test_sla_risk_medium_is_not_blocking(self):
        assert _is_blocking("sla_risk", "medium", {}) is False

    def test_revenue_anomaly_high_drop_is_blocking(self):
        assert _is_blocking("revenue_anomaly", "high", {"direction": "drop"}) is True

    def test_revenue_anomaly_high_spike_is_not_blocking(self):
        assert _is_blocking("revenue_anomaly", "high", {"direction": "spike"}) is False

    def test_revenue_anomaly_medium_is_not_blocking(self):
        assert _is_blocking("revenue_anomaly", "medium", {"direction": "drop"}) is False

    def test_slow_moving_is_never_blocking(self):
        assert _is_blocking("slow_moving", "medium", {}) is False
        assert _is_blocking("slow_moving", "high", {}) is False


# ---------------------------------------------------------------------------
# 3. why_now and expected_impact
# ---------------------------------------------------------------------------


class TestWhyNowExpectedImpact:
    def test_why_now_stock_risk_zero_stock(self):
        text = _why_now("stock_risk", "high", {
            "ingredient_name": "Strawberry", "hours_to_stockout": 0,
            "velocity_per_hour": 0.5, "unit": "g",
        })
        assert "zero stock" in text
        assert "Strawberry" in text

    def test_why_now_stock_risk_with_velocity(self):
        text = _why_now("stock_risk", "high", {
            "ingredient_name": "Cream",
            "hours_to_stockout": 3.5,
            "velocity_per_hour": 2.0,
            "unit": "ml",
        })
        assert "3.5h" in text
        assert "2.00 ml/h" in text

    def test_why_now_demand_spike(self):
        text = _why_now("demand_spike", "high", {"spike_ratio": 4.2})
        assert "4.2" in text
        assert "60 minutes" in text

    def test_why_now_sla_risk_critical(self):
        text = _why_now("sla_risk", "high", {
            "critical_count": 2, "worst_age_minutes": 13.5,
            "warning_count": 0,
        })
        assert "2" in text
        assert "13.5" in text

    def test_why_now_revenue_drop(self):
        text = _why_now("revenue_anomaly", "high", {
            "direction": "drop", "ratio": 0.2,
            "last_1h_revenue": 40.0, "avg_hourly_baseline": 200.0,
        })
        assert "drop" in text or "below" in text
        assert "₺40" in text

    def test_expected_impact_stock_risk_with_revenue(self):
        text = _expected_impact("stock_risk", "high", {"revenue_at_risk": 500.0}, True)
        assert "₺500" in text

    def test_expected_impact_demand_spike(self):
        text = _expected_impact("demand_spike", "high", {"spike_ratio": 3.5}, True)
        assert "3.5×" in text

    def test_expected_impact_slow_moving(self):
        text = _expected_impact("slow_moving", "medium", {"tied_capital": 120.0}, False)
        assert "₺120" in text

    def test_expected_impact_sla_risk(self):
        text = _expected_impact("sla_risk", "high", {
            "critical_count": 2, "warning_count": 1
        }, True)
        assert "3" in text  # 2+1

    def test_why_now_all_types_return_non_empty(self):
        for signal_type, data in [
            ("stock_risk",      {"ingredient_name": "X", "hours_to_stockout": 0, "velocity_per_hour": 1.0, "unit": "g"}),
            ("demand_spike",    {"spike_ratio": 2.0}),
            ("slow_moving",     {"ingredient_name": "Y", "current_stock": 50.0}),
            ("sla_risk",        {"critical_count": 1, "worst_age_minutes": 12.0, "warning_count": 0}),
            ("revenue_anomaly", {"direction": "drop", "ratio": 0.3, "last_1h_revenue": 10.0, "avg_hourly_baseline": 100.0}),
        ]:
            text = _why_now(signal_type, "high", data)
            assert len(text) > 10, f"why_now too short for {signal_type}"


# ---------------------------------------------------------------------------
# 4. Deterministic ordering
# ---------------------------------------------------------------------------


class TestDecisionOrdering:
    def test_decisions_sorted_by_score_desc(self, db):
        """After GET, decisions must come back in score DESC order."""
        # Force a zero-stock ingredient to guarantee a high-score signal
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        result = get_owner_decisions(db)

        scores = [d["decision_score"] for d in result["decisions"]]
        assert scores == sorted(scores, reverse=True), f"Not sorted DESC: {scores}"

        cleanup_ingredient(db, ing.id)

    def test_tiebreak_by_id_asc(self, db):
        """
        Two decisions with identical score must be ordered id ASC.
        We use two slow-moving ingredients (both score = medium base = 50).
        """
        ing_a, _ = make_ingredient(db, stock_quantity=Decimal("50.00"), name="AAA_slow")
        ing_b, _ = make_ingredient(db, stock_quantity=Decimal("50.00"), name="ZZZ_slow")
        # Remove any movements so they're both slow-moving
        _cleanup_movements(db, ing_a.id)
        _cleanup_movements(db, ing_b.id)

        result = get_owner_decisions(db)
        slow = [d for d in result["decisions"] if d["type"] == "slow_moving"]
        ids = [d["id"] for d in slow]
        assert ids == sorted(ids), f"Tiebreak not id ASC: {ids}"

        cleanup_ingredient(db, ing_a.id)
        cleanup_ingredient(db, ing_b.id)

    def test_blocking_decisions_score_higher_than_non_blocking_same_severity(self):
        """
        A blocking high-severity signal (100+25=125) must outscore
        a non-blocking high-severity signal (100).
        """
        blocking_score     = _decision_score("high", "sla_risk", {"critical_count": 0, "warning_count": 0}, True)
        non_blocking_score = _decision_score("high", "slow_moving", {}, False)
        assert blocking_score > non_blocking_score


# ---------------------------------------------------------------------------
# 5. Signal detection (integration)
# ---------------------------------------------------------------------------


class TestStockRiskSignals:
    def test_zero_stock_is_high_severity(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        signals = _stock_risk_signals(db)
        match = next((s for s in signals if s["data"]["ingredient_id"] == ing.id), None)
        assert match is not None
        assert match["severity"] == "high"
        assert match["decision_score"] > 100  # has urgency bonus
        assert match["blocking_vs_non_blocking"] is True
        cleanup_ingredient(db, ing.id)

    def test_high_velocity_high_severity(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("5.00"))
        _add_movement(db, ing.id, -24.0, hours_ago=12)  # 1g/h → stockout 5h < 6h
        signals = _stock_risk_signals(db)
        match = next((s for s in signals if s["data"]["ingredient_id"] == ing.id), None)
        assert match is not None
        assert match["severity"] == "high"
        assert match["data"]["hours_to_stockout"] < 6.0
        _cleanup_movements(db, ing.id)
        cleanup_ingredient(db, ing.id)

    def test_healthy_stock_not_flagged(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        signals = _stock_risk_signals(db)
        flagged = {s["data"]["ingredient_id"] for s in signals}
        assert ing.id not in flagged
        cleanup_ingredient(db, ing.id)

    def test_old_movements_not_counted(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("5.00"))
        _add_movement(db, ing.id, -48.0, hours_ago=25)  # outside 24h window
        signals = _stock_risk_signals(db)
        match = next((s for s in signals if s["data"]["ingredient_id"] == ing.id), None)
        assert match is not None
        assert match["data"]["velocity_per_hour"] == 0.0
        _cleanup_movements(db, ing.id)
        cleanup_ingredient(db, ing.id)


class TestSlowMovingSignals:
    def test_no_demand_flags_ingredient(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        _cleanup_movements(db, ing.id)
        signals = _slow_moving_signals(db)
        match = next((s for s in signals if s["data"]["ingredient_id"] == ing.id), None)
        assert match is not None
        assert match["type"] == "slow_moving"
        assert match["blocking_vs_non_blocking"] is False
        cleanup_ingredient(db, ing.id)

    def test_recent_demand_clears_flag(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        _add_movement(db, ing.id, -5.0, hours_ago=2)
        signals = _slow_moving_signals(db)
        flagged = {s["data"]["ingredient_id"] for s in signals}
        assert ing.id not in flagged
        _cleanup_movements(db, ing.id)
        cleanup_ingredient(db, ing.id)


class TestSlaRiskSignals:
    def test_critical_breach_is_high_blocking(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        oid = r.json()["order_id"]
        _backdate(db, oid, minutes_ago=12)

        signals = _sla_risk_signals(db)
        assert len(signals) >= 1
        s = signals[0]
        assert s["severity"] == "high"
        assert s["blocking_vs_non_blocking"] is True
        assert oid in s["data"]["critical_order_ids"]
        cleanup_ingredient(db, ing.id)

    def test_fresh_order_not_in_sla_signal(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        oid = r.json()["order_id"]

        for s in _sla_risk_signals(db):
            all_ids = s["data"]["critical_order_ids"] + s["data"]["warning_order_ids"]
            assert oid not in all_ids
        cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 6. get_owner_decisions envelope
# ---------------------------------------------------------------------------


class TestGetOwnerDecisions:
    def test_envelope_structure(self, db):
        result = get_owner_decisions(db)
        for field in ("decisions", "generated_at", "signals_evaluated", "active_count", "summary"):
            assert field in result
        assert result["signals_evaluated"] == 5

    def test_summary_counts_accurate(self, db):
        result = get_owner_decisions(db)
        d = result["decisions"]
        s = result["summary"]
        assert s["high"]   == sum(1 for x in d if x["severity"] == "high")
        assert s["medium"] == sum(1 for x in d if x["severity"] == "medium")
        assert s["low"]    == sum(1 for x in d if x["severity"] == "low")

    def test_active_count_equals_len(self, db):
        result = get_owner_decisions(db)
        assert result["active_count"] == len(result["decisions"])

    def test_generated_at_is_utc_iso(self, db):
        result = get_owner_decisions(db)
        parsed = datetime.fromisoformat(result["generated_at"])
        assert parsed.tzinfo is not None

    def test_all_decisions_have_lifecycle_fields(self, db):
        result = get_owner_decisions(db)
        for d in result["decisions"]:
            for field in ("status", "acknowledged_at", "completed_at",
                          "actor_id", "resolution_note", "why_now", "expected_impact",
                          "decision_score", "blocking_vs_non_blocking"):
                assert field in d, f"Missing {field} in {d['id']}"

    def test_signal_failure_does_not_crash(self, db, monkeypatch):
        from app.services import decision_engine as de
        monkeypatch.setattr(de, "_stock_risk_signals", lambda db: (_ for _ in ()).throw(RuntimeError("fail")))
        result = get_owner_decisions(db)
        assert "decisions" in result

    def test_upsert_preserves_lifecycle_on_re_evaluation(self, db):
        """
        Calling get_owner_decisions twice must not reset a pending→acknowledged
        decision back to pending.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

        result1 = get_owner_decisions(db)
        decision_id = next(d["id"] for d in result1["decisions"] if d["type"] == "stock_risk")

        # Acknowledge it
        apply_decision_action(db, decision_id, "acknowledge", actor_id="owner1")

        # Re-evaluate
        result2 = get_owner_decisions(db)
        d = next((x for x in result2["decisions"] if x["id"] == decision_id), None)
        assert d is not None
        assert d["status"] == "acknowledged", "Re-evaluation must preserve acknowledged status"

        cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 7. Action lifecycle transitions
# ---------------------------------------------------------------------------


class TestActionLifecycle:
    def test_acknowledge_pending(self, db):
        row = _seed_decision(db, f"test_ack_{uuid.uuid4().hex[:6]}")
        result = apply_decision_action(db, row.decision_id, "acknowledge", actor_id="staff1")
        assert result["status"] == "acknowledged"
        assert result["acknowledged_at"] is not None
        assert result["actor_id"] == "staff1"
        _cleanup_decision(db, row.decision_id)

    def test_complete_pending(self, db):
        row = _seed_decision(db, f"test_comp_{uuid.uuid4().hex[:6]}")
        result = apply_decision_action(db, row.decision_id, "complete",
                                       actor_id="mgr1", resolution_note="Done")
        assert result["status"] == "completed"
        assert result["completed_at"] is not None
        assert result["resolution_note"] == "Done"
        _cleanup_decision(db, row.decision_id)

    def test_complete_acknowledged(self, db):
        row = _seed_decision(db, f"test_ack_comp_{uuid.uuid4().hex[:6]}", status="acknowledged")
        result = apply_decision_action(db, row.decision_id, "complete")
        assert result["status"] == "completed"
        _cleanup_decision(db, row.decision_id)

    def test_dismiss_pending(self, db):
        row = _seed_decision(db, f"test_dis_{uuid.uuid4().hex[:6]}")
        result = apply_decision_action(db, row.decision_id, "dismiss",
                                       resolution_note="Not relevant today")
        assert result["status"] == "dismissed"
        assert result["completed_at"] is not None
        assert result["resolution_note"] == "Not relevant today"
        _cleanup_decision(db, row.decision_id)

    def test_dismiss_acknowledged(self, db):
        row = _seed_decision(db, f"test_ack_dis_{uuid.uuid4().hex[:6]}", status="acknowledged")
        result = apply_decision_action(db, row.decision_id, "dismiss")
        assert result["status"] == "dismissed"
        _cleanup_decision(db, row.decision_id)

    def test_acknowledge_already_acknowledged_raises(self, db):
        row = _seed_decision(db, f"test_aa_{uuid.uuid4().hex[:6]}", status="acknowledged")
        with pytest.raises(ValueError, match="Cannot"):
            apply_decision_action(db, row.decision_id, "acknowledge")
        _cleanup_decision(db, row.decision_id)

    def test_acknowledge_completed_raises(self, db):
        row = _seed_decision(db, f"test_ac_{uuid.uuid4().hex[:6]}", status="completed")
        with pytest.raises(ValueError, match="Cannot"):
            apply_decision_action(db, row.decision_id, "acknowledge")
        _cleanup_decision(db, row.decision_id)

    def test_complete_dismissed_raises(self, db):
        row = _seed_decision(db, f"test_cd_{uuid.uuid4().hex[:6]}", status="dismissed")
        with pytest.raises(ValueError, match="Cannot"):
            apply_decision_action(db, row.decision_id, "complete")
        _cleanup_decision(db, row.decision_id)

    def test_unknown_action_raises(self, db):
        row = _seed_decision(db, f"test_unk_{uuid.uuid4().hex[:6]}")
        with pytest.raises(ValueError, match="Unknown action"):
            apply_decision_action(db, row.decision_id, "fly_away")
        _cleanup_decision(db, row.decision_id)

    def test_not_found_raises(self, db):
        with pytest.raises(LookupError):
            apply_decision_action(db, "nonexistent_decision_xyz", "acknowledge")


# ---------------------------------------------------------------------------
# 8. Cooldown / deduplication
# ---------------------------------------------------------------------------


class TestCooldownBehavior:
    def test_completed_within_cooldown_is_suppressed(self, db):
        """
        A completed decision updated just now must NOT reappear as pending
        on the next evaluation within the cooldown window.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

        # First evaluation — creates pending row
        result1 = get_owner_decisions(db)
        decision_id = next(
            d["id"] for d in result1["decisions"] if d["type"] == "stock_risk"
            and d["data"].get("ingredient_id") == ing.id
        )

        # Complete it
        apply_decision_action(db, decision_id, "complete")

        # Second evaluation — must be suppressed (within cooldown)
        result2 = get_owner_decisions(db)
        ids = [d["id"] for d in result2["decisions"]]
        assert decision_id not in ids, "Completed decision should be suppressed within cooldown"

        cleanup_ingredient(db, ing.id)

    def test_completed_after_cooldown_resets_to_pending(self, db):
        """
        A completed decision whose updated_at is older than COOLDOWN_HOURS
        must re-appear as pending on the next evaluation.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

        result1 = get_owner_decisions(db)
        decision_id = next(
            d["id"] for d in result1["decisions"] if d["type"] == "stock_risk"
            and d["data"].get("ingredient_id") == ing.id
        )

        # Complete it
        apply_decision_action(db, decision_id, "complete")

        # Backdate the row's updated_at past the cooldown window
        expired_ts = datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS + 1)
        db.query(OwnerDecision).filter(
            OwnerDecision.decision_id == decision_id
        ).update({"updated_at": expired_ts}, synchronize_session=False)
        db.commit()

        # Third evaluation — should reset and re-appear
        result2 = get_owner_decisions(db)
        match = next((d for d in result2["decisions"] if d["id"] == decision_id), None)
        assert match is not None, "Decision should reappear after cooldown expiry"
        assert match["status"] == "pending"
        assert match["acknowledged_at"] is None
        assert match["completed_at"] is None

        cleanup_ingredient(db, ing.id)

    def test_dismissed_within_cooldown_is_suppressed(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

        result1 = get_owner_decisions(db)
        decision_id = next(
            d["id"] for d in result1["decisions"] if d["type"] == "stock_risk"
            and d["data"].get("ingredient_id") == ing.id
        )
        apply_decision_action(db, decision_id, "dismiss")

        result2 = get_owner_decisions(db)
        assert decision_id not in [d["id"] for d in result2["decisions"]]

        cleanup_ingredient(db, ing.id)

    def test_pending_decision_updated_not_reset(self, db):
        """
        A pending decision must have its mutable fields updated on re-evaluation,
        without changing the status.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

        result1 = get_owner_decisions(db)
        decision_id = next(
            d["id"] for d in result1["decisions"] if d["type"] == "stock_risk"
            and d["data"].get("ingredient_id") == ing.id
        )
        assert result1["decisions"][0]["status"] == "pending" or any(
            d["status"] == "pending" for d in result1["decisions"] if d["id"] == decision_id
        )

        # Re-evaluate — row must still be pending and score/description refreshed
        result2 = get_owner_decisions(db)
        match = next(d for d in result2["decisions"] if d["id"] == decision_id)
        assert match["status"] == "pending"
        assert match["decision_score"] > 0

        cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 9. HTTP endpoint tests
# ---------------------------------------------------------------------------


class TestDecisionsEndpointGet:
    def test_returns_200(self):
        assert client.get("/owner/decisions/").status_code == 200

    def test_response_shape(self):
        body = client.get("/owner/decisions/").json()
        for field in ("decisions", "generated_at", "signals_evaluated", "active_count", "summary"):
            assert field in body

    def test_each_decision_has_all_fields(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        body = client.get("/owner/decisions/").json()
        required = (
            "id", "type", "severity", "decision_score", "blocking_vs_non_blocking",
            "title", "description", "impact", "recommended_action",
            "why_now", "expected_impact", "data",
            "status", "acknowledged_at", "completed_at", "actor_id", "resolution_note",
        )
        for d in body["decisions"]:
            for f in required:
                assert f in d, f"Missing field '{f}' in decision {d['id']}"
        cleanup_ingredient(db, ing.id)

    def test_severity_values_valid(self):
        body = client.get("/owner/decisions/").json()
        for d in body["decisions"]:
            assert d["severity"] in ("high", "medium", "low")

    def test_type_values_valid(self):
        valid = {"stock_risk", "demand_spike", "slow_moving", "sla_risk", "revenue_anomaly"}
        body = client.get("/owner/decisions/").json()
        for d in body["decisions"]:
            assert d["type"] in valid

    def test_status_values_valid(self):
        body = client.get("/owner/decisions/").json()
        for d in body["decisions"]:
            assert d["status"] in ("pending", "acknowledged", "completed", "dismissed")

    def test_sorted_by_score_desc(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        body = client.get("/owner/decisions/").json()
        scores = [d["decision_score"] for d in body["decisions"]]
        assert scores == sorted(scores, reverse=True)
        cleanup_ingredient(db, ing.id)


class TestDecisionsEndpointPatch:
    def test_acknowledge_via_http(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        body = client.get("/owner/decisions/").json()
        decision_id = next(d["id"] for d in body["decisions"] if d["type"] == "stock_risk")

        r = client.patch(
            f"/owner/decisions/{decision_id}",
            json={"action": "acknowledge", "actor_id": "owner42"},
        )
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "acknowledged"
        assert result["actor_id"] == "owner42"
        assert result["acknowledged_at"] is not None

        cleanup_ingredient(db, ing.id)

    def test_complete_via_http(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        body = client.get("/owner/decisions/").json()
        decision_id = next(d["id"] for d in body["decisions"] if d["type"] == "stock_risk")

        r = client.patch(
            f"/owner/decisions/{decision_id}",
            json={"action": "complete", "resolution_note": "Restocked."},
        )
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "completed"
        assert result["resolution_note"] == "Restocked."

        cleanup_ingredient(db, ing.id)

    def test_dismiss_via_http(self, db):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))
        body = client.get("/owner/decisions/").json()
        decision_id = next(d["id"] for d in body["decisions"] if d["type"] == "stock_risk")

        r = client.patch(
            f"/owner/decisions/{decision_id}",
            json={"action": "dismiss", "resolution_note": "Will handle tomorrow."},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "dismissed"

        cleanup_ingredient(db, ing.id)

    def test_invalid_transition_returns_409(self, db):
        row = _seed_decision(db, f"http_inv_{uuid.uuid4().hex[:6]}", status="completed")
        r = client.patch(
            f"/owner/decisions/{row.decision_id}",
            json={"action": "acknowledge"},
        )
        assert r.status_code == 409
        _cleanup_decision(db, row.decision_id)

    def test_unknown_action_returns_409(self, db):
        row = _seed_decision(db, f"http_unk_{uuid.uuid4().hex[:6]}")
        r = client.patch(
            f"/owner/decisions/{row.decision_id}",
            json={"action": "teleport"},
        )
        assert r.status_code == 409
        _cleanup_decision(db, row.decision_id)

    def test_nonexistent_decision_returns_404(self):
        r = client.patch(
            "/owner/decisions/this_does_not_exist_xyz",
            json={"action": "acknowledge"},
        )
        assert r.status_code == 404

    def test_patch_response_has_all_fields(self, db):
        row = _seed_decision(db, f"http_shape_{uuid.uuid4().hex[:6]}")
        r = client.patch(f"/owner/decisions/{row.decision_id}", json={"action": "acknowledge"})
        assert r.status_code == 200
        result = r.json()
        for f in ("id", "type", "severity", "decision_score", "blocking_vs_non_blocking",
                  "status", "why_now", "expected_impact", "acknowledged_at"):
            assert f in result, f"Missing {f}"
        _cleanup_decision(db, row.decision_id)
