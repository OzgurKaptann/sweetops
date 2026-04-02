
  create view "sweetops_db"."analytics"."stg_order_items__dbt_tmp"
    
    
  as (
    select
    id as order_item_id,
    order_id,
    product_id,
    quantity,
    price as base_price
from "sweetops_db"."public"."order_items"
  );