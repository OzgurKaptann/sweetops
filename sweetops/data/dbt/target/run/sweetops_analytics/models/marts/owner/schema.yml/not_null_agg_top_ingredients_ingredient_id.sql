select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select ingredient_id
from "sweetops_db"."analytics"."agg_top_ingredients"
where ingredient_id is null



      
    ) dbt_internal_test