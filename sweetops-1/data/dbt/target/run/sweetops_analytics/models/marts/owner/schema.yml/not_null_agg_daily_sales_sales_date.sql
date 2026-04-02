select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    



select sales_date
from "sweetops_db"."analytics"."agg_daily_sales"
where sales_date is null



      
    ) dbt_internal_test