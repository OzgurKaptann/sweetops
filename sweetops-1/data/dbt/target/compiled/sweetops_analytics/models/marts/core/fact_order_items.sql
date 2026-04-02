select
    i.order_item_id,
    i.order_id,
    i.product_id,
    p.product_name,
    p.product_category,
    i.quantity,
    i.base_price,
    (i.base_price * i.quantity) as item_subtotal
from "sweetops_db"."analytics"."stg_order_items" i
left join "sweetops_db"."analytics"."stg_products" p on i.product_id = p.product_id