-- 7 Günlük Horizon Üretimi için basit Series Cross Join
with recursive horizon_days as (
    select 1 as day_offset
    union all
    select day_offset + 1
    from horizon_days
    where day_offset < 7
),
signals as (
    select * from "sweetops_db"."analytics"."forecast_ingredient_trend_signals"
)
select
    s.ingredient_id,
    s.ingredient_name,
    (s.as_of_date + (h.day_offset || ' days')::interval) as forecast_date,
    s.recent_avg_usage as predicted_usage, -- Şimdilik ML olmadığı için Average, tüm horizon'a dağıtılır.
    s.recent_avg_usage,
    s.trend_direction,
    s.trend_delta,
    s.baseline_method,
    s.confidence_level,
    s.data_points_used
from signals s
cross join horizon_days h