"""Alert email notifications.

Entirely optional — if SMTP_HOST isn't set, sending silently no-ops rather
than erroring, the same "degrades gracefully" pattern used for the agent's
optional websocket-client dependency. Callers should treat this as
fire-and-forget: it never raises, so a broken/misconfigured mail server
can't take down alert evaluation or a heartbeat.

Fires only when an alert newly opens (see app/alerts.py's _upsert_alert) —
never on every heartbeat while a problem is still ongoing — and batches
everything that opened in one evaluation pass into a single email per VM,
rather than one email per alert.
"""
import smtplib
import ssl
import sys
from email.mime.text import MIMEText

from .config import settings


def send_alert_notification(vm_name: str, alerts: list[dict], recipients: list[str]):
    if not settings.smtp_host or not recipients or not alerts:
        return
    try:
        count = len(alerts)
        subject = f"[InfraWatch] {count} new alert{'s' if count != 1 else ''} on {vm_name}"

        lines = [
            f"{a['severity'].upper()} — {a['resource_type']}: {a['resource_name']}\n  {a['message']}"
            for a in alerts
        ]
        body = "\n\n".join(lines)
        if settings.frontend_url:
            body += f"\n\nView in InfraWatch: {settings.frontend_url.rstrip('/')}/"

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = ", ".join(recipients)

        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                if settings.smtp_port != 25:  # plain port 25 is typically an unauthenticated relay/test catcher
                    server.starttls(context=ssl.create_default_context())
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())
    except Exception as e:
        print(f"alert email failed: {e}", file=sys.stderr)
