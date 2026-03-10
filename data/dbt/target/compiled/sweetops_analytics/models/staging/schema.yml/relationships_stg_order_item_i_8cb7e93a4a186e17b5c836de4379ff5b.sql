
    
    

with child as (
    select ingredient_id as from_field
    from "sweetops_db"."analytics"."stg_order_item_ingredients"
    where ingredient_id is not null
),

parent as (
    select ingredient_id as to_field
    from "sweetops_db"."analytics"."stg_ingredients"
)

select
    from_field

from child
left join parent
    on child.from_field = parent.to_field

where parent.to_field is null


