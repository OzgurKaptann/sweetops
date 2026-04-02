select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select current_status
from "sweetops_db"."analytics"."fact_orders"
where current_status is null



      
    ) dbt_internal_test