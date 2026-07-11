from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import (
    auth,
    health,
    public_menu,
    public_orders,
    public_qr,
    kitchen_orders,
    cashier,
    inventory,
    owner_analytics,
    owner_insights,
    owner_metrics,
    owner_payments,
    ws,
)

app = FastAPI(title="SweetOps API", version="1.0.0")

# Cookie auth requires an explicit, credentialed allow-list — never "*".
# Staff + public origins come from configuration (STAFF_TRUSTED_ORIGINS /
# PUBLIC_TRUSTED_ORIGINS) so production values are supplied via the environment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.all_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(public_menu.router)
app.include_router(public_qr.router)
app.include_router(public_orders.router)
app.include_router(kitchen_orders.router)
app.include_router(cashier.router)
app.include_router(inventory.router)
app.include_router(owner_analytics.router)
app.include_router(owner_insights.router)
app.include_router(owner_metrics.router)
app.include_router(owner_payments.router)
app.include_router(ws.router)
