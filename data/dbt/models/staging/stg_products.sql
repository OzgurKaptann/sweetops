select
    id as product_id,
    name as product_name,
    category as product_category,
    base_price
from {{ source('public', 'products') }}
