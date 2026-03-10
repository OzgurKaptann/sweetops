# Demo Execution Flow

Welcome to SweetOps! If you are reviewing this project for demonstration purposes, follow these guaranteed steps to showcase all capabilities (from Realtime ordering up to AI-like Forecasting).

## 1. Launch the Cluster
Ensure Docker Desktop is running. Start the entire ecosystem:
```bash
docker-compose up -d
```
*Tip: This boots Postgres DB, Redis, the FastAPI backend, and provisions Metabase & dbt containers.*

## 2. Seed Historical Data (The "Magic" Step)
For the Analytics/Forecast tabs to look realistic, you need past data. The historical seeder automatically backfills 14 days of realistic random events, prioritizing weekends and creating trends (like high Nutella usage).
```bash
# Run this from the repository root:
docker-compose run --rm -v "${PWD}/scripts:/scripts" api python /scripts/demo_seed.py
```
*(Wait until you see "Demo Seed Process DB Phase Completed" in the terminal).*

## 3. Generate Analytics Models (dbt)
The backend DB is now packed with raw data. It's time to let `dbt` transform this raw data into the robust models the Owner App depends on.
```bash
docker-compose run --rm dbt dbt run
```

## 4. Run the Frontends
Now let's spin up our Next.js UI suite! Open a new terminal and navigate to the root directory. Install dependencies and run Turborepo:
```bash
npm install
npm run dev
```

## 5. The Demo Experience (What to show)

1. **The Owner's Perspective (Historical View):**
   - Open [http://localhost:3003](http://localhost:3003) (Owner Web)
   - Show off the KPIs, Total Revenue, Top Ingredients Chart.
   - **Forecast Panel:** Navigate to the Predictive tab pointing out how last week's data automatically generated next week's predictions for major ingredients (Waffle Batter, Strawberries).

2. **The KDS Dashboard (Kitchen):**
   - Open [http://localhost:3002](http://localhost:3002) (Kitchen Web)
   - Keep this visible on one side of your screen. Point out the "Live" Green badge ensuring WebSocket connection.
   
3. **The Customer Flow (Action):**
   - Open [http://localhost:3001](http://localhost:3001) (Customer Web) on the other side.
   - Build a Waffle with ingredients. Click **Place Order**.
   - Notice how instantly (without any browser refresh), the Kitchen Screen gets populated!
   - On the Kitchen Web, click **Mark In Prep** and then **Mark Ready**. The card magically vanishes.
