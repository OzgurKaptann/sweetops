from pydantic import BaseModel
from typing import List, Optional
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
