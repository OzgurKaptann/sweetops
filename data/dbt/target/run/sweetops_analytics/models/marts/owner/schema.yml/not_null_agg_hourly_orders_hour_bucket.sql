select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select hour_bucket
from "sweetops_db"."analytics"."agg_hourly_orders"
where hour_bucket is null



      
    ) dbt_internal_test