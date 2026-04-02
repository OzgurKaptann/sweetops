
  create view "sweetops_db"."analytics"."stg_ingredients__dbt_tmp"
    
    
  as (
    select
    id as ingredient_id,
    name as ingredient_name,
    category as ingredient_category,
    price as ingredient_price
from "sweetops_db"."public"."ingredients"
  );