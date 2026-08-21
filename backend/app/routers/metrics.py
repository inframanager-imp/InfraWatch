from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..metrics import RANGE_SPECS, bucketed_history
from ..models import User, VM
from ..security import get_current_user

router = APIRouter(tags=["metrics"])


@router.get("/vms/{vm_id}/metrics", response_model=list[schemas.MetricPointOut])
def vm_metric_history(vm_id: str, range: str = Query("24h"), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if range not in RANGE_SPECS:
        raise HTTPException(status_code=400, detail=f"range must be one of {sorted(RANGE_SPECS)}")
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    if user.role != "admin":
        allowed_ids = {a.vm_id for a in user.vm_access}
        if vm.id not in allowed_ids:
            raise HTTPException(status_code=403, detail="Not authorized for this VM")
    return bucketed_history(db, vm.id, range)
