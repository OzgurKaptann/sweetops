# SweetOps MVP Scope

## Included in MVP
- **Customer Context**: QR code resolution for basic store and table context.
- **Menu & Customization**: Categories, product browsing, dynamic waffle building with business rules (min/max topping selections, premium pricing, ingredient limits).
- **Checkout Process**: Simplified cart functionality, order generation, and live order tracking screen.
- **Kitchen Flow**: Order state transitions (`NEW` -> `IN_PREP` -> `READY` -> `DELIVERED`). Clear layout designed for high-stress reading.
- **Owner Analytics**: Revenue metrics, top combos, hourly demand curves, average prep times.
- **Forecasting**: Baseline models (moving average, same day last week) for short-term ingredient demand projection.
- **Infrastructure**: Monorepo scaffolding, PostgreSQL DB (normalized format), and Docker setup for deployment.

## Excluded from MVP
- Loyalty programs & user (customer) accounts.
- ML-driven complex AI forecasting (e.g. Prophet, LightGBM) to avoid fake claims without sufficient historical data.
- Complex multitenant multi-store global SaaS abstractions (keeping it simple for pilot implementations).
- Native iOS/Android apps.
- Heavy event streaming infrastructure (Kafka/RabbitMQ) - falling back to WebSockets and Postgres.
