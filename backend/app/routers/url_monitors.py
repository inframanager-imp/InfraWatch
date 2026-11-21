from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import UrlCheckSample, UrlMonitor, User, VM
from ..security import get_current_user, require_admin
from ..urlmonitor import run_check

router = APIRouter(tags=["url-monitors"])

UPTIME_DAYS = 7


def _accessible_vm(db: Session, user: User, vm_id: str) -> VM:
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if user.role != "admin" and vm.id not in {a.vm_id for a in user.vm_access}:
        raise HTTPException(status_code=403, detail="Not authorized for this VM")
    return vm


def _as_date(value) -> date:
    """Postgres hands back a date; SQLite hands back its string form."""
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _uptime_by_day(db: Session, monitor_ids: list[str]) -> dict[str, dict[date, tuple[int, int]]]:
    """(checks, successes) per day per monitor, counted in a single query.

    Counted by the database rather than by loading the samples: a monitor
    checked every 15 seconds has around forty thousand of them inside this
    window, and the fleet page serialises every monitor the caller can see.
    Reading all of those rows in order to divide two numbers was the slowest
    thing this API did, and it ran before the page could be drawn.
    """
    if not monitor_ids:
        return {}
    since = datetime.utcnow() - timedelta(days=UPTIME_DAYS)
    day = func.date(UrlCheckSample.checked_at)
    rows = (
        db.query(
            UrlCheckSample.monitor_id,
            day.label("day"),
            func.count().label("checks"),
            func.sum(case((UrlCheckSample.ok, 1), else_=0)).label("successes"),
        )
        .filter(UrlCheckSample.monitor_id.in_(monitor_ids),
                UrlCheckSample.checked_at >= since)
        .group_by(UrlCheckSample.monitor_id, day)
        .all()
    )
    out: dict[str, dict[date, tuple[int, int]]] = {}
    for monitor_id, day_value, checks, successes in rows:
        out.setdefault(monitor_id, {})[_as_date(day_value)] = (int(checks), int(successes or 0))
    return out


def _serialize(db: Session, monitor: UrlMonitor,
               by_day: dict[date, tuple[int, int]] | None = None) -> schemas.UrlMonitorOut:
    """One monitor as the API returns it.

    by_day is this monitor's slice of a batched count, so a list of monitors
    costs one query for all of them. Left out, it is counted for this one --
    which is what the single-monitor endpoints want.
    """
    out = schemas.UrlMonitorOut.model_validate(monitor)
    days = by_day if by_day is not None else _uptime_by_day(db, [monitor.id]).get(monitor.id, {})

    checks = sum(c for c, _ in days.values())
    if checks:
        out.uptime_7d = round(100.0 * sum(s for _, s in days.values()) / checks, 2)

    # One figure per day, oldest first. A day with no samples stays null rather
    # than reporting 0% -- "we were not watching" is not "it was down".
    today = datetime.utcnow().date()
    daily = []
    for offset in range(UPTIME_DAYS - 1, -1, -1):
        entry = days.get(today - timedelta(days=offset))
        daily.append(round(100.0 * entry[1] / entry[0], 1) if entry and entry[0] else None)
    out.daily = daily
    return out


def _apply(monitor: UrlMonitor, payload: schemas.UrlMonitorIn) -> None:
    if payload.check_from not in ("server", "agent"):
        raise HTTPException(status_code=400, detail='check_from must be "server" or "agent"')
    if not payload.url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if payload.interval_seconds < 15:
        raise HTTPException(status_code=400, detail="Interval must be at least 15 seconds")
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="A name is required")

    monitor.name = payload.name.strip()
    monitor.url = payload.url.strip()
    monitor.check_from = payload.check_from
    monitor.method = (payload.method or "GET").upper()
    monitor.expected_status = (payload.expected_status or "").strip() or None
    monitor.body_contains = (payload.body_contains or "").strip() or None
    monitor.headers = (payload.headers or "").strip() or None
    monitor.interval_seconds = payload.interval_seconds
    monitor.timeout_seconds = max(1, payload.timeout_seconds)
    monitor.failure_threshold = max(1, payload.failure_threshold)
    monitor.slow_ms = payload.slow_ms
    monitor.verify_tls = payload.verify_tls
    monitor.cert_warn_days = (payload.cert_warn_days or "").strip() or None
    monitor.enabled = payload.enabled


@router.get("/url-monitors", response_model=list[schemas.UrlMonitorOut])
def list_all_monitors(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every monitor the caller may see, across the fleet."""
    # Outer join: a standalone monitor has no VM row to match. joinedload on
    # top of it, so naming the VM below does not cost a query per monitor.
    query = (db.query(UrlMonitor)
             .outerjoin(VM, UrlMonitor.vm_id == VM.id)
             .options(joinedload(UrlMonitor.vm)))
    if user.role != "admin":
        allowed = {a.vm_id for a in user.vm_access}
        query = query.filter(or_(UrlMonitor.vm_id.in_(allowed), UrlMonitor.vm_id.is_(None)))
    monitors = query.order_by(VM.name, UrlMonitor.name).all()

    # One count covering every monitor on the page, not one per row.
    by_day = _uptime_by_day(db, [m.id for m in monitors])
    out = []
    for monitor in monitors:
        item = _serialize(db, monitor, by_day.get(monitor.id, {}))
        item.vm_name = monitor.vm.name if monitor.vm else None
        out.append(item)
    return out


@router.post("/url-monitors", response_model=schemas.UrlMonitorOut)
def create_standalone_monitor(payload: schemas.UrlMonitorIn,
                              db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """A monitor for an endpoint that runs nowhere InfraWatch can reach.

    Checked from the server like any other, but attached to no VM, so it
    cannot inherit a VM's alert groups and mails the admins instead.
    """
    name = payload.name.strip()
    if db.query(UrlMonitor).filter(UrlMonitor.vm_id.is_(None), UrlMonitor.name == name).first():
        raise HTTPException(status_code=400, detail="A standalone monitor with that name already exists")

    monitor = UrlMonitor(vm_id=None)
    _apply(monitor, payload)
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    log_action(db, admin.email, "url_monitor.create", target=f"(no VM):{monitor.name}", detail=monitor.url)
    return _serialize(db, monitor)


@router.get("/vms/{vm_id}/url-monitors", response_model=list[schemas.UrlMonitorOut])
def list_monitors(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _accessible_vm(db, user, vm_id)
    monitors = db.query(UrlMonitor).filter(UrlMonitor.vm_id == vm.id).order_by(UrlMonitor.name).all()
    by_day = _uptime_by_day(db, [m.id for m in monitors])
    return [_serialize(db, m, by_day.get(m.id, {})) for m in monitors]


@router.post("/vms/{vm_id}/url-monitors", response_model=schemas.UrlMonitorOut)
def create_monitor(vm_id: str, payload: schemas.UrlMonitorIn,
                   db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if db.query(UrlMonitor).filter(UrlMonitor.vm_id == vm.id, UrlMonitor.name == payload.name.strip()).first():
        raise HTTPException(status_code=400, detail="This VM already has a monitor with that name")

    monitor = UrlMonitor(vm_id=vm.id)
    _apply(monitor, payload)
    db.add(monitor)
    db.commit()
    db.refresh(monitor)
    log_action(db, admin.email, "url_monitor.create", target=f"{vm.name}:{monitor.name}", detail=monitor.url)
    return _serialize(db, monitor)


@router.patch("/url-monitors/{monitor_id}", response_model=schemas.UrlMonitorOut)
def update_monitor(monitor_id: str, payload: schemas.UrlMonitorIn,
                   db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    monitor = db.query(UrlMonitor).filter(UrlMonitor.id == monitor_id).first()
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    clash = (
        db.query(UrlMonitor)
        .filter(UrlMonitor.vm_id == monitor.vm_id, UrlMonitor.name == payload.name.strip(),
                UrlMonitor.id != monitor.id)
        .first()
    )
    if clash:
        raise HTTPException(status_code=400, detail=(
            "This VM already has a monitor with that name" if monitor.vm_id
            else "A standalone monitor with that name already exists"))

    _apply(monitor, payload)
    db.commit()
    db.refresh(monitor)
    log_action(db, admin.email, "url_monitor.update", target=monitor.name, detail=monitor.url)
    return _serialize(db, monitor)


@router.delete("/url-monitors/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    monitor = db.query(UrlMonitor).filter(UrlMonitor.id == monitor_id).first()
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    name = monitor.name
    db.delete(monitor)
    db.commit()
    log_action(db, admin.email, "url_monitor.delete", target=name)


@router.post("/url-monitors/{monitor_id}/check", response_model=schemas.UrlMonitorOut)
def check_now(monitor_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Run a check immediately. Saving a monitor and waiting a minute to find
    out the URL was wrong is a poor way to learn it."""
    monitor = db.query(UrlMonitor).filter(UrlMonitor.id == monitor_id).first()
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")

    from ..checker import apply_result  # imported here to avoid a circular import
    result = run_check({
        "url": monitor.url, "method": monitor.method, "timeout_seconds": monitor.timeout_seconds,
        "expected_status": monitor.expected_status, "body_contains": monitor.body_contains,
        "headers": monitor.headers, "verify_tls": monitor.verify_tls,
    })
    apply_result(db, monitor, result)
    db.commit()
    db.refresh(monitor)
    return _serialize(db, monitor)
