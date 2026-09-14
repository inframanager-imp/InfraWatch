from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..database import get_db
from ..models import User
from ..notifications import send_test_email
from ..security import require_admin
from ..settings_store import save_values, smtp_config

router = APIRouter(tags=["settings"])


def _as_out(cfg: dict) -> schemas.SmtpSettingsOut:
    # The stored password is never returned -- the client only learns whether
    # one exists, so the form can show a "leave blank to keep" placeholder.
    return schemas.SmtpSettingsOut(
        smtp_host=(cfg.get("smtp_host") or ""),
        smtp_port=cfg.get("smtp_port") or 587,
        smtp_user=(cfg.get("smtp_user") or ""),
        smtp_from=(cfg.get("smtp_from") or ""),
        frontend_url=(cfg.get("frontend_url") or ""),
        password_set=bool(cfg.get("smtp_password")),
    )


@router.get("/settings/smtp", response_model=schemas.SmtpSettingsOut)
def read_smtp_settings(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _as_out(smtp_config(db))


@router.put("/settings/smtp", response_model=schemas.SmtpSettingsOut)
def update_smtp_settings(
    payload: schemas.SmtpSettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    if payload.smtp_port < 1 or payload.smtp_port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")

    values = {
        "smtp_host": payload.smtp_host.strip(),
        "smtp_port": str(payload.smtp_port),
        "smtp_user": payload.smtp_user.strip(),
        "smtp_from": payload.smtp_from.strip(),
        "frontend_url": payload.frontend_url.strip().rstrip("/"),
    }
    # An omitted password means "keep the stored one" -- the client never
    # receives it, so it has nothing to send back, and treating blank as
    # "clear it" would wipe the credential on every unrelated edit.
    if payload.smtp_password:
        values["smtp_password"] = payload.smtp_password

    save_values(db, values)
    log_action(
        db, user.email, "smtp_settings_update", target=values["smtp_host"] or "(cleared)",
        detail=f"port={values['smtp_port']} from={values['smtp_from']} password_changed={bool(payload.smtp_password)}",
    )
    return _as_out(smtp_config(db))


@router.post("/settings/smtp/test", response_model=schemas.SmtpTestResult)
def send_smtp_test(
    payload: schemas.SmtpTestRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    cfg = smtp_config(db)
    if not (cfg.get("smtp_host") or "").strip():
        raise HTTPException(status_code=400, detail="No SMTP host configured yet — save one first.")

    recipient = (payload.recipient or user.email).strip()
    if "@" not in recipient:
        raise HTTPException(status_code=400, detail="A valid recipient address is required")

    try:
        send_test_email(cfg, recipient)
    except Exception as e:
        # Reported verbatim rather than swallowed. Alert mail fails silently
        # by design, which makes a wrong credential indistinguishable from
        # "nothing has alerted yet" -- this endpoint exists to tell them apart.
        log_action(db, user.email, "smtp_test_failed", target=recipient, detail=f"{type(e).__name__}: {e}")
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")

    log_action(db, user.email, "smtp_test_sent", target=recipient)
    return schemas.SmtpTestResult(status="sent", recipient=recipient)
