"""
Owner Analytics Service — MVP version using direct table queries.
No dependency on analytics.* schema or views.
"""
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from datetime import datetime, timezone, timedelta
from app.schemas.owner_analytics import (
    KPIsResponse, KPIsData,
    TopIngredientsResponse, TopIngredientItem,
    HourlyDemandResponse, HourlyDemandPoint,
    DailySalesResponse, DailySalesPoint,
    ForecastItem, IngredientForecastResponse
)


def get_current_utc():
    return datetime.now(timezone.utc)


def fetch_kpis(db: Session) -> KPIsResponse:
    """KPIs from direct queries on orders table."""
    now = get_current_utc()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Today's stats
    today_query = text("""
        SELECT 
            COUNT(id) as total_orders,
            COALESCE(SUM(total_amount), 0) as gross_revenue,
            COALESCE(AVG(total_amount), 0) as aov
        FROM orders
        WHERE created_at >= :today_start
    """)
    today_res = db.execute(today_query, {"today_start": today_start}).fetchone()

    # Active orders (NEW + IN_PREP + READY)
    active_query = text("""
        SELECT COUNT(id)
        FROM orders
        WHERE status IN ('NEW', 'IN_PREP', 'READY')
    """)
    active_res = db.execute(active_query).fetchone()

    # Delivered/completed today
    delivered_query = text("""
        SELECT COUNT(id)
        FROM orders
        WHERE status IN ('READY', 'DELIVERED')
          AND created_at >= :today_start
    """)
    delivered_res = db.execute(delivered_query, {"today_start": today_start}).fetchone()

    # Peak hour today
    peak_query = text("""
        SELECT EXTRACT(HOUR FROM created_at)::int as hour, COUNT(*) as cnt
        FROM orders
        WHERE created_at >= :today_start
        GROUP BY hour
        ORDER BY cnt DESC
        LIMIT 1
    """)
    peak_res = db.execute(peak_query, {"today_start": today_start}).fetchone()

    kpi_data = KPIsData(
        total_orders=int(today_res[0]) if today_res else 0,
        gross_revenue=float(today_res[1]) if today_res else 0.0,
        average_order_value=float(today_res[2]) if today_res else 0.0,
        active_orders_count=int(active_res[0]) if active_res else 0,
        delivered_orders_count=int(delivered_res[0]) if delivered_res else 0,
        peak_hour=f"{int(peak_res[0]):02d}:00" if peak_res and peak_res[0] is not None else None
    )

    return KPIsResponse(
        as_of=now,
        currency="TRY",
        kpis=kpi_data
    )


def fetch_top_ingredients(db: Session, limit: int = 5) -> TopIngredientsResponse:
    """Top ingredients by usage count from order_item_ingredients."""
    query = text("""
        WITH usage AS (
            SELECT
                i.name as ingredient_name,
                COUNT(oi.id) as total_used
            FROM order_item_ingredients oi
            JOIN ingredients i ON oi.ingredient_id = i.id
            GROUP BY i.name
        ),
        grand AS (
            SELECT COALESCE(SUM(total_used), 0) as grand_total FROM usage
        )
        SELECT
            u.ingredient_name,
            u.total_used,
            CASE WHEN g.grand_total > 0
                THEN ROUND(u.total_used::numeric / g.grand_total, 4)
                ELSE 0
            END as usage_share
        FROM usage u, grand g
        ORDER BY u.total_used DESC
        LIMIT :limit
    """)

    rows = db.execute(query, {"limit": limit}).fetchall()
    items = []
    for rank, row in enumerate(rows, start=1):
        items.append(TopIngredientItem(
            rank=rank,
            ingredient_name=row[0],
            usage_count=int(row[1]),
            usage_share=float(row[2])
        ))

    return TopIngredientsResponse(as_of=get_current_utc(), items=items)


def fetch_hourly_demand(db: Session) -> HourlyDemandResponse:
    """Hourly order counts for today."""
    today_start = get_current_utc().replace(hour=0, minute=0, second=0, microsecond=0)

    query = text("""
        SELECT
            EXTRACT(HOUR FROM created_at)::int as hour,
            COUNT(*) as order_count
        FROM orders
        WHERE created_at >= :today_start
        GROUP BY hour
        ORDER BY hour ASC
    """)

    rows = db.execute(query, {"today_start": today_start}).fetchall()
    points = [
        HourlyDemandPoint(hour_bucket=f"{int(r[0]):02d}:00", order_count=int(r[1]))
        for r in rows
    ]

    return HourlyDemandResponse(as_of=get_current_utc(), points=points)


def fetch_daily_sales(db: Session) -> DailySalesResponse:
    """Daily sales for last 7 days."""
    seven_days_ago = get_current_utc() - timedelta(days=7)

    query = text("""
        SELECT
            DATE(created_at) as sales_date,
            COUNT(id) as total_orders,
            COALESCE(SUM(total_amount), 0) as gross_revenue,
            COALESCE(AVG(total_amount), 0) as average_order_value
        FROM orders
        WHERE created_at >= :since
        GROUP BY DATE(created_at)
        ORDER BY sales_date ASC
    """)

    rows = db.execute(query, {"since": seven_days_ago}).fetchall()
    points = [
        DailySalesPoint(
            sales_date=str(r[0]),
            total_orders=int(r[1]),
            gross_revenue=float(r[2]),
            average_order_value=float(r[3])
        )
        for r in rows
    ]

    return DailySalesResponse(as_of=get_current_utc(), points=points)


def fetch_ingredient_forecast(db: Session) -> IngredientForecastResponse:
    """
    Simple forecast based on last 7 days of ingredient usage.
    No ML needed for MVP — just project forward using average daily usage.
    """
    now = get_current_utc()
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)

    # Get this week's and last week's usage per ingredient
    query = text("""
        SELECT
            i.name as ingredient_name,
            COUNT(CASE WHEN o.created_at >= :this_week THEN oi.id END) as this_week_usage,
            COUNT(CASE WHEN o.created_at >= :last_week AND o.created_at < :this_week THEN oi.id END) as last_week_usage
        FROM order_item_ingredients oi
        JOIN order_items it ON oi.order_item_id = it.id
        JOIN orders o ON it.order_id = o.id
        JOIN ingredients i ON oi.ingredient_id = i.id
        WHERE o.created_at >= :last_week
        GROUP BY i.name
        ORDER BY this_week_usage DESC
    """)

    rows = db.execute(query, {
        "this_week": seven_days_ago,
        "last_week": fourteen_days_ago,
    }).fetchall()

    items = []
    tomorrow = now + timedelta(days=1)
    for r in rows:
        name = r[0]
        tw = int(r[1])
        lw = int(r[2])
        avg_daily = tw / 7.0 if tw > 0 else 0

        if lw > 0:
            delta = ((tw - lw) / lw) * 100
        elif tw > 0:
            delta = 100.0
        else:
            delta = 0.0

        if delta > 10:
            direction = "up"
        elif delta < -10:
            direction = "down"
        else:
            direction = "stable"

        # Simple linear projection for tomorrow
        predicted = avg_daily * 1.0  # just use avg as prediction for MVP

        data_points = tw + lw
        if data_points >= 10:
            confidence = "high"
        elif data_points >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        items.append(ForecastItem(
            ingredient_name=name,
            forecast_date=str(tomorrow.date()),
            predicted_usage=round(predicted, 2),
            recent_avg_usage=round(avg_daily, 2),
            trend_direction=direction,
            trend_delta=round(delta, 2),
            baseline_method="7d_moving_avg",
            confidence_level=confidence,
            data_points_used=data_points,
        ))

    return IngredientForecastResponse(
        as_of=now,
        forecast_horizon_days=7,
        items=items
    )
