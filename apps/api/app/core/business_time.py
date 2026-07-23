"""
Business time — the single place that decides where a business day starts.

The rule this module encodes
----------------------------
**Storage is UTC. Reporting is local.**

Every timestamp column in SweetOps is ``DateTime(timezone=True)`` (Postgres
``timestamptz``) and every write stores an aware UTC instant. That does not
change here and must not change: event ordering, the payment ledger, audit
rows, session expiry and idempotency windows are all instant-based and are
correct precisely because they are UTC.

What *was* wrong is the reporting layer. A shop in Istanbul (UTC+03 year-round)
had its "today" end at 03:00 local, because ``created_at::date`` and
``EXTRACT(HOUR FROM created_at)`` were evaluated in UTC. Everything sold between
midnight and 03:00 — ordinary hours for a dessert shop — landed on the previous
day's report, and every hourly bucket was labelled three hours off the clock on
the wall. See ``docs/RUNTIME_PRODUCT_GAP_REVIEW.md`` F-04.

So: a **business day** is a calendar day in the configured business timezone,
and this module converts it into the half-open UTC interval ``[start, end)``
that the database can actually be queried with. Half-open, always — an inclusive
upper bound either double-counts the boundary instant or silently drops the last
microsecond of the day.

Two ways to bucket, and when to use which
-----------------------------------------
1. **UTC bounds** (:func:`business_day_bounds_utc`, :func:`business_date_bounds_utc`,
   :func:`last_n_days_bounds_utc`) — preferred. Produces plain
   ``col >= :start AND col < :end`` predicates that are DB-neutral and can use
   an index on the timestamp column. Use this for every window filter.
2. **SQL expressions** (:func:`business_date_sql_expression`,
   :func:`business_hour_sql_expression`) — for GROUP BY only, where a row must
   be assigned to a local day or a local hour rather than merely included in a
   range. These are not sargable; keep a bounds filter in the WHERE clause too.

Why the timezone is not interpolated unsafely
---------------------------------------------
The SQL helpers embed the zone name as a literal because a bind parameter cannot
be threaded through every ``text()`` call site without inviting a
forgot-the-param bug. The name is therefore validated twice: it must resolve via
``zoneinfo.ZoneInfo`` (so it is a real IANA zone) and it must match
:data:`_IANA_NAME_RE` (letters, digits, ``_ + - /`` only — no quote can survive).
Both checks run at import, so a typo in ``BUSINESS_TIMEZONE`` fails the process
at startup instead of quietly reporting in the wrong zone.

Nothing here reads the machine's local timezone. ``ZoneInfo`` data is bundled
with the stdlib (``tzdata`` on Windows), so results are identical on a developer
laptop, in CI and in the container.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.config import settings

__all__ = [
    "get_business_timezone",
    "get_business_timezone_name",
    "utc_now",
    "business_now",
    "business_today",
    "to_business",
    "to_utc",
    "business_day_bounds_utc",
    "local_date_to_utc_bounds",
    "business_date_bounds_utc",
    "last_n_days_bounds_utc",
    "business_date_sql_expression",
    "business_hour_sql_expression",
]

# IANA zone names are ASCII and never contain a quote or a semicolon. Anything
# outside this alphabet is rejected before it can reach a SQL string.
_IANA_NAME_RE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$")


@lru_cache(maxsize=None)
def _resolve(name: str) -> ZoneInfo:
    if not _IANA_NAME_RE.match(name or ""):
        raise ValueError(
            f"BUSINESS_TIMEZONE {name!r} is not a valid IANA timezone name "
            f"(expected e.g. 'Europe/Istanbul')."
        )
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError and friends
        raise ValueError(
            f"BUSINESS_TIMEZONE {name!r} could not be resolved by zoneinfo. "
            f"On a slim container image this usually means the 'tzdata' package "
            f"is missing."
        ) from exc


def get_business_timezone_name() -> str:
    """The configured IANA zone name, validated."""
    name = settings.BUSINESS_TIMEZONE
    _resolve(name)  # validate (cached)
    return name


def get_business_timezone() -> ZoneInfo:
    """The configured business timezone. Never the machine's local zone."""
    return _resolve(settings.BUSINESS_TIMEZONE)


# ── Clocks ────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    """Aware UTC now — what gets stored. Unchanged semantics, named for clarity."""
    return datetime.now(timezone.utc)


def business_now() -> datetime:
    """Aware now, expressed in the business timezone. For reporting only."""
    return utc_now().astimezone(get_business_timezone())


def business_today() -> date:
    """
    The business calendar date right now.

    At 2026-07-23 22:30 UTC this is 2026-07-24 in Istanbul — that is the whole
    point, and it is why "today" must never be ``utc_now().date()``.
    """
    return business_now().date()


def to_business(moment: datetime) -> datetime:
    """
    Express a stored instant in business local time.

    A naive datetime is assumed to be UTC, matching how the ORM hands back rows
    from a driver that dropped the zone. Assuming *local* would make behaviour
    depend on the machine, which is exactly what this module exists to prevent.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(get_business_timezone())


def to_utc(moment: datetime) -> datetime:
    """Normalise any aware (or assumed-UTC naive) instant to UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ── Day boundaries ────────────────────────────────────────────────────────────

def business_day_bounds_utc(day: date | None = None) -> tuple[datetime, datetime]:
    """
    Half-open UTC interval ``[start, end)`` covering one business calendar day.

    ``day=None`` means the business day in progress right now.

    For Istanbul (UTC+03) the business day 2026-07-24 is
    ``[2026-07-23 21:00Z, 2026-07-24 21:00Z)`` — it starts on the *previous* UTC
    date. Filter with ``col >= start AND col < end``.
    """
    if day is None:
        day = business_today()
    tz = get_business_timezone()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    # Add the day in local terms, then convert — so a zone with DST lands on the
    # next local midnight rather than 24 fixed hours later.
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_date_to_utc_bounds(day: date) -> tuple[datetime, datetime]:
    """Explicit alias of :func:`business_day_bounds_utc` for a known local date."""
    return business_day_bounds_utc(day)


def business_date_bounds_utc(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    """
    Half-open UTC interval spanning business days ``start_day``..``end_day``
    **inclusive** of both ends.

    ``business_date_bounds_utc(d, d)`` is the same single day as
    :func:`business_day_bounds_utc`.
    """
    if end_day < start_day:
        raise ValueError(f"end_day {end_day} is before start_day {start_day}")
    start, _ = business_day_bounds_utc(start_day)
    _, end = business_day_bounds_utc(end_day)
    return start, end


def last_n_days_bounds_utc(
    days: int, end_day: date | None = None
) -> tuple[datetime, datetime]:
    """
    Half-open UTC interval for the last ``days`` business days **including**
    ``end_day`` (default: today).

    ``last_n_days_bounds_utc(7)`` is seven whole local days ending with the day
    in progress — not "168 hours ago", which would cut yesterday in half and put
    a partial day at the left edge of every chart.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if end_day is None:
        end_day = business_today()
    return business_date_bounds_utc(end_day - timedelta(days=days - 1), end_day)


# ── SQL expressions (GROUP BY only — see module docstring) ────────────────────

def business_date_sql_expression(column: str) -> str:
    """
    SQL that yields the business-local calendar date of a timestamptz column.

    ``AT TIME ZONE 'Europe/Istanbul'`` converts the instant to a naive local
    timestamp; the cast then takes the local date. Used for ``GROUP BY`` so a
    22:00Z order on 2026-07-23 buckets into the 2026-07-24 local day.
    """
    return f"(({column}) AT TIME ZONE '{get_business_timezone_name()}')::date"


def business_hour_sql_expression(column: str) -> str:
    """
    SQL that yields the business-local hour-of-day (0-23) of a timestamptz column.

    This is what makes "Saatlik Talep" and ``peak_hour`` agree with the clock on
    the shop wall instead of being three hours early.
    """
    return (
        f"EXTRACT(HOUR FROM (({column}) AT TIME ZONE "
        f"'{get_business_timezone_name()}'))::int"
    )
