from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timezone
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
    # Fetch from agg_daily_sales (Revenue, AOV, Total Delivered)
    sales_query = text("""
        SELECT 
            COALESCE(SUM(total_orders), 0) as total_delivered,
            COALESCE(SUM(gross_revenue), 0) as gross_revenue,
            COALESCE(SUM(gross_revenue) / NULLIF(SUM(total_orders), 0), 0) as aov
        FROM analytics.agg_daily_sales
    """)
    sales_res = db.execute(sales_query).fetchone()
    
    # Fetch from fact_orders (Active vs Total lifetime orders)
    orders_query = text("""
        SELECT 
            COUNT(order_id) as total_lifetime_orders,
            COUNT(CASE WHEN current_status IN ('NEW', 'IN_PREP', 'READY') THEN 1 END) as active_orders
        FROM analytics.fact_orders
    """)
    orders_res = db.execute(orders_query).fetchone()

    # Fetch peak hour from agg_hourly_orders
    peak_query = text("""
        SELECT to_char(hour_bucket, 'HH24:00') as peak_hour
        FROM analytics.agg_hourly_orders
        ORDER BY total_orders DESC
        LIMIT 1
    """)
    peak_res = db.execute(peak_query).fetchone()

    kpi_data = KPIsData(
        total_orders=orders_res[0] if orders_res else 0, # Total lifetime orders across states
        gross_revenue=float(sales_res[1]) if sales_res else 0.0,
        average_order_value=float(sales_res[2]) if sales_res else 0.0,
        active_orders_count=orders_res[1] if orders_res else 0,
        delivered_orders_count=sales_res[0] if sales_res else 0,
        peak_hour=peak_res[0] if peak_res and peak_res[0] else None
    )

    return KPIsResponse(
        as_of=get_current_utc(),
        currency="USD",
        kpis=kpi_data
    )

def fetch_top_ingredients(db: Session, limit: int = 5) -> TopIngredientsResponse:
    query = text("""
        WITH total_usage AS (
            SELECT COALESCE(SUM(total_quantity_used), 0) as grand_total
            FROM analytics.agg_top_ingredients
        )
        SELECT 
            ingredient_name,
            total_quantity_used,
            CASE 
                WHEN (SELECT grand_total FROM total_usage) > 0 THEN 
                    ROUND((total_quantity_used * 1.0) / (SELECT grand_total FROM total_usage), 4)
                ELSE 0.0 
            END as usage_share
        FROM analytics.agg_top_ingredients
        ORDER BY total_quantity_used DESC
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
    # Gets last 24h of data (or all available for MVP)
    query = text("""
        SELECT to_char(hour_bucket, 'HH24:00') as hour_str, total_orders
        FROM analytics.agg_hourly_orders
        ORDER BY hour_bucket ASC
    """)
    
    rows = db.execute(query).fetchall()
    points = [HourlyDemandPoint(hour_bucket=r[0], order_count=int(r[1])) for r in rows]

    return HourlyDemandResponse(as_of=get_current_utc(), points=points)

def fetch_daily_sales(db: Session) -> DailySalesResponse:
    query = text("""
        SELECT 
            to_char(sales_date, 'YYYY-MM-DD') as date_str,
            total_orders,
            gross_revenue,
            average_order_value
        FROM analytics.agg_daily_sales
        ORDER BY sales_date ASC
    """)
    
    rows = db.execute(query).fetchall()
    points = [
        DailySalesPoint(
            date=r[0],
            total_orders=int(r[1]),
            gross_revenue=float(r[2]),
            average_order_value=float(r[3])
        ) for r in rows
    ]

    return DailySalesResponse(as_of=get_current_utc(), points=points)

def fetch_ingredient_forecast(db: Session) -> IngredientForecastResponse:
    query = text("""
        SELECT 
            ingredient_name,
            to_char(forecast_date, 'YYYY-MM-DD') as f_date,
            ROUND(predicted_usage::numeric, 2) as predicted_usage,
            ROUND(recent_avg_usage::numeric, 2) as recent_avg_usage,
            trend_direction,
            ROUND(trend_delta::numeric, 2) as trend_delta,
            baseline_method,
            confidence_level,
            data_points_used
        FROM analytics.forecast_ingredient_daily_baseline
        ORDER BY ingredient_name ASC, forecast_date ASC
    """)
    
    rows = db.execute(query).fetchall()
    
    items = []
    for r in rows:
        items.append(ForecastItem(
            ingredient_name=r[0],
            forecast_date=r[1],
            predicted_usage=float(r[2]),
            recent_avg_usage=float(r[3]),
            trend_direction=r[4],
            trend_delta=float(r[5]),
            baseline_method=r[6],
            confidence_level=r[7],
            data_points_used=int(r[8])
        ))

    return IngredientForecastResponse(
        as_of=get_current_utc(),
        forecast_horizon_days=7,
        items=items
    )
