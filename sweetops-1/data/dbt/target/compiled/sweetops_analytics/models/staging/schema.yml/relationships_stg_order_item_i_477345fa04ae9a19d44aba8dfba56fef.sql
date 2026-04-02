
    
    

with child as (
    select order_item_id as from_field
    from "sweetops_db"."analytics"."stg_order_item_ingredients"
    where order_item_id is not null
),

parent as (
    select order_item_id as to_field
    from "sweetops_db"."analytics"."stg_order_items"
)

select
    from_field

from child
left join parent
    on child.from_field = parent.to_field

where parent.to_field is null


