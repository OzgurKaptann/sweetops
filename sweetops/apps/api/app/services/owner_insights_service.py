"""
Owner Insights Service — Value perception features.
Critical alerts, prep time, trending ingredients, popular combos.
"""
from sqlalchemy.orm import Session
from sqlalchemy import func, text, case, literal_column
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.order_status_event import OrderStatusEvent


def fetch_critical_alerts(db: Session):
    """
    Low stock alerts with estimated lost revenue.
    If an ingredient runs out, how much ₺/day does the owner lose?
    """
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # Get daily avg usage and revenue per ingredient over last 7 days
    daily_stats = db.execute(text("""
        SELECT
            oi.ingredient_id,
            i.name,
            i.category,
            i.unit,
            COALESCE(s.stock_quantity, 0) as stock_qty,
            COALESCE(s.reorder_level, 0) as reorder_lvl,
            COUNT(DISTINCT DATE(o.created_at)) as active_days,
            COUNT(oi.id) as total_selections,
            COALESCE(SUM(oi.price_modifier), 0) as total_revenue
        FROM order_item_ingredients oi
        JOIN order_items it ON oi.order_item_id = it.id
        JOIN orders o ON it.order_id = o.id
        JOIN ingredients i ON oi.ingredient_id = i.id
        LEFT JOIN ingredient_stock s ON s.ingredient_id = i.id
        WHERE o.created_at >= :since
          AND o.status IN ('DELIVERED', 'READY', 'IN_PREP', 'NEW')
        GROUP BY oi.ingredient_id, i.name, i.category, i.unit, s.stock_quantity, s.reorder_level
        ORDER BY stock_qty ASC
    """), {"since": seven_days_ago}).fetchall()

    alerts = []
    for row in daily_stats:
        ingredient_id = row[0]
        name = row[1]
        category = row[2]
        unit = row[3]
        stock_qty = float(row[4])
        reorder_lvl = float(row[5])
        active_days = max(int(row[6]), 1)
        total_selections = int(row[7])
        total_revenue = float(row[8])

        avg_daily_selections = total_selections / active_days
        avg_daily_revenue = total_revenue / active_days

        # Calculate days remaining
        if avg_daily_selections > 0:
            # Estimate consumed per selection using standard_quantity
            ing = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
            std_qty = float(ing.standard_quantity) if ing and ing.standard_quantity else 0
            avg_daily_consumed = avg_daily_selections * std_qty
            days_remaining = stock_qty / avg_daily_consumed if avg_daily_consumed > 0 else 999
        else:
            avg_daily_consumed = 0
            days_remaining = 999

        # Determine severity
        if stock_qty <= 0:
            severity = "critical"
            message = "Stok tükendi!"
            lost_revenue_daily = avg_daily_revenue
        elif days_remaining < 2:
            severity = "critical"
            message = f"Stok kritik — {days_remaining:.1f} gün kaldı"
            lost_revenue_daily = avg_daily_revenue
        elif days_remaining < 3:
            severity = "warning"
            message = f"Stok azalıyor — {days_remaining:.1f} gün kaldı"
            lost_revenue_daily = avg_daily_revenue * 0.5  # partial risk
        else:
            continue  # No alert needed

        alerts.append({
            "ingredient_id": ingredient_id,
            "ingredient_name": name,
            "category": category,
            "unit": unit,
            "stock_quantity": stock_qty,
            "days_remaining": round(days_remaining, 1),
            "severity": severity,
            "message": message,
            "avg_daily_revenue": round(avg_daily_revenue, 2),
            "estimated_lost_revenue_daily": round(lost_revenue_daily, 2),
            "avg_daily_selections": round(avg_daily_selections, 1),
        })

    total_daily_risk = sum(a["estimated_lost_revenue_daily"] for a in alerts)

    return {
        "alerts": alerts,
        "total_daily_risk": round(total_daily_risk, 2),
        "total_alerts": len(alerts),
    }


def fetch_prep_time_stats(db: Session):
    """
    Calculate average prep time from IN_PREP → READY status transitions.
    """
    # Get pairs of IN_PREP and READY events for the same order
    prep_events = db.execute(text("""
        SELECT
            e1.order_id,
            e1.created_at as prep_start,
            e2.created_at as prep_end,
            EXTRACT(EPOCH FROM (e2.created_at - e1.created_at)) as prep_seconds
        FROM order_status_events e1
        JOIN order_status_events e2 ON e1.order_id = e2.order_id
        WHERE e1.status_to = 'IN_PREP'
          AND e2.status_to = 'READY'
          AND e2.created_at > e1.created_at
        ORDER BY e2.created_at DESC
        LIMIT 50
    """)).fetchall()

    if not prep_events:
        return {
            "avg_prep_seconds": None,
            "avg_prep_display": "—",
            "fastest_seconds": None,
            "slowest_seconds": None,
            "total_tracked": 0,
            "recent_orders": [],
        }

    times = [float(row[3]) for row in prep_events]
    avg_secs = sum(times) / len(times)
    fastest = min(times)
    slowest = max(times)

    def format_time(secs):
        mins = int(secs // 60)
        remainder = int(secs % 60)
        if mins > 0:
            return f"{mins}dk {remainder}sn"
        return f"{remainder}sn"

    recent = []
    for row in prep_events[:10]:
        secs = float(row[3])
        recent.append({
            "order_id": row[0],
            "prep_seconds": round(secs),
            "prep_display": format_time(secs),
            "completed_at": row[2].isoformat() if row[2] else None,
        })

    return {
        "avg_prep_seconds": round(avg_secs),
        "avg_prep_display": format_time(avg_secs),
        "fastest_seconds": round(fastest),
        "fastest_display": format_time(fastest),
        "slowest_seconds": round(slowest),
        "slowest_display": format_time(slowest),
        "total_tracked": len(times),
        "recent_orders": recent,
    }


def fetch_trending_ingredients(db: Session):
    """
    Compare this week vs last week ingredient usage.
    """
    now = datetime.now(timezone.utc)
    this_week_start = now - timedelta(days=7)
    last_week_start = now - timedelta(days=14)

    def usage_in_range(start, end):
        rows = db.execute(text("""
            SELECT
                oi.ingredient_id,
                i.name,
                i.category,
                COUNT(oi.id) as selections
            FROM order_item_ingredients oi
            JOIN order_items it ON oi.order_item_id = it.id
            JOIN orders o ON it.order_id = o.id
            JOIN ingredients i ON oi.ingredient_id = i.id
            WHERE o.created_at >= :start AND o.created_at < :end
            GROUP BY oi.ingredient_id, i.name, i.category
        """), {"start": start, "end": end}).fetchall()
        return {row[0]: {"name": row[1], "category": row[2], "count": int(row[3])} for row in rows}

    this_week = usage_in_range(this_week_start, now)
    last_week = usage_in_range(last_week_start, this_week_start)

    # All ingredient IDs across both weeks
    all_ids = set(this_week.keys()) | set(last_week.keys())

    trends = []
    for ing_id in all_ids:
        tw = this_week.get(ing_id, {"name": "", "category": "", "count": 0})
        lw = last_week.get(ing_id, {"name": "", "category": "", "count": 0})

        name = tw["name"] or lw["name"]
        category = tw["category"] or lw["category"]
        tw_count = tw["count"]
        lw_count = lw["count"]

        if lw_count > 0:
            change_pct = ((tw_count - lw_count) / lw_count) * 100
        elif tw_count > 0:
            change_pct = 100.0  # new ingredient, 100% increase
        else:
            change_pct = 0.0

        if change_pct > 10:
            direction = "up"
        elif change_pct < -10:
            direction = "down"
        else:
            direction = "stable"

        trends.append({
            "ingredient_id": ing_id,
            "ingredient_name": name,
            "category": category,
            "this_week": tw_count,
            "last_week": lw_count,
            "change_pct": round(change_pct, 1),
            "direction": direction,
        })

    # Sort by absolute change, most notable first
    trends.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

    rising = [t for t in trends if t["direction"] == "up"]
    falling = [t for t in trends if t["direction"] == "down"]

    return {
        "trends": trends,
        "rising_count": len(rising),
        "falling_count": len(falling),
        "top_rising": rising[:5],
        "top_falling": falling[:5],
    }


def fetch_popular_combinations(db: Session, limit: int = 5):
    """
    Find which ingredient pairs are most commonly ordered together.
    """
    # Self-join order_item_ingredients to find co-occurring pairs within the same order_item
    combos = db.execute(text("""
        SELECT
            i1.name as ing1_name,
            i2.name as ing2_name,
            COUNT(*) as combo_count
        FROM order_item_ingredients oi1
        JOIN order_item_ingredients oi2
            ON oi1.order_item_id = oi2.order_item_id
            AND oi1.ingredient_id < oi2.ingredient_id
        JOIN ingredients i1 ON oi1.ingredient_id = i1.id
        JOIN ingredients i2 ON oi2.ingredient_id = i2.id
        GROUP BY i1.name, i2.name
        HAVING COUNT(*) >= 2
        ORDER BY combo_count DESC
        LIMIT :limit
    """), {"limit": limit}).fetchall()

    pairs = []
    for row in combos:
        pairs.append({
            "ingredient_1": row[0],
            "ingredient_2": row[1],
            "count": int(row[2]),
            "display": f"{row[0]} + {row[1]}",
        })

    # Also find the single most popular set of 3
    triples = db.execute(text("""
        SELECT
            i1.name, i2.name, i3.name,
            COUNT(*) as combo_count
        FROM order_item_ingredients oi1
        JOIN order_item_ingredients oi2
            ON oi1.order_item_id = oi2.order_item_id AND oi1.ingredient_id < oi2.ingredient_id
        JOIN order_item_ingredients oi3
            ON oi1.order_item_id = oi3.order_item_id AND oi2.ingredient_id < oi3.ingredient_id
        JOIN ingredients i1 ON oi1.ingredient_id = i1.id
        JOIN ingredients i2 ON oi2.ingredient_id = i2.id
        JOIN ingredients i3 ON oi3.ingredient_id = i3.id
        GROUP BY i1.name, i2.name, i3.name
        HAVING COUNT(*) >= 2
        ORDER BY combo_count DESC
        LIMIT 3
    """)).fetchall()

    top_triples = []
    for row in triples:
        top_triples.append({
            "ingredients": [row[0], row[1], row[2]],
            "count": int(row[3]),
            "display": f"{row[0]} + {row[1]} + {row[2]}",
        })

    return {
        "top_pairs": pairs,
        "top_triples": top_triples,
    }


def fetch_value_summary(db: Session):
    """
    One-screen value proof for the owner.
    Shows ₺ protected, ₺ at risk, and top insights.
    """
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)

    # === Revenue this week ===
    rev_result = db.execute(text("""
        SELECT
            COUNT(id) as orders,
            COALESCE(SUM(total_amount), 0) as revenue
        FROM orders
        WHERE created_at >= :since
          AND status IN ('DELIVERED', 'READY', 'IN_PREP', 'NEW')
    """), {"since": seven_days_ago}).fetchone()

    this_week_orders = int(rev_result[0]) if rev_result else 0
    this_week_revenue = float(rev_result[1]) if rev_result else 0

    # === Revenue last week (for comparison) ===
    last_rev = db.execute(text("""
        SELECT COALESCE(SUM(total_amount), 0)
        FROM orders
        WHERE created_at >= :start AND created_at < :end
          AND status IN ('DELIVERED', 'READY', 'IN_PREP', 'NEW')
    """), {"start": fourteen_days_ago, "end": seven_days_ago}).fetchone()

    last_week_revenue = float(last_rev[0]) if last_rev else 0

    revenue_change_pct = 0
    if last_week_revenue > 0:
        revenue_change_pct = ((this_week_revenue - last_week_revenue) / last_week_revenue) * 100

    # === Stock risk (₺ at risk per day) ===
    alerts_data = fetch_critical_alerts(db)
    daily_risk = alerts_data["total_daily_risk"]
    weekly_risk = daily_risk * 7

    # === Stockout prevention value ===
    # If the system prevented even 1 stockout per week, estimate saved revenue
    # Use avg daily revenue of the most popular ingredient as baseline
    prevented_estimate = daily_risk * 2  # "2 days of risk avoided this week"

    # === Prep time ===
    prep_data = fetch_prep_time_stats(db)
    avg_prep = prep_data.get("avg_prep_display", "—")

    # === Top trending ===
    trend_data = fetch_trending_ingredients(db)
    top_rising_name = trend_data["top_rising"][0]["ingredient_name"] if trend_data["top_rising"] else None
    top_rising_pct = trend_data["top_rising"][0]["change_pct"] if trend_data["top_rising"] else 0

    # === Top combo ===
    combo_data = fetch_popular_combinations(db, limit=1)
    top_combo = combo_data["top_pairs"][0]["display"] if combo_data["top_pairs"] else None

    # === Build value items ===
    value_items = []

    if this_week_revenue > 0:
        value_items.append({
            "icon": "💰",
            "label": "Bu haftaki geliriniz",
            "value": f"₺{this_week_revenue:,.0f}",
            "detail": f"{this_week_orders} sipariş",
            "color": "green",
        })

    if revenue_change_pct != 0:
        direction = "↑" if revenue_change_pct > 0 else "↓"
        value_items.append({
            "icon": "📊",
            "label": "Geçen haftaya göre",
            "value": f"{direction} %{abs(revenue_change_pct):.0f}",
            "detail": f"Geçen hafta: ₺{last_week_revenue:,.0f}",
            "color": "green" if revenue_change_pct > 0 else "red",
        })

    if daily_risk > 0:
        value_items.append({
            "icon": "🚨",
            "label": "Stok tükenme riski",
            "value": f"₺{weekly_risk:,.0f}/hafta",
            "detail": f"Günlük ₺{daily_risk:,.0f} kayıp riski",
            "color": "red",
        })

    if prevented_estimate > 0:
        value_items.append({
            "icon": "🛡️",
            "label": "SweetOps ile korunan gelir",
            "value": f"₺{prevented_estimate:,.0f}",
            "detail": "Stok uyarıları sayesinde",
            "color": "green",
        })

    if avg_prep != "—":
        value_items.append({
            "icon": "⏱️",
            "label": "Ort. hazırlık süresi",
            "value": avg_prep,
            "detail": f"{prep_data.get('total_tracked', 0)} sipariş ölçüldü",
            "color": "blue",
        })

    if top_rising_name:
        value_items.append({
            "icon": "🔥",
            "label": "Yükselen malzeme",
            "value": top_rising_name,
            "detail": f"+{top_rising_pct:.0f}% bu hafta",
            "color": "orange",
        })

    if top_combo:
        value_items.append({
            "icon": "🤝",
            "label": "En popüler kombinasyon",
            "value": top_combo,
            "detail": "Müşteri favorisi",
            "color": "purple",
        })

    # === Headline ===
    if daily_risk > 0:
        headline = f"SweetOps bu hafta ₺{prevented_estimate:,.0f} gelir korumanıza yardımcı oldu"
    elif this_week_revenue > 0:
        headline = f"Bu hafta ₺{this_week_revenue:,.0f} gelir elde ettiniz"
    else:
        headline = "SweetOps ile işletmenizi takip edin"

    return {
        "headline": headline,
        "items": value_items,
        "weekly_revenue": round(this_week_revenue, 2),
        "weekly_risk": round(weekly_risk, 2),
        "protected_revenue": round(prevented_estimate, 2),
    }

