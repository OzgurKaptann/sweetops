
  
    

  create  table "sweetops_db"."analytics"."agg_hourly_orders__dbt_tmp"
  
  
    as
  
  (
    select
    date_trunc('hour', created_at) as hour_bucket,
    count(order_id) as total_orders
from "sweetops_db"."analytics"."fact_orders"
group by 1
  );
  