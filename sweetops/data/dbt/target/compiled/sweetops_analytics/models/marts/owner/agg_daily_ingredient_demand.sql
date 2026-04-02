select
    i.ingredient_id,
    i.ingredient_name,
    date_trunc('day', o.created_at) as demand_date,
    sum(i.quantity) as total_daily_quantity
from "sweetops_db"."analytics"."fact_order_ingredients" i
join "sweetops_db"."analytics"."fact_order_items" oi on i.order_item_id = oi.order_item_id
join "sweetops_db"."analytics"."fact_orders" o on oi.order_id = o.order_id
where o.current_status != 'CANCELLED'
group by 1, 2, 3