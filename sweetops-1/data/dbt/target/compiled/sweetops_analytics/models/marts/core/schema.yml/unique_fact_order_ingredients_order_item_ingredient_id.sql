
    
    

select
    order_item_ingredient_id as unique_field,
    count(*) as n_records

from "sweetops_db"."analytics"."fact_order_ingredients"
where order_item_ingredient_id is not null
group by order_item_ingredient_id
having count(*) > 1


