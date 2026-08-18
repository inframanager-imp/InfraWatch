from sqlalchemy.orm import Session

from .config import settings
from .models import Environment, User, VM
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

    if db.query(VM).count() == 0:
        prod = db.query(Environment).filter(Environment.name == "production").first()
        db.add(VM(
            name="prod-web-01",
            hostname="prod-web-01.internal",
            ip_address="10.20.1.11",
            environment_id=prod.id,
            agent_token_hash=hash_password("seed-token-not-usable"),
        ))
        db.commit()
