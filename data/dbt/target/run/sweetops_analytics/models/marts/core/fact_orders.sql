
  
    

  create  table "sweetops_db"."analytics"."fact_orders__dbt_tmp"
  
  
    as
  
  (
    with latest_event as (
    select
        order_id,
        status_to as current_status,
        created_at as latest_status_at,
        row_number() over (partition by order_id order by created_at desc, event_id desc) as rn
    from "sweetops_db"."analytics"."stg_order_status_events"
)
select
    o.order_id,
    o.store_id,
    o.table_id,
    o.total_amount,
    o.created_at,
    coalesce(le.current_status, o.original_status) as current_status,
    le.latest_status_at
from "sweetops_db"."analytics"."stg_orders" o
left join latest_event le on o.order_id = le.order_id and le.rn = 1
  );
  