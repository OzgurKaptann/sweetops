select
    date_trunc('day', created_at) as sales_date,
    count(order_id) as total_orders,
    sum(total_amount) as gross_revenue,
    sum(total_amount) / nullif(count(order_id), 0) as average_order_value
from {{ ref('fact_orders') }}
where current_status = 'DELIVERED'
group by 1
