
  create view "sweetops_db"."analytics"."stg_order_item_ingredients__dbt_tmp"
    
    
  as (
    select
    id as order_item_ingredient_id,
    order_item_id,
    ingredient_id,
    quantity,
    price_modifier
from "sweetops_db"."public"."order_item_ingredients"
  );