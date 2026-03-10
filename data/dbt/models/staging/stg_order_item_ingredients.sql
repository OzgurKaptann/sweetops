select
    id as order_item_ingredient_id,
    order_item_id,
    ingredient_id,
    quantity,
    price_modifier
from {{ source('public', 'order_item_ingredients') }}
