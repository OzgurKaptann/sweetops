select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select order_item_ingredient_id
from "sweetops_db"."analytics"."fact_order_ingredients"
where order_item_ingredient_id is null



      
    ) dbt_internal_test