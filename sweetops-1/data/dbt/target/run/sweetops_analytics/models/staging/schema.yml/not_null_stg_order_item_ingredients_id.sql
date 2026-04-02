select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select id
from "sweetops_db"."analytics"."stg_order_item_ingredients"
where id is null



      
    ) dbt_internal_test