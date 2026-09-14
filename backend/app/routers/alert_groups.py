from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import AlertGroup, AlertGroupMember, User, VMAlertGroup
from ..security import require_admin

router = APIRouter(tags=["alert-groups"])


def _serialize(group: AlertGroup) -> schemas.AlertGroupOut:
    return schemas.AlertGroupOut(
        id=group.id,
        name=group.name,
        emails=sorted(m.email for m in group.members),
        vm_count=len(group.vm_links),
    )


def _clean_emails(raw: list[str]) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively while keeping the
    address as typed. A pasted list is the normal way these arrive, so it is
    worth being forgiving about whitespace and repeats."""
    seen, out = set(), []
    for value in raw:
        email = (value or "").strip()
        if not email:
            continue
        if "@" not in email:
            raise HTTPException(status_code=400, detail=f"Not a valid email address: {email}")
        if email.lower() in seen:
            continue
        seen.add(email.lower())
        out.append(email)
    return out


def _get_group_or_404(db: Session, group_id: str) -> AlertGroup:
    group = db.query(AlertGroup).filter(AlertGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Alert group not found")
    return group


@router.get("/alert-groups", response_model=list[schemas.AlertGroupOut])
def list_alert_groups(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    groups = db.query(AlertGroup).order_by(AlertGroup.name).all()
    return [_serialize(g) for g in groups]


@router.post("/alert-groups", response_model=schemas.AlertGroupOut)
def create_alert_group(payload: schemas.AlertGroupIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A group name is required")
    if db.query(AlertGroup).filter(AlertGroup.name == name).first():
        raise HTTPException(status_code=400, detail="A group with this name already exists")

    group = AlertGroup(name=name)
    db.add(group)
    db.flush()
    for email in _clean_emails(payload.emails):
        db.add(AlertGroupMember(group_id=group.id, email=email))
    db.commit()
    db.refresh(group)
    log_action(db, admin.email, "alert_group.create", target=name, detail=f"{len(group.members)} recipients")
    return _serialize(group)


@router.patch("/alert-groups/{group_id}", response_model=schemas.AlertGroupOut)
def update_alert_group(group_id: str, payload: schemas.AlertGroupIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    group = _get_group_or_404(db, group_id)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A group name is required")
    if db.query(AlertGroup).filter(AlertGroup.name == name, AlertGroup.id != group.id).first():
        raise HTTPException(status_code=400, detail="A group with this name already exists")
    group.name = name

    # Replace the membership wholesale -- the UI edits the list as one block,
    # and diffing it here would only matter if rows carried state, which they
    # do not.
    emails = _clean_emails(payload.emails)
    db.query(AlertGroupMember).filter(AlertGroupMember.group_id == group.id).delete(synchronize_session=False)
    for email in emails:
        db.add(AlertGroupMember(group_id=group.id, email=email))

    db.commit()
    db.refresh(group)
    log_action(db, admin.email, "alert_group.update", target=name, detail=f"{len(emails)} recipients")
    return _serialize(group)


@router.delete("/alert-groups/{group_id}", status_code=204)
def delete_alert_group(group_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    group = _get_group_or_404(db, group_id)
    name = group.name
    # Assignments go with it; any VM left with no group falls back to
    # notifying admins rather than notifying nobody.
    db.query(VMAlertGroup).filter(VMAlertGroup.group_id == group.id).delete(synchronize_session=False)
    db.delete(group)
    db.commit()
    log_action(db, admin.email, "alert_group.delete", target=name)
