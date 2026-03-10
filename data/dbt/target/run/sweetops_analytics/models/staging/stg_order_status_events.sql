
  create view "sweetops_db"."analytics"."stg_order_status_events__dbt_tmp"
    
    
  as (
    select
    id as event_id,
    order_id,
    status_from,
    status_to,
    created_at
from "sweetops_db"."public"."order_status_events"
  );