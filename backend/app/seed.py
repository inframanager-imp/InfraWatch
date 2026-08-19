from sqlalchemy.orm import Session

from .config import settings
from .models import Environment, User
from .security import hash_password

DEFAULT_ENVIRONMENTS = ["production", "staging", "development", "qa", "demo", "das", "sandbox"]


def seed(db: Session):
    if not db.query(User).filter(User.email == settings.admin_email).first():
        db.add(User(
            email=settings.admin_email,
            name="Admin",
            password_hash=hash_password(settings.admin_password),
            role="admin",
        ))

    existing_envs = {e.name for e in db.query(Environment).all()}
    for name in DEFAULT_ENVIRONMENTS:
        if name not in existing_envs:
            db.add(Environment(name=name))

    db.commit()
