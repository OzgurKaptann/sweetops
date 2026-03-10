select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select forecast_date
from "sweetops_db"."analytics"."forecast_ingredient_daily_baseline"
where forecast_date is null



      
    ) dbt_internal_test