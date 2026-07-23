"""
Business timezone — the day boundary every report is keyed on.

The defect these tests lock down (RUNTIME_PRODUCT_GAP_REVIEW.md F-04): daily and
hourly reporting buckets were computed in UTC while the shop trades in Istanbul
(UTC+03). The business day therefore ended at 03:00 local — everything sold
between local midnight and 03:00 landed on the previous day's report — and every
hour bucket was labelled three hours off the wall clock.

What is asserted here
---------------------
  * a business day is a LOCAL calendar day, expressed as the half-open UTC
    interval covering it; for Istanbul it opens at 21:00Z on the PREVIOUS UTC date;
  * orders and payments either side of local midnight land on the correct
    business day;
  * hour buckets (Saatlik Talep, peak_hour) are local hours;
  * "today", 7-day and 30-day windows are whole local days;
  * none of this regressed store scoping or the payment ledger.

Determinism
-----------
Nothing here reads the machine's local timezone, and nothing asserts against a
hard-coded wall-clock time. Rows are placed at instants derived from the SAME
helpers the services use (``day_start``, ``day_start - 1s``, …), so a boundary
bug shows up as a misclassified row regardless of when the suite runs. The one
genuinely time-sensitive risk — the business date rolling over mid-test — is
handled by :func:`_assert_same_business_day`, which skips rather than flakes.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core import business_time as bt
from app.core.config import settings
from app.models.order import Order
from app.models.order_status_event import OrderStatusEvent
from app.models.payment_settlement import PaymentSettlement
from tests.conftest import _ledger_maintenance, make_authed_client

ISTANBUL = ZoneInfo("Europe/Istanbul")
ONE_SECOND = timedelta(seconds=1)

DASH = "/owner/operational-dashboard"


# =============================================================================
# Part 1 — the helpers, with no database and no machine clock involved
# =============================================================================

class TestBusinessDayBounds:
    """Pure arithmetic on a fixed date — identical on any machine, in any zone."""

    def test_default_timezone_is_istanbul(self):
        assert settings.BUSINESS_TIMEZONE == "Europe/Istanbul"
        assert bt.get_business_timezone() == ISTANBUL

    def test_business_day_starts_at_21_utc_on_the_previous_utc_date(self):
        """
        The headline property. Istanbul is UTC+03 year-round, so the business day
        2026-07-24 begins at 21:00Z on 2026-07-23 — a DIFFERENT UTC date. Any
        implementation using ``created_at::date`` in UTC fails this.
        """
        start, end = bt.business_day_bounds_utc(date(2026, 7, 24))

        assert start == datetime(2026, 7, 23, 21, 0, tzinfo=timezone.utc)
        assert start.date() == date(2026, 7, 23)          # previous UTC date
        assert end == datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)

    def test_bounds_are_half_open_and_exactly_one_local_day(self):
        start, end = bt.business_day_bounds_utc(date(2026, 7, 24))
        assert end - start == timedelta(days=1)
        # Local view: midnight to midnight.
        assert start.astimezone(ISTANBUL).time() == time(0, 0)
        assert end.astimezone(ISTANBUL).time() == time(0, 0)
        # Half-open: the end instant belongs to the NEXT day, never to both.
        next_start, _ = bt.business_day_bounds_utc(date(2026, 7, 25))
        assert next_start == end

    def test_consecutive_days_tile_without_gap_or_overlap(self):
        day = date(2026, 7, 24)
        for _ in range(40):
            _, end = bt.business_day_bounds_utc(day)
            nxt_start, _ = bt.business_day_bounds_utc(day + timedelta(days=1))
            assert end == nxt_start
            day += timedelta(days=1)

    def test_local_date_to_utc_bounds_is_the_same_mapping(self):
        assert bt.local_date_to_utc_bounds(date(2026, 7, 24)) == \
            bt.business_day_bounds_utc(date(2026, 7, 24))

    def test_business_date_bounds_utc_is_inclusive_of_both_ends(self):
        start, end = bt.business_date_bounds_utc(date(2026, 7, 20), date(2026, 7, 24))
        assert start == datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)
        assert end - start == timedelta(days=5)

    def test_business_date_bounds_rejects_reversed_range(self):
        with pytest.raises(ValueError):
            bt.business_date_bounds_utc(date(2026, 7, 24), date(2026, 7, 20))


class TestTodayAndNow:
    """``business_today`` must follow the local calendar, not the UTC one."""

    @staticmethod
    def _freeze(monkeypatch, moment: datetime) -> None:
        monkeypatch.setattr(bt, "utc_now", lambda: moment)

    def test_after_local_midnight_today_is_the_next_utc_date(self, monkeypatch):
        # 21:30Z on 23 July == 00:30 on 24 July in Istanbul. The shop is open and
        # every sale from here belongs to the 24th.
        self._freeze(monkeypatch, datetime(2026, 7, 23, 21, 30, tzinfo=timezone.utc))
        assert bt.business_today() == date(2026, 7, 24)
        assert bt.business_now().hour == 0

    def test_before_local_midnight_today_is_the_same_utc_date(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 7, 23, 20, 30, tzinfo=timezone.utc))
        assert bt.business_today() == date(2026, 7, 23)
        assert bt.business_now().hour == 23

    def test_the_02_00_local_sale_that_used_to_be_counted_yesterday(self, monkeypatch):
        # 23:00Z on 23 July == 02:00 local on the 24th — the exact window the
        # audit called out. UTC would say the 23rd; the shop says the 24th.
        moment = datetime(2026, 7, 23, 23, 0, tzinfo=timezone.utc)
        self._freeze(monkeypatch, moment)
        assert moment.date() == date(2026, 7, 23)      # what the bug reported
        assert bt.business_today() == date(2026, 7, 24)  # what the shop lived

    def test_utc_now_is_still_utc(self):
        """Storage semantics are untouched: utc_now() is an aware UTC instant."""
        assert bt.utc_now().tzinfo is timezone.utc

    def test_business_now_carries_the_local_offset(self):
        assert bt.business_now().utcoffset() == timedelta(hours=3)


class TestRollingWindows:
    """"Son 7 gün" / "Son 30 gün" must be whole local days, not N×24 hours."""

    def test_last_7_days_covers_seven_whole_local_days(self):
        start, end = bt.last_n_days_bounds_utc(7, end_day=date(2026, 7, 24))
        assert end - start == timedelta(days=7)
        # Starts at local midnight six days before the end day (end day included).
        assert start == datetime(2026, 7, 17, 21, 0, tzinfo=timezone.utc)
        assert start.astimezone(ISTANBUL).date() == date(2026, 7, 18)
        assert end == datetime(2026, 7, 24, 21, 0, tzinfo=timezone.utc)

    def test_last_30_days_covers_thirty_whole_local_days(self):
        start, end = bt.last_n_days_bounds_utc(30, end_day=date(2026, 7, 24))
        assert end - start == timedelta(days=30)
        assert start.astimezone(ISTANBUL).time() == time(0, 0)
        assert start.astimezone(ISTANBUL).date() == date(2026, 6, 25)

    def test_window_edges_are_local_midnights_not_the_current_time(self):
        """
        The old code did ``now - timedelta(days=7)``, which put a half day at
        each edge and made the chart's average line depend on the query time.
        """
        for n in (1, 7, 14, 30):
            start, end = bt.last_n_days_bounds_utc(n, end_day=date(2026, 7, 24))
            assert start.astimezone(ISTANBUL).time() == time(0, 0)
            assert end.astimezone(ISTANBUL).time() == time(0, 0)

    def test_single_day_window_equals_the_day_bounds(self):
        assert bt.last_n_days_bounds_utc(1, end_day=date(2026, 7, 24)) == \
            bt.business_day_bounds_utc(date(2026, 7, 24))

    def test_zero_or_negative_window_rejected(self):
        with pytest.raises(ValueError):
            bt.last_n_days_bounds_utc(0)


class TestIndependenceFromMachineAndConfig:

    def test_result_does_not_depend_on_process_tz_env(self, monkeypatch):
        """
        A developer laptop in UTC, CI in UTC and a container with TZ=America/
        New_York must all agree. zoneinfo is consulted explicitly, so TZ is
        never read.
        """
        expected = bt.business_day_bounds_utc(date(2026, 7, 24))
        for tz_env in ("UTC", "America/New_York", "Asia/Tokyo", ""):
            monkeypatch.setenv("TZ", tz_env)
            assert bt.business_day_bounds_utc(date(2026, 7, 24)) == expected

    def test_timezone_is_configurable(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_TIMEZONE", "UTC")
        start, end = bt.business_day_bounds_utc(date(2026, 7, 24))
        assert start == datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)

    def test_unknown_timezone_fails_loudly(self, monkeypatch):
        """Never silently fall back to UTC — that is how F-04 stayed invisible."""
        monkeypatch.setattr(settings, "BUSINESS_TIMEZONE", "Europe/Istanbulll")
        with pytest.raises(ValueError):
            bt.get_business_timezone()

    def test_timezone_name_with_sql_metacharacters_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "BUSINESS_TIMEZONE", "UTC'; DROP TABLE orders;--")
        with pytest.raises(ValueError):
            bt.business_date_sql_expression("created_at")


class TestSqlExpressions:

    def test_date_expression_names_the_configured_zone(self):
        sql = bt.business_date_sql_expression("o.created_at")
        assert "AT TIME ZONE 'Europe/Istanbul'" in sql
        assert sql.endswith("::date")

    def test_hour_expression_extracts_a_local_hour(self):
        sql = bt.business_hour_sql_expression("created_at")
        assert "EXTRACT(HOUR FROM" in sql
        assert "AT TIME ZONE 'Europe/Istanbul'" in sql

    def test_date_expression_evaluates_locally_in_the_database(self, db):
        """The DB session runs in UTC — the cast must still yield the local date."""
        from sqlalchemy import text

        expr = bt.business_date_sql_expression("TIMESTAMPTZ '2026-07-23 23:00:00+00'")
        local_date = db.execute(text(f"SELECT {expr}")).scalar()
        assert local_date == date(2026, 7, 24)

    def test_hour_expression_evaluates_locally_in_the_database(self, db):
        from sqlalchemy import text

        expr = bt.business_hour_sql_expression("TIMESTAMPTZ '2026-07-23 23:00:00+00'")
        assert db.execute(text(f"SELECT {expr}")).scalar() == 2   # 02:00 local


# =============================================================================
# Part 2 — the reporting endpoints, against the real database
# =============================================================================

def _assert_same_business_day(day: date) -> None:
    """
    Guard against the business date rolling over mid-test (a ~1-in-a-million
    run at exactly 21:00Z). Skipping is honest; a flaky assertion is not.
    """
    if bt.business_today() != day:
        pytest.skip("business day rolled over during the test")


def _place_order_at(db, make_order, store_id: int, when: datetime, *,
                    total: str = "100.00", status: str = "DELIVERED") -> Order:
    """An order that exists at a chosen instant. created_at is UTC, as stored."""
    order = make_order(store_id, None, Decimal(total), status=status)
    order.created_at = when
    db.commit()
    db.refresh(order)
    return order


def _add_prep_events(db, order: Order, started: datetime, ready: datetime) -> None:
    """The IN_PREP → READY pair the kitchen timing summary measures between."""
    for status_to, ts in (("IN_PREP", started), ("READY", ready)):
        event = OrderStatusEvent(order_id=order.id, status_to=status_to, actor_type="STAFF")
        db.add(event)
        db.flush()
        event.created_at = ts
    db.commit()


def _owner_for(db, make_staff, store):
    return make_authed_client(db, make_staff("OWNER", store_id=store.id))


class TestOperationalDashboardDayBoundary:

    def test_order_just_after_local_midnight_counts_today(
        self, db, make_store, make_staff, make_order
    ):
        """
        An order at exactly the local midnight instant (21:00Z the PREVIOUS UTC
        day) belongs to today. Under the UTC boundary it was reported yesterday.
        """
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        _place_order_at(db, make_order, store.id, day_start)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        assert body["business_date"] == today.isoformat()
        assert body["orders"]["completed_today"] == 1
        # Proof the boundary really is local: the row's UTC date is yesterday's.
        assert day_start.date() == today - timedelta(days=1)

    def test_order_just_before_local_midnight_counts_yesterday(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        _place_order_at(db, make_order, store.id, day_start - ONE_SECOND)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        assert body["orders"]["completed_today"] == 0

    def test_only_the_orders_inside_the_local_day_are_counted(
        self, db, make_store, make_staff, make_order
    ):
        """One second either side of each edge — the full boundary picture."""
        today = bt.business_today()
        day_start, day_end = bt.business_day_bounds_utc(today)
        store = make_store()

        _place_order_at(db, make_order, store.id, day_start - ONE_SECOND)  # out
        _place_order_at(db, make_order, store.id, day_start)              # in
        _place_order_at(db, make_order, store.id, day_end - ONE_SECOND)   # in
        _place_order_at(db, make_order, store.id, day_end)                # out (tomorrow)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        assert body["orders"]["completed_today"] == 2

    def test_cancelled_orders_use_the_same_local_boundary(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        _place_order_at(db, make_order, store.id, day_start, status="CANCELLED")
        _place_order_at(db, make_order, store.id, day_start - ONE_SECOND, status="CANCELLED")

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        assert body["orders"]["cancelled_today"] == 1


class TestPaymentsDayBoundary:
    """The ledger stays the source of truth; only the window moved."""

    @staticmethod
    def _settle_at(db, client, order: Order, when: datetime) -> str:
        import uuid

        res = client.post(
            f"/cashier/orders/{order.id}/payments",
            json={"payment_method": "CASH"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert res.status_code == 200, res.text
        settlement_id = res.json()["settlement_id"]
        # Backdating a frozen ledger row needs the sanctioned ownership-gated
        # escape hatch; immutability is restored before the test asserts.
        with _ledger_maintenance(db):
            db.query(PaymentSettlement).filter(
                PaymentSettlement.id == settlement_id
            ).update({"completed_at": when}, synchronize_session=False)
        db.commit()
        return settlement_id

    def test_settlement_at_local_midnight_counts_today(
        self, db, make_store, make_table, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        table = make_table(store.id)
        cashier = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
        order = make_order(store.id, table.id, Decimal("60.00"))
        self._settle_at(db, cashier, order, day_start)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        assert body["payments"]["gross_collected_today"] == "60.00"
        assert body["payments"]["net_collected_today"] == "60.00"

    def test_settlement_just_before_local_midnight_counts_yesterday(
        self, db, make_store, make_table, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        table = make_table(store.id)
        cashier = make_authed_client(db, make_staff("CASHIER", store_id=store.id))
        order = make_order(store.id, table.id, Decimal("60.00"))
        self._settle_at(db, cashier, order, day_start - ONE_SECOND)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        assert body["payments"]["gross_collected_today"] == "0.00"

    def test_payment_ledger_semantics_unchanged(self, db, collected_ledger, make_staff):
        """
        Regression guard: money is still Σ completed allocations minus refunds,
        read from the ledger — the timezone work must not have touched the
        definition, only the window it is evaluated over.
        """
        env = collected_ledger
        today = bt.business_today()
        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, env.store).get(DASH).json()

        payments = body["payments"]
        assert payments["gross_collected_today"] == "100.00"
        assert payments["refunds_today"] == "10.00"
        assert payments["net_collected_today"] == "90.00"
        assert payments["currency"] == "TRY"


class TestHourBucketsAreLocal:

    def test_hourly_demand_buckets_use_the_local_hour(
        self, db, make_store, make_staff, make_order
    ):
        """
        The chart caption promises "en yoğun saatler". An order at the top of the
        current LOCAL hour must be labelled with that hour — not the UTC hour
        three earlier, which is what shipped.
        """
        today = bt.business_today()
        local_hour = bt.business_now().replace(minute=0, second=0, microsecond=0)
        when = local_hour.astimezone(timezone.utc)
        expected = f"{local_hour.hour:02d}:00"
        utc_label = f"{when.hour:02d}:00"

        store = make_store()
        _place_order_at(db, make_order, store.id, when)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get("/owner/hourly-demand").json()

        buckets = {p["hour_bucket"]: p["order_count"] for p in body["points"]}
        assert buckets == {expected: 1}
        # The test only proves something if the two labels genuinely differ.
        assert expected != utc_label

    def test_peak_hour_is_the_local_hour(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        local_hour = bt.business_now().replace(minute=0, second=0, microsecond=0)
        when = local_hour.astimezone(timezone.utc)

        store = make_store()
        _place_order_at(db, make_order, store.id, when)
        _place_order_at(db, make_order, store.id, when + timedelta(minutes=1))

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get("/owner/kpis").json()

        assert body["kpis"]["peak_hour"] == f"{local_hour.hour:02d}:00"
        assert body["kpis"]["peak_hour"] != f"{when.hour:02d}:00"

    def test_an_order_after_local_midnight_buckets_into_an_early_local_hour(
        self, db, make_store, make_staff, make_order
    ):
        """
        01:00 local (22:00Z the previous UTC day) is hour 01 of today — not
        hour 22 of yesterday.
        """
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        when = day_start + timedelta(hours=1)
        if when > bt.utc_now():
            pytest.skip("01:00 local has not happened yet today")

        store = make_store()
        _place_order_at(db, make_order, store.id, when)

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get("/owner/hourly-demand").json()

        assert [p["hour_bucket"] for p in body["points"]] == ["01:00"]
        assert when.hour == 22   # the UTC hour the old code would have shown


class TestTodayAndMultiDayWindows:

    def test_kpis_today_uses_the_business_day(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        _place_order_at(db, make_order, store.id, day_start, total="40.00")
        _place_order_at(db, make_order, store.id, day_start - ONE_SECOND, total="99.00")

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get("/owner/kpis").json()

        assert body["kpis"]["total_orders"] == 1
        assert body["kpis"]["gross_revenue"] == 40.0

    def test_daily_sales_groups_by_local_calendar_date(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        _place_order_at(db, make_order, store.id, day_start, total="40.00")
        _place_order_at(db, make_order, store.id, day_start - ONE_SECOND, total="20.00")

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get("/owner/daily-sales").json()

        by_date = {p["sales_date"]: p for p in body["points"]}
        yesterday = (today - timedelta(days=1)).isoformat()
        assert by_date[today.isoformat()]["gross_revenue"] == 40.0
        assert by_date[yesterday]["gross_revenue"] == 20.0

    def test_seven_day_window_starts_at_a_local_midnight(
        self, db, make_store, make_staff, make_order
    ):
        """
        The left edge of "son 7 gün" is a local midnight six days back. A row one
        second before it is out of the window; a row at it is in.
        """
        today = bt.business_today()
        window_start, _ = bt.last_n_days_bounds_utc(7, end_day=today)
        store = make_store()
        _place_order_at(db, make_order, store.id, window_start, total="11.00")
        _place_order_at(db, make_order, store.id, window_start - ONE_SECOND, total="77.00")

        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get("/owner/daily-sales").json()

        revenues = {p["sales_date"]: p["gross_revenue"] for p in body["points"]}
        first_day = (today - timedelta(days=6)).isoformat()
        assert revenues.get(first_day) == 11.0
        assert 77.0 not in revenues.values()

    def test_thirty_day_window_is_thirty_whole_local_days(self):
        """
        No endpoint serves 30 days yet (that is F-11, a separate finding), so the
        contract is pinned on the helper the future window will use.
        """
        today = bt.business_today()
        start, end = bt.last_n_days_bounds_utc(30, end_day=today)
        assert end - start == timedelta(days=30)
        assert start.astimezone(ISTANBUL).date() == today - timedelta(days=29)
        assert end == bt.business_day_bounds_utc(today)[1]


class TestKitchenTimingSummaryWindow:

    def test_prep_completed_after_local_midnight_is_todays_tempo(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()

        order = _place_order_at(db, make_order, store.id, day_start, status="READY")
        _add_prep_events(db, order, day_start + timedelta(minutes=1),
                         day_start + timedelta(minutes=6))

        _assert_same_business_day(today)
        kitchen = make_authed_client(db, make_staff("KITCHEN", store_id=store.id))
        body = kitchen.get("/kitchen/timing/summary").json()

        assert body["completed_orders_today"] == 1
        assert body["average_prep_seconds_today"] == 300

    def test_prep_completed_before_local_midnight_is_not_todays_tempo(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        before = day_start - timedelta(minutes=10)
        store = make_store()

        order = _place_order_at(db, make_order, store.id, before, status="READY")
        _add_prep_events(db, order, before + timedelta(minutes=1),
                         before + timedelta(minutes=6))

        _assert_same_business_day(today)
        kitchen = make_authed_client(db, make_staff("KITCHEN", store_id=store.id))
        body = kitchen.get("/kitchen/timing/summary").json()

        assert body["completed_orders_today"] == 0
        assert body["average_prep_seconds_today"] is None   # never a fabricated 0


class TestOwnerMetricsBusinessDate:

    def test_current_business_date_is_not_rejected_as_future(
        self, db, make_store, make_staff
    ):
        """
        Between 21:00Z and midnight UTC the local date is already tomorrow. An
        owner asking for the date on their own calendar must not get a 422.
        """
        today = bt.business_today()
        store = make_store()
        owner = _owner_for(db, make_staff, store)

        res = owner.get(f"/owner/metrics/?date={today.isoformat()}")
        assert res.status_code == 200, res.text
        assert res.json()["meta"]["target_date"] == today.isoformat()

    def test_tomorrow_is_still_rejected(self, db, make_store, make_staff):
        tomorrow = bt.business_today() + timedelta(days=1)
        store = make_store()
        owner = _owner_for(db, make_staff, store)

        res = owner.get(f"/owner/metrics/?date={tomorrow.isoformat()}")
        assert res.status_code == 422
        assert res.json()["detail"]["error"] == "future_date"

    def test_daily_metrics_bucket_by_local_date(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store = make_store()
        _place_order_at(db, make_order, store.id, day_start)
        _place_order_at(db, make_order, store.id, day_start - ONE_SECOND)

        _assert_same_business_day(today)
        owner = _owner_for(db, make_staff, store)
        body = owner.get(f"/owner/metrics/?date={today.isoformat()}").json()

        # sample_size for combo_usage_rate is the day's non-cancelled order count.
        assert body["conversion"]["combo_usage_rate"]["quality"]["sample_size"] == 1


class TestStoreScopingNotRegressed:
    """The window changed; the store filter did not."""

    def test_boundary_orders_stay_inside_their_own_store(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        day_start, _ = bt.business_day_bounds_utc(today)
        store_a = make_store()
        store_b = make_store()
        _place_order_at(db, make_order, store_a.id, day_start, total="40.00")
        _place_order_at(db, make_order, store_b.id, day_start, total="900.00")
        _place_order_at(db, make_order, store_b.id, day_start + timedelta(minutes=1),
                        total="900.00")

        _assert_same_business_day(today)
        owner_a = _owner_for(db, make_staff, store_a)
        owner_b = _owner_for(db, make_staff, store_b)

        assert owner_a.get(DASH).json()["orders"]["completed_today"] == 1
        assert owner_b.get(DASH).json()["orders"]["completed_today"] == 2
        assert owner_a.get("/owner/kpis").json()["kpis"]["gross_revenue"] == 40.0
        assert owner_b.get("/owner/kpis").json()["kpis"]["gross_revenue"] == 1800.0

    def test_hourly_demand_is_store_scoped(
        self, db, make_store, make_staff, make_order
    ):
        today = bt.business_today()
        local_hour = bt.business_now().replace(minute=0, second=0, microsecond=0)
        when = local_hour.astimezone(timezone.utc)
        store_a = make_store()
        store_b = make_store()
        _place_order_at(db, make_order, store_a.id, when)
        _place_order_at(db, make_order, store_b.id, when)
        _place_order_at(db, make_order, store_b.id, when + timedelta(minutes=2))

        _assert_same_business_day(today)
        a = _owner_for(db, make_staff, store_a).get("/owner/hourly-demand").json()
        b = _owner_for(db, make_staff, store_b).get("/owner/hourly-demand").json()

        assert [p["order_count"] for p in a["points"]] == [1]
        assert [p["order_count"] for p in b["points"]] == [2]


class TestStorageSemanticsUnchanged:

    def test_stored_timestamps_are_still_utc(
        self, db, make_store, make_order
    ):
        """
        The fix must not have started writing local time into the database. A
        freshly created order's created_at is an aware UTC instant within
        seconds of utc_now().
        """
        store = make_store()
        before = bt.utc_now()
        order = make_order(store.id, None, Decimal("10.00"))
        after = bt.utc_now()

        db.refresh(order)
        created = order.created_at
        assert created.utcoffset() == timedelta(0)
        assert before - timedelta(seconds=5) <= created <= after + timedelta(seconds=5)

    def test_dashboard_as_of_is_utc_but_business_date_is_local(
        self, db, make_store, make_staff
    ):
        today = bt.business_today()
        store = make_store()
        _assert_same_business_day(today)
        body = _owner_for(db, make_staff, store).get(DASH).json()

        as_of = datetime.fromisoformat(body["as_of"])
        assert as_of.utcoffset() == timedelta(0)
        assert body["business_date"] == today.isoformat()
