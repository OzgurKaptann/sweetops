
  create view "sweetops_db"."analytics"."stg_products__dbt_tmp"
    
    
  as (
    select
    id as product_id,
    name as product_name,
    category as product_category,
    base_price
from "sweetops_db"."public"."products"
  );