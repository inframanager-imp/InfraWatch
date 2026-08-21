from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import Alert, User, VM
from ..security import get_current_user, require_admin

router = APIRouter(tags=["alerts"])

MAX_SNOOZE_HOURS = 24 * 30  # 30 days -- generous ceiling, not a real limit anyone should hit


def _get_alert_or_404(db: Session, alert_id: str) -> Alert:
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


def _alert_target(alert: Alert) -> str:
    vm = alert.vm  # relationship -- one extra lazy-load, fine at this call frequency
    vm_name = vm.name if vm else alert.vm_id
    return f"{vm_name}:{alert.resource_name}:{alert.rule}"


@router.get("/alerts", response_model=list[schemas.AlertOut])
def list_all_alerts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = db.query(Alert).filter(Alert.status == "active")
    if user.role != "admin":
        allowed_ids = {a.vm_id for a in user.vm_access}
        query = query.filter(Alert.vm_id.in_(allowed_ids))
    return query.order_by(Alert.severity.desc(), Alert.last_seen.desc()).all()


@router.get("/vms/{vm_id}/alerts", response_model=list[schemas.AlertOut])
def list_vm_alerts(vm_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if user.role != "admin":
        allowed_ids = {a.vm_id for a in user.vm_access}
        if vm.id not in allowed_ids:
            raise HTTPException(status_code=403, detail="Not authorized for this VM")
    return db.query(Alert).filter(Alert.vm_id == vm.id).order_by(Alert.status, Alert.last_seen.desc()).all()


@router.post("/alerts/{alert_id}/acknowledge", response_model=schemas.AlertOut)
def acknowledge_alert(alert_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    alert = _get_alert_or_404(db, alert_id)
    if alert.status != "active":
        raise HTTPException(status_code=400, detail="Only active alerts can be acknowledged")
    alert.acknowledged_at = datetime.utcnow()
    alert.acknowledged_by = admin.email
    db.commit()
    db.refresh(alert)
    log_action(db, admin.email, "alert.acknowledge", target=_alert_target(alert))
    return alert


@router.post("/alerts/{alert_id}/unacknowledge", response_model=schemas.AlertOut)
def unacknowledge_alert(alert_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    alert = _get_alert_or_404(db, alert_id)
    alert.acknowledged_at = None
    alert.acknowledged_by = None
    db.commit()
    db.refresh(alert)
    log_action(db, admin.email, "alert.unacknowledge", target=_alert_target(alert))
    return alert


@router.post("/alerts/{alert_id}/snooze", response_model=schemas.AlertOut)
def snooze_alert(alert_id: str, payload: schemas.AlertSnoozeRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    alert = _get_alert_or_404(db, alert_id)
    if alert.status != "active":
        raise HTTPException(status_code=400, detail="Only active alerts can be snoozed")
    if payload.hours <= 0 or payload.hours > MAX_SNOOZE_HOURS:
        raise HTTPException(status_code=400, detail=f"hours must be between 0 and {MAX_SNOOZE_HOURS}")
    alert.snoozed_until = datetime.utcnow() + timedelta(hours=payload.hours)
    db.commit()
    db.refresh(alert)
    log_action(db, admin.email, "alert.snooze", target=_alert_target(alert), detail=f"{payload.hours}h")
    return alert


@router.post("/alerts/{alert_id}/unsnooze", response_model=schemas.AlertOut)
def unsnooze_alert(alert_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    alert = _get_alert_or_404(db, alert_id)
    alert.snoozed_until = None
    db.commit()
    db.refresh(alert)
    log_action(db, admin.email, "alert.unsnooze", target=_alert_target(alert))
    return alert
