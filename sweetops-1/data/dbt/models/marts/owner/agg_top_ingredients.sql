select
    ingredient_id,
    ingredient_name,
    ingredient_category,
    sum(quantity) as total_quantity_used,
    count(distinct order_item_id) as total_order_items_included
from {{ ref('fact_order_ingredients') }}
group by 1, 2, 3
