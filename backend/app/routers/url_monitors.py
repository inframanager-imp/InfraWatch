from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

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


def _serialize(db: Session, monitor: UrlMonitor) -> schemas.UrlMonitorOut:
    out = schemas.UrlMonitorOut.model_validate(monitor)
    since = datetime.utcnow() - timedelta(days=UPTIME_DAYS)
    samples = (
        db.query(UrlCheckSample)
        .filter(UrlCheckSample.monitor_id == monitor.id, UrlCheckSample.checked_at >= since)
        .all()
    )
    if samples:
        out.uptime_7d = round(100.0 * sum(1 for s in samples if s.ok) / len(samples), 2)

    # One figure per day, oldest first. A day with no samples stays null rather
    # than reporting 0% -- "we were not watching" is not "it was down".
    buckets: list[list[bool]] = [[] for _ in range(UPTIME_DAYS)]
    today = datetime.utcnow().date()
    for s in samples:
        index = UPTIME_DAYS - 1 - (today - s.checked_at.date()).days
        if 0 <= index < UPTIME_DAYS:
            buckets[index].append(s.ok)
    out.daily = [
        round(100.0 * sum(1 for ok in day if ok) / len(day), 1) if day else None
        for day in buckets
    ]
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


@router.get("/vms/{vm_id}/url-monitors", response_model=list[schemas.UrlMonitorOut])
def list_monitors(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = _accessible_vm(db, user, vm_id)
    monitors = db.query(UrlMonitor).filter(UrlMonitor.vm_id == vm.id).order_by(UrlMonitor.name).all()
    return [_serialize(db, m) for m in monitors]


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
        raise HTTPException(status_code=400, detail="This VM already has a monitor with that name")

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
