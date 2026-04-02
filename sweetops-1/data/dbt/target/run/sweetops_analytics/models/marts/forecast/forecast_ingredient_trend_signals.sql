
  
    

  create  table "sweetops_db"."analytics"."forecast_ingredient_trend_signals__dbt_tmp"
  
  
    as
  
  (
    with daily_demand as (
    select
        ingredient_id,
        ingredient_name,
        demand_date,
        total_daily_quantity
    from "sweetops_db"."analytics"."agg_daily_ingredient_demand"
),
date_range as (
    -- Gerekli hesaplamalar için malzemenin güncel veriye sahip olduğu max tarihi (as_of) alıyoruz
    select 
        ingredient_id,
        ingredient_name,
        max(demand_date) as last_demand_date,
        count(demand_date) as data_points_used
    from daily_demand
    group by 1, 2
),
rolling_stats as (
    select
        d.ingredient_id,
        d.ingredient_name,
        r.last_demand_date as as_of_date,
        r.data_points_used,
        
        -- Recent 7 Days (Son 7 Günlük Toplam/Ort)
        sum(case when d.demand_date > r.last_demand_date - interval '7 days' 
            then d.total_daily_quantity else 0 end) as recent_7d_total,
            
        -- Prior 7 Days (Önceki 7 Günlük, yani 8-14 gün arası Toplam/Ort)
        sum(case when d.demand_date > r.last_demand_date - interval '14 days' 
                  and d.demand_date <= r.last_demand_date - interval '7 days'
            then d.total_daily_quantity else 0 end) as prior_7d_total
            
    from date_range r
    left join daily_demand d on r.ingredient_id = d.ingredient_id
    group by 1, 2, 3, 4
)
select
    ingredient_id,
    ingredient_name,
    as_of_date,
    data_points_used,
    
    -- Recent Average (Eğer mevcut data points < 7 ise hepsinin, >=7 ise 7'nin ortalaması)
    (recent_7d_total / case when data_points_used < 7 then greatest(data_points_used, 1) else 7 end) as recent_avg_usage,
    
    -- Prior Average (Karşılaştırma için)
    (prior_7d_total / 7.0) as prior_avg_usage,
    
    -- Confidence Logic
    case 
        when data_points_used >= 14 then 'high'
        when data_points_used >= 7 then 'medium'
        else 'low'
    end as confidence_level,
    
    -- Trend Delta ve Direction
    case 
        when data_points_used >= 14 then 
            ((recent_7d_total / 7.0) - (prior_7d_total / 7.0))
        when data_points_used >= 7 and data_points_used < 14 then 
            -- Eğer prior 7 yoksa genel ortalamayı (veya sadece 0'ı) sinyal ver
            0.0
        else 0.0
    end as trend_delta,
    
    case 
        when data_points_used >= 14 then 
            case 
                when ((recent_7d_total / 7.0) - (prior_7d_total / 7.0)) > 0 then 'up'
                when ((recent_7d_total / 7.0) - (prior_7d_total / 7.0)) < 0 then 'down'
                else 'stable'
            end
        when data_points_used >= 7 and data_points_used < 14 then 'stable'
        else 'stable'
    end as trend_direction,
    
    -- Baseline Yöntemi Etiketi
    case 
        when data_points_used >= 7 then 'rolling_7d_avg'
        else 'available_days_avg'
    end as baseline_method

from rolling_stats
  );
  