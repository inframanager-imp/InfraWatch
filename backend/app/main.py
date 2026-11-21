import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError

from .alerts import sweep_vm_offline_alerts
from .checker import prune_url_samples, run_due_checks
from .config import settings
from .database import SessionLocal
from .logfolders import ScanUnavailable, apply_scan, folders_for_sync, scan_folder
from .streams import agent_sockets
from .metrics import prune_old_metric_samples
from .notifications import alert_summary, send_alert_notification
from .recipients import recipients_for_vm
from .routers import (
    agent, alerts as alerts_router, auth, environments, metrics as metrics_router,
    alert_groups as alert_groups_router, aws_logs as aws_logs_router,
    s3 as s3_router, search as search_router, settings as settings_router,
    url_monitors as url_monitors_router, users, vms,
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
app.include_router(url_monitors_router.router)
app.include_router(aws_logs_router.router)
app.include_router(s3_router.router)
app.include_router(search_router.router)

OFFLINE_SWEEP_INTERVAL_SECONDS = 60
# A log that rolls over at midnight means the file anyone wants in the
# morning is one nobody has registered. Ten minutes is far more often than
# that, and a scan is one readdir on a VM that is already connected.
FOLDER_SYNC_INTERVAL_SECONDS = 600
# Monitors carry their own interval; this is just how often the loop looks for
# ones that are due, so the shortest interval it can honour is this long.
URL_CHECK_TICK_SECONDS = 15


async def _offline_sweep_loop():
    while True:
        await asyncio.sleep(OFFLINE_SWEEP_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            # Both do blocking DB I/O -- run them off the event loop thread
            # so a slow query never stalls request handling.
            changes = await asyncio.to_thread(sweep_vm_offline_alerts, db)
            await asyncio.to_thread(prune_old_metric_samples, db)
            await asyncio.to_thread(prune_url_samples, db)
            if changes:
                # Keyed by VM rather than by name: recipients are per-VM, so
                # the VM itself has to survive the grouping.
                by_vm = {}
                for vm, event, alert in changes:
                    entry = by_vm.setdefault(vm.id, {"vm": vm, "opened": [], "resolved": []})
                    entry[event].append(alert_summary(alert))
                # Resolved here, while the session is open -- the sender runs
                # off-thread with no DB access of its own.
                smtp_cfg = smtp_config(db)
                for entry in by_vm.values():
                    recipients = recipients_for_vm(db, entry["vm"])
                    await asyncio.to_thread(
                        send_alert_notification, entry["vm"].name, entry["opened"],
                        recipients, smtp_cfg, entry["resolved"],
                    )
        except Exception:
            pass
        finally:
            db.close()


async def _url_check_loop():
    while True:
        await asyncio.sleep(URL_CHECK_TICK_SECONDS)
        db = SessionLocal()
        try:
            # Blocking HTTP and DB work, kept off the event loop thread so a
            # slow endpoint cannot stall request handling.
            changes = await asyncio.to_thread(run_due_checks, db)
            if changes:
                smtp_cfg = smtp_config(db)
                for monitor, opened, resolved in changes:
                    # A standalone monitor has no VM to name or to route by,
                    # so the subject carries the monitor itself and the mail
                    # falls back to the admins.
                    recipients = recipients_for_vm(db, monitor.vm)
                    await asyncio.to_thread(
                        send_alert_notification,
                        monitor.vm.name if monitor.vm else monitor.name,
                        [alert_summary(a) for a in opened],
                        recipients, smtp_cfg,
                        [alert_summary(a) for a in resolved],
                    )
        except Exception:
            pass
        finally:
            db.close()


async def _folder_sync_loop():
    """Keeps each folder entry's file rows current without anyone pressing
    anything.

    A VM whose agent is not connected is skipped rather than emptied: its
    files have not gone anywhere, we simply cannot see them, and deleting
    the rows would lose the ability to read yesterday's log from a VM that
    is merely offline.
    """
    while True:
        await asyncio.sleep(FOLDER_SYNC_INTERVAL_SECONDS)
        db = SessionLocal()
        try:
            folders = await asyncio.to_thread(folders_for_sync, db)
            for source, vm_name in folders:
                agent_ws = agent_sockets.get(vm_name)
                if agent_ws is None:
                    continue
                try:
                    files = await scan_folder(agent_ws, source.path, source.container)
                except ScanUnavailable:
                    # An agent too old to know the question, or one that
                    # dropped mid-scan. Either way the next pass retries.
                    continue
                await asyncio.to_thread(apply_scan, db, source, files)
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
    asyncio.create_task(_url_check_loop())
    asyncio.create_task(_folder_sync_loop())


@app.exception_handler(DataError)
async def unreadable_identifier(request, exc: DataError):
    """An id that is not a UUID means no such row, not a server fault.

    Every id in this API is a path parameter, and Postgres refuses one that
    is not a UUID before any handler can check whether it exists -- so
    /vms/banana used to answer 500 on every route that takes an id. Caught
    here rather than in each of them, because the next route to be added
    would otherwise have to remember.

    Anything else that reaches this is a real fault and is left to behave
    like one.
    """
    if "invalid input syntax for type uuid" in str(getattr(exc, "orig", exc)):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    raise exc


@app.get("/health")
def health():
    return {"status": "ok"}
