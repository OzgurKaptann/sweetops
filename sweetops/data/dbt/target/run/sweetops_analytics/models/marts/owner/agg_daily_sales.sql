
  
    

  create  table "sweetops_db"."analytics"."agg_daily_sales__dbt_tmp"
  
  
    as
  
  (
    select
    date_trunc('day', created_at) as sales_date,
    count(order_id) as total_orders,
    sum(total_amount) as gross_revenue,
    sum(total_amount) / nullif(count(order_id), 0) as average_order_value
from "sweetops_db"."analytics"."fact_orders"
where current_status = 'DELIVERED'
group by 1
  );
  