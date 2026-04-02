# Application Programming Interface (API) Overview

SweetOps provides clearly separated REST APIs tailored for different actors (Customers, Kitchen Staff, Owner).

## 1. Public API (Customer)
Endpoints directly accessed by consumers to place orders.

- **GET `/public/menu`**
  - Returns available products and ingredient choices.
  
- **POST `/public/orders`**
  - Creates a new order.
  - Automatically calculates sub-totals and triggers background events for the Kitchen WebSocket.

## 2. Kitchen API (Operations)
Endpoints for the Kitchen Display System (KDS).

- **GET `/kitchen/orders`**
  - Fetches currently active orders (`NEW` and `IN_PREP`).
  - Pre-populates the React dashboard at startup.
  
- **PATCH `/kitchen/orders/{id}/status`**
  - Updates an order to a new state (`IN_PREP`, `READY`).
  - Broadcasts `order_status_updated` via WebSocket to instantly refresh monitors.

- **WS `/ws/kitchen`**
  - **WebSocket** connection for real-time reactivity. Uses ping/pong mechanics and silent re-fetching to guarantee data integrity across devices.

## 3. Owner Analytics API (Business)
Aggregated data powered by our `dbt` background pipelines.

- **GET `/owner/kpis`**
  - Lifetime revenue, total orders delivered, average order value, and peak hours.

- **GET `/owner/top-ingredients`**
  - Raw usage share of ingredients (e.g. Nutella vs Strawberries) to negotiate bulk buys.

- **GET `/owner/hourly-demand`**
  - Distribution of transactions across the 24-hour cycle.

- **GET `/owner/ingredient-forecast`**
  - Fetches the advanced dbt-generated predictive model showcasing estimated upcoming daily usage versus historical averages.
