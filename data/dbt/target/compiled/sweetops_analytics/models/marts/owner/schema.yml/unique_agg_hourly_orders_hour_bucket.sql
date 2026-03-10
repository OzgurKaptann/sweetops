
    
    

select
    hour_bucket as unique_field,
    count(*) as n_records

from "sweetops_db"."analytics"."agg_hourly_orders"
where hour_bucket is not null
group by hour_bucket
having count(*) > 1


