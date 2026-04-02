select
    ing.order_item_ingredient_id,
    ing.order_item_id,
    ing.ingredient_id,
    d.ingredient_name,
    d.ingredient_category,
    ing.quantity,
    ing.price_modifier,
    (ing.price_modifier * ing.quantity) as ingredient_subtotal
from {{ ref('stg_order_item_ingredients') }} ing
left join {{ ref('stg_ingredients') }} d on ing.ingredient_id = d.ingredient_id
