"""Alert email notifications.

Entirely optional — if no SMTP host is configured, sending silently no-ops
rather than erroring, the same "degrades gracefully" pattern used for the
agent's optional websocket-client dependency. Callers should treat
send_alert_notification as fire-and-forget: it never raises, so a broken or
misconfigured mail server can't take down alert evaluation or a heartbeat.

Fires only when an alert newly opens (see app/alerts.py's _upsert_alert) —
never on every heartbeat while a problem is still ongoing — and batches
everything that opened in one evaluation pass into a single email per VM,
rather than one email per alert.

Configuration arrives as a dict rather than being read from `settings`
directly, because it is now editable from the admin UI and resolved
per-send against the database (see settings_store.smtp_config). Callers
holding a session pass it in; the env values stand in when none is given.
"""
import smtplib
import ssl
import sys
from email.mime.text import MIMEText

from .config import settings

SMTP_FIELDS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "frontend_url")


def env_smtp_config() -> dict:
    return {field: getattr(settings, field, None) for field in SMTP_FIELDS}


def _deliver(cfg: dict, subject: str, body: str, recipients: list[str]) -> None:
    """Raises on any delivery failure. send_alert_notification swallows that;
    the admin test endpoint deliberately does not, because a silent failure
    is the exact problem that endpoint exists to expose."""
    host = (cfg.get("smtp_host") or "").strip()
    port = int(cfg.get("smtp_port") or 587)
    user = (cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_password") or ""
    sender = (cfg.get("smtp_from") or user or "").strip()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    if port == 465:
        # Implicit TLS from the first byte -- starttls() on 465 never completes.
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            if user:
                server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if port != 25:  # plain port 25 is typically an unauthenticated relay/test catcher
                server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())


def send_alert_notification(vm_name: str, alerts: list[dict], recipients: list[str], cfg: dict | None = None):
    cfg = cfg or env_smtp_config()
    if not (cfg.get("smtp_host") or "").strip() or not recipients or not alerts:
        return
    try:
        count = len(alerts)
        subject = f"[InfraWatch] {count} new alert{'s' if count != 1 else ''} on {vm_name}"

        lines = [
            f"{a['severity'].upper()} — {a['resource_type']}: {a['resource_name']}\n  {a['message']}"
            for a in alerts
        ]
        body = "\n\n".join(lines)
        frontend_url = (cfg.get("frontend_url") or "").strip()
        if frontend_url:
            body += f"\n\nView in InfraWatch: {frontend_url.rstrip('/')}/"

        _deliver(cfg, subject, body, recipients)
    except Exception as e:
        print(f"alert email failed: {e}", file=sys.stderr)


def send_test_email(cfg: dict, recipient: str) -> None:
    """Used by the admin settings page. Lets exceptions escape so the caller
    can show the operator why delivery failed."""
    frontend_url = (cfg.get("frontend_url") or "").strip()
    body = (
        "This is a test message from InfraWatch.\n\n"
        "If you are reading this, alert notifications are configured correctly "
        "and will be delivered to the admin users on this instance."
    )
    if frontend_url:
        body += f"\n\nInfraWatch: {frontend_url.rstrip('/')}/"
    _deliver(cfg, "[InfraWatch] Test message", body, [recipient])
