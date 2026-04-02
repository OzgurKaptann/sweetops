
  create view "sweetops_db"."analytics"."stg_orders__dbt_tmp"
    
    
  as (
    select
    id as order_id,
    store_id,
    table_id,
    status as original_status,
    total_amount,
    created_at
from "sweetops_db"."public"."orders"
  );