from sqlalchemy.orm import Session

from .models import AuditLog


def log_action(db: Session, actor_email: str, action: str, target: str | None = None, detail: str | None = None):
    db.add(AuditLog(actor_email=actor_email, action=action, target=target, detail=detail))
    db.commit()
