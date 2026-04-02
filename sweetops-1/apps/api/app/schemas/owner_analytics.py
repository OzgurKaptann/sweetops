from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from datetime import datetime
from decimal import Decimal

# Base Metadata
class AnalyticsBase(BaseModel):
    as_of: datetime
    currency: str = "USD"

# 1. GET /owner/kpis
class KPIsData(BaseModel):
    total_orders: int
    gross_revenue: float
    average_order_value: float
    active_orders_count: int
    delivered_orders_count: int
    peak_hour: Optional[str]

class KPIsResponse(AnalyticsBase):
    kpis: KPIsData

# 2. GET /owner/top-ingredients
class TopIngredientItem(BaseModel):
    rank: int
    ingredient_name: str
    usage_count: int
    usage_share: float

class TopIngredientsResponse(AnalyticsBase):
    items: List[TopIngredientItem]

# 3. GET /owner/hourly-demand
class HourlyDemandPoint(BaseModel):
    hour_bucket: str
    order_count: int

class HourlyDemandResponse(AnalyticsBase):
    points: List[HourlyDemandPoint]

# 4. GET /owner/daily-sales
class DailySalesPoint(BaseModel):
    sales_date: str
    total_orders: int
    gross_revenue: float
    average_order_value: float

class DailySalesResponse(AnalyticsBase):
    sales: List[DailySalesPoint]

# 5. GET /owner/ingredient-forecast
class ForecastItem(BaseModel):
    ingredient_name: str
    forecast_date: str
    predicted_usage: float
    recent_avg_usage: float
    trend_direction: str
    trend_delta: float
    baseline_method: str
    confidence_level: str
    data_points_used: int

class IngredientForecastResponse(AnalyticsBase):
    forecast_horizon_days: int = 7
    items: List[ForecastItem]


# 6. GET /owner/decisions/ + PATCH /owner/decisions/{id}
class DecisionSummary(BaseModel):
    high: int
    medium: int
    low: int

class OwnerDecision(BaseModel):
    id: str
    type: str                        # stock_risk | demand_spike | slow_moving | sla_risk | revenue_anomaly
    severity: str                    # high | medium | low
    # Prioritization (additive — new fields)
    decision_score: float
    blocking_vs_non_blocking: bool
    # Human-readable payload
    title: str
    description: str
    impact: str
    recommended_action: str
    why_now: str
    expected_impact: str
    data: Dict[str, Any] = {}
    # Lifecycle (additive — new fields)
    status: str = "pending"          # pending | acknowledged | completed | dismissed
    acknowledged_at: Optional[str] = None
    completed_at: Optional[str] = None
    actor_id: Optional[str] = None
    resolution_note: Optional[str] = None
    # Outcome tracking
    resolution_quality: Optional[str] = None       # good | partial | failed
    estimated_revenue_saved: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class OwnerDecisionsResponse(BaseModel):
    decisions: List[OwnerDecision]
    generated_at: str
    signals_evaluated: int
    active_count: int
    summary: DecisionSummary

class DecisionActionRequest(BaseModel):
    action: str                                          # acknowledge | complete | dismiss
    actor_id: Optional[str] = None
    resolution_note: Optional[str] = None
    resolution_quality: Optional[str] = None             # good | partial | failed
    estimated_revenue_saved: Optional[float] = None      # ₺ saved by acting
