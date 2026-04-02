select
    id as event_id,
    order_id,
    status_from,
    status_to,
    created_at
from {{ source('public', 'order_status_events') }}
