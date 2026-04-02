# System Architecture Overview

SweetOps is a modern, event-driven, full-stack micro-operations platform designed to handle real-time restaurant/kitchen operations while simultaneously accumulating data for deep, historical business analytics (via dbt).

## High-Level Architecture

```mermaid
flowchart TD
    %% Actors
    Cust([Customer])
    Chef([Kitchen Staff])
    Own([Business Owner])

    %% Frontends
    subgraph UI [Micro Frontends (Next.js)]
        CW(Customer Web\n:3001)
        KW(Kitchen Web KDS\n:3002)
        OW(Owner Dashboard\n:3003)
    end

    %% Backend
    subgraph BE [Backend (FastAPI)]
        API[REST API\nOrder / Analytics]
        WS[WebSocket Manager\nKitchen Events]
    end

    %% Data Layer
    subgraph DB [Data Layer (PostgreSQL & dbt)]
        PG[(PostgreSQL\nTransactional)]
        DBT((dbt Core\nTransformations))
        WH[(PostgreSQL\nAnalytics Schema)]
        
        PG -->|Raw Data| DBT
        DBT -->|Aggregations & Forecasts| WH
    end

    %% Cache
    RD[(Redis Cache)]

    %% Connections
    Cust -->|Places Order| CW
    CW -->|POST /public/orders| API
    
    API -->|Write| PG
    API -->|Publish| RD
    RD -->|Subscribe| WS
    API -->|Async Broadcast| WS
    
    WS -.->|order_created| KW
    Chef -->|Updates Status| KW
    KW -->|PATCH /kitchen/orders| API
    WS -.->|order_status_updated| KW
    
    Own -->|View KPIs & Forecast| OW
    OW -->|GET /owner/*| API
    API -->|Read| WH
```

The architecture is built on a "Monolithic API + Micro Frontends" conceptual model, running under a Dockerized Monorepo.

1. **Frontend Layer (Next.js / React)**
   - **Customer Web:** A mobile-first ordering interface for dining-in customers.
   - **Kitchen Web (KDS):** A real-time kitchen display system listening to WebSockets.
   - **Owner Web:** A dashboard fetching complex unified analytics and predictive algorithms.

2. **Backend API (FastAPI)**
   - Exposes REST endpoints for transactions.
   - Runs a native asynchronous WebSocket server for the KDS.
   - Central hub connected to PostgreSQL and Redis.

3. **Data & Analytics Layer (dbt & PostgreSQL)**
   - All transactions are saved in a normalized schema.
   - **dbt Core** transforms raw operational tables (`orders`, `order_items`, `order_status_events`) into aggregated views and trend analysis tables (e.g. `agg_daily_ingredient_demand`, `forecast_ingredient_trend_signals`).
   - Forecast models calculate 7-day rolling averages and predict the next 7 days of ingredient demand to help the business owner balance inventory.

## Technology Stack

- **API:** Python 3.12, FastAPI, SQLAlchemy, Pydantic, WebSockets
- **Database:** PostgreSQL 16
- **Cache/Events:** Redis 7 (Prepared for scale-out Pub/Sub)
- **Analytics:** dbt-postgres
- **Frontends:** Next.js 14 (App Router), Tailwind CSS, TypeScript
- **Infrastructure:** Docker Compose

## Monorepo Layout

```text
SweetOps/
├── apps/
│   ├── api/             # FastAPI backend (Models, Routers, Services)
│   ├── customer-web/    # Next.js mobile ordering web app
│   ├── kitchen-web/     # Next.js Kitchen Display System
│   └── owner-web/       # Next.js Owner KPI Dashboard
├── packages/
│   ├── types/           # Shared TypeScript interfaces (Contract-first)
│   ├── ui/              # Shared React UI components (Tailwind)
│   └── config-tailwind/ # Shared styling rules
├── data/
│   └── dbt/             # dbt models and SQL transformations
├── scripts/
│   └── demo_seed.py     # Python script to generate historical demo data
└── docker-compose.yml   # Multi-container orchestration
```
