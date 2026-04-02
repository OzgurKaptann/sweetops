select
    id as order_id,
    store_id,
    table_id,
    status as original_status,
    total_amount,
    created_at
from {{ source('public', 'orders') }}
