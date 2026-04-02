select
      count(*) as failures,
      count(*) != 0 as should_warn,
      count(*) != 0 as should_error
    from (
      
    
    

select
    ingredient_id as unique_field,
    count(*) as n_records

from "sweetops_db"."analytics"."forecast_ingredient_trend_signals"
where ingredient_id is not null
group by ingredient_id
having count(*) > 1



      
    ) dbt_internal_test