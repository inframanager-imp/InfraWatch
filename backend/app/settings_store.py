"""Runtime-editable settings, stored in the database with an environment fallback.

Anything saved in app_settings wins; anything unset there falls back to the
value from the environment (.env via config.py). That ordering means an
existing deployment keeps working untouched after this ships -- the table
starts empty and every lookup resolves to the same env value it always did
-- and an admin editing SMTP from the UI takes effect immediately, with no
file edit and no restart.

The SMTP password is the one value that never travels back out to a client;
see routers/settings.py.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings
from .models import AppSetting

SMTP_FIELDS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "frontend_url")
DEFAULT_SMTP_PORT = 587


def get_values(db: Session, keys) -> dict:
    keys = tuple(keys)
    stored = {row.key: row.value for row in db.query(AppSetting).filter(AppSetting.key.in_(keys)).all()}
    out = {}
    for key in keys:
        value = stored.get(key)
        # An empty string in the table is treated as "not set" rather than as
        # a deliberate blank -- otherwise clearing a field in the UI would
        # silently resurrect the env value it was meant to override.
        out[key] = value if value not in (None, "") else getattr(settings, key, None)
    return out


def smtp_config(db: Session) -> dict:
    cfg = get_values(db, SMTP_FIELDS)
    try:
        cfg["smtp_port"] = int(cfg["smtp_port"] or DEFAULT_SMTP_PORT)
    except (TypeError, ValueError):
        cfg["smtp_port"] = DEFAULT_SMTP_PORT
    return cfg


def save_values(db: Session, values: dict) -> None:
    now = datetime.utcnow()
    for key, value in values.items():
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
            row.updated_at = now
        else:
            db.add(AppSetting(key=key, value=value, updated_at=now))
    db.commit()
