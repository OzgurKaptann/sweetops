from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import health, public_menu, public_orders, kitchen_orders, owner_analytics, owner_insights, owner_metrics, ws

app = FastAPI(title="SweetOps API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://localhost:3003",
        "http://localhost:3004",
        "http://localhost:3005",
        "http://localhost:3006",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(public_menu.router)
app.include_router(public_orders.router)
app.include_router(kitchen_orders.router)
app.include_router(owner_analytics.router)
app.include_router(owner_insights.router)
app.include_router(owner_metrics.router)
app.include_router(ws.router)

