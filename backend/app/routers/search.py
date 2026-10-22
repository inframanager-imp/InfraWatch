"""Fleet-wide search, answered by the database.

The browser used to search a copy of every container and unit on every VM,
which meant downloading all of it at sign-in -- four requests per VM, 145 of
them at 36 VMs, before anything could be drawn. Asking Postgres instead
turns that into one request typed on demand, and it stops getting slower as
the fleet grows.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Container, Environment, LogSource, Service, User, VM
from ..security import get_current_user

router = APIRouter(tags=["search"])

# Enough to find what you meant, few enough to scan with your eyes. The
# dropdown showed 20 when it searched in the browser; this keeps that.
PER_KIND = 10
TOTAL = 20


@router.get("/search")
def search(
    q: str = Query(..., min_length=2),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """VMs, containers, units and log paths matching q, within what you may see."""
    term = f"%{q.strip()}%"

    visible = db.query(VM.id)
    if user.role != "admin":
        visible = visible.filter(VM.id.in_([a.vm_id for a in user.vm_access]))
    vm_ids = [row[0] for row in visible.all()]
    if not vm_ids:
        return []

    names = dict(db.query(VM.id, VM.name).filter(VM.id.in_(vm_ids)).all())
    hits = []

    matched = (
        db.query(VM)
        .join(Environment, VM.environment_id == Environment.id)
        .filter(
            VM.id.in_(vm_ids),
            or_(VM.name.ilike(term), VM.ip_address.ilike(term),
                VM.hostname.ilike(term), Environment.name.ilike(term)),
        )
        .order_by(VM.name)
        .limit(PER_KIND)
        .all()
    )
    for vm in matched:
        hits.append({
            "kind": "VM", "label": vm.name, "vmId": vm.id, "tab": "overview",
            "sub": f"{vm.ip_address or vm.hostname or '—'} · {vm.environment.name}",
        })

    for model, kind, tab in (
        (Container, "Container", "docker"),
        (Service, "Unit", "systemd"),
    ):
        rows = (
            db.query(model)
            .filter(model.vm_id.in_(vm_ids), model.name.ilike(term))
            .order_by(model.name)
            .limit(PER_KIND)
            .all()
        )
        for row in rows:
            hits.append({"kind": kind, "label": row.name, "sub": names.get(row.vm_id, ""),
                         "vmId": row.vm_id, "tab": tab})

    sources = (
        db.query(LogSource)
        .filter(LogSource.vm_id.in_(vm_ids),
                or_(LogSource.name.ilike(term), LogSource.path.ilike(term)))
        .order_by(LogSource.name)
        .limit(PER_KIND)
        .all()
    )
    for source in sources:
        hits.append({
            "kind": "Log", "label": source.name, "vmId": source.vm_id, "tab": "logs",
            "sub": f"{names.get(source.vm_id, '')} · {source.path}",
        })

    return hits[:TOTAL]
