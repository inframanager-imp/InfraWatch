from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..models import Alert, User, VM
from ..security import get_current_user

router = APIRouter(tags=["alerts"])


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
