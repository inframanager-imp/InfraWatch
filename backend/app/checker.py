"""The background loop that runs URL monitors, and the bookkeeping for one result.

Only monitors with check_from="server" run here. The agent-side ones are
reported by the agent on its heartbeat; apply_result is shared by both paths
so a result is recorded identically wherever the request was made.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .alerts import evaluate_url_alerts
from .models import UrlCheckSample, UrlMonitor
from .urlmonitor import run_check

SAMPLE_RETENTION_DAYS = 8  # a little past the 7 days the UI charts


def monitor_config(monitor: UrlMonitor) -> dict:
    return {
        "url": monitor.url,
        "method": monitor.method,
        "timeout_seconds": monitor.timeout_seconds,
        "expected_status": monitor.expected_status,
        "body_contains": monitor.body_contains,
        "headers": monitor.headers,
        "verify_tls": monitor.verify_tls,
    }


def apply_result(db: Session, monitor: UrlMonitor, result: dict) -> None:
    """Write one check result onto the monitor and record a sample.

    Does not commit -- the caller decides the transaction boundary, since the
    loop batches a pass and the heartbeat already has one open.
    """
    monitor.last_checked_at = result.get("checked_at") or datetime.utcnow()
    monitor.last_code = result.get("status_code")
    monitor.last_response_ms = result.get("response_ms")
    monitor.last_error = result.get("error")

    if result.get("ok"):
        monitor.consecutive_failures = 0
        slow = monitor.slow_ms is not None and (monitor.last_response_ms or 0) >= monitor.slow_ms
        monitor.last_status = "slow" if slow else "up"
    else:
        monitor.consecutive_failures = (monitor.consecutive_failures or 0) + 1
        monitor.last_status = "down"

    if result.get("cert_checked"):
        monitor.cert_expires_at = result.get("cert_expires_at")
        monitor.cert_issuer = result.get("cert_issuer")
        monitor.cert_error = result.get("cert_error")
    elif result.get("cert_unreachable"):
        # Host was not reachable: clear any stale certificate verdict rather
        # than leaving yesterday's reading to look like today's.
        monitor.cert_error = None

    db.add(UrlCheckSample(
        monitor_id=monitor.id,
        checked_at=monitor.last_checked_at,
        ok=bool(result.get("ok")),
        response_ms=monitor.last_response_ms,
        status_code=monitor.last_code,
    ))


def due_monitors(db: Session) -> list[UrlMonitor]:
    now = datetime.utcnow()
    monitors = (
        db.query(UrlMonitor)
        .filter(UrlMonitor.enabled == True, UrlMonitor.check_from == "server")  # noqa: E712
        .all()
    )
    return [
        m for m in monitors
        if m.last_checked_at is None
        or m.last_checked_at + timedelta(seconds=m.interval_seconds) <= now
    ]


def prune_url_samples(db: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(days=SAMPLE_RETENTION_DAYS)
    deleted = (
        db.query(UrlCheckSample)
        .filter(UrlCheckSample.checked_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


def run_due_checks(db: Session) -> list[tuple[object, list, list]]:
    """Checks every server-side monitor that is due.

    Returns (vm, opened, resolved) per monitor that changed alert state, so the
    caller can mail while its session is still open.
    """
    changes = []
    for monitor in due_monitors(db):
        result = run_check(monitor_config(monitor))
        apply_result(db, monitor, result)
        opened, resolved = evaluate_url_alerts(db, monitor)
        if opened or resolved:
            changes.append((monitor.vm, opened, resolved))
    db.commit()
    return changes
