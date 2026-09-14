import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .alerts import sweep_vm_offline_alerts
from .config import settings
from .database import SessionLocal
from .metrics import prune_old_metric_samples
from .notifications import send_alert_notification
from .recipients import recipients_for_vm
from .routers import (
    agent, alerts as alerts_router, auth, environments, metrics as metrics_router,
    alert_groups as alert_groups_router, settings as settings_router, users, vms,
)
from .settings_store import smtp_config
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
app.include_router(metrics_router.router)
app.include_router(settings_router.router)
app.include_router(alert_groups_router.router)

OFFLINE_SWEEP_INTERVAL_SECONDS = 60


async def _offline_sweep_loop():
    while True:
        await asyncio.sleep(OFFLINE_SWEEP_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            # Both do blocking DB I/O -- run them off the event loop thread
            # so a slow query never stalls request handling.
            newly_opened = await asyncio.to_thread(sweep_vm_offline_alerts, db)
            await asyncio.to_thread(prune_old_metric_samples, db)
            if newly_opened:
                # Keyed by VM rather than by name: recipients are per-VM now,
                # so the VM itself has to survive the grouping.
                by_vm = {}
                for vm, alert in newly_opened:
                    entry = by_vm.setdefault(vm.id, {"vm": vm, "alerts": []})
                    entry["alerts"].append({
                        "severity": alert.severity, "resource_type": alert.resource_type,
                        "resource_name": alert.resource_name, "message": alert.message,
                    })
                # Resolved here, while the session is open -- the sender runs
                # off-thread with no DB access of its own.
                smtp_cfg = smtp_config(db)
                for entry in by_vm.values():
                    recipients = recipients_for_vm(db, entry["vm"])
                    await asyncio.to_thread(
                        send_alert_notification, entry["vm"].name, entry["alerts"], recipients, smtp_cfg,
                    )
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
