select
    id as ingredient_id,
    name as ingredient_name,
    category as ingredient_category,
    price as ingredient_price
from {{ source('public', 'ingredients') }}
