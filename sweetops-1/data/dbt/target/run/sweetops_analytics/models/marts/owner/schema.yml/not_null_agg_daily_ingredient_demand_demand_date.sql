select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select demand_date
from "sweetops_db"."analytics"."agg_daily_ingredient_demand"
where demand_date is null



      
    ) dbt_internal_test