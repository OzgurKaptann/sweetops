select
    date_trunc('hour', created_at) as hour_bucket,
    count(order_id) as total_orders
from {{ ref('fact_orders') }}
group by 1
