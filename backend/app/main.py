import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .alerts import sweep_vm_offline_alerts
from .config import settings
from .database import SessionLocal
from .routers import agent, alerts as alerts_router, auth, environments, users, vms
from .seed import seed

# Schema is owned by Alembic (see alembic/), applied via `alembic upgrade
# head` in the container entrypoint before this app starts — not by
# create_all(), which can only add new tables and silently never alters
# existing ones.
app = FastAPI(title="InfraWatch API")

# CORS_ORIGINS="*" (default, local dev) or a comma-separated list of real origins in production.
cors_origins = ["*"] if settings.cors_origins.strip() == "*" else [o.strip() for o in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(vms.router)
app.include_router(users.router)
app.include_router(environments.router)
app.include_router(agent.router)
app.include_router(alerts_router.router)

OFFLINE_SWEEP_INTERVAL_SECONDS = 60


async def _offline_sweep_loop():
    while True:
        await asyncio.sleep(OFFLINE_SWEEP_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            # sweep_vm_offline_alerts does blocking DB I/O -- run it off the
            # event loop thread so a slow query never stalls request handling.
            await asyncio.to_thread(sweep_vm_offline_alerts, db)
        except Exception:
            pass
        finally:
            db.close()


@app.on_event("startup")
async def on_startup():
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    asyncio.create_task(_offline_sweep_loop())


@app.get("/health")
def health():
    return {"status": "ok"}
