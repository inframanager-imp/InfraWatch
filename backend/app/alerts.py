"""Alert rule evaluation.

Two entry points:
- evaluate_heartbeat_alerts(db, vm, containers, services): called from the
  heartbeat handler with this heartbeat's just-received payload data (not
  re-queried from the DB, since Container/Service rows get fully replaced
  every heartbeat anyway). Covers every rule except vm_offline.
- sweep_vm_offline_alerts(db): a periodic background check (see main.py)
  for VMs that have gone silent -- nothing reactive would ever notice that,
  since there's no heartbeat left to react to.

Both funnel through _upsert_alert, the one place that knows how to
open/extend/resolve an alert row without ever duplicating an ongoing
incident. One row per (vm, resource, rule) at a time; a resolved incident
that recurs later reopens the same row as a fresh one rather than losing
its history to a new row.
"""
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import Alert, ResourceSetting, VM

OFFLINE_AFTER_SECONDS = 90  # matches the online/offline threshold in routers/vms.py
CPU_HIGH_THRESHOLD = 90
MEM_HIGH_THRESHOLD = 90
DISK_HIGH_THRESHOLD = 90
RESTART_LOOP_THRESHOLD = 3


def _upsert_alert(db: Session, vm_id: str, resource_type: str, resource_name: str, rule: str,
                   condition: bool, severity: str = "warning", message: str = ""):
    """Returns ("opened", alert) when this call made the alert newly active
    (a brand new incident, or a resolved one recurring), ("resolved", alert)
    when it just cleared, and None for everything else -- an incident that
    is simply still going, or nothing to do. Those two transitions are the
    only moments worth an email; every heartbeat in between just extends
    what is already known.

    A snoozed alert reports neither: a snooze mutes the incident, and that
    covers it clearing as well as it recurring."""
    existing = db.query(Alert).filter(
        Alert.vm_id == vm_id, Alert.resource_type == resource_type,
        Alert.resource_name == resource_name, Alert.rule == rule,
    ).first()
    now = datetime.utcnow()

    if condition:
        if existing and existing.status == "active":
            existing.last_seen = now
            if message:
                existing.message = message
            return None
        elif existing:  # previously resolved, condition is true again -- reopen as a fresh incident
            still_snoozed = existing.snoozed_until is not None and existing.snoozed_until > now
            existing.status = "active"
            existing.first_seen = now
            existing.last_seen = now
            existing.resolved_at = None
            existing.message = message
            # A reopened incident is functionally new -- last occurrence's
            # acknowledgment shouldn't silently apply to this one.
            existing.acknowledged_at = None
            existing.acknowledged_by = None
            if still_snoozed:
                # Honor an in-progress mute across a resolve/reactivate flap
                # instead of re-notifying on every flap -- that's the whole
                # point of snoozing. Snooze itself is left in place.
                return None
            existing.snoozed_until = None
            return ("opened", existing)
        else:
            alert = Alert(
                vm_id=vm_id, resource_type=resource_type, resource_name=resource_name, rule=rule,
                severity=severity, message=message, status="active", first_seen=now, last_seen=now,
            )
            db.add(alert)
            return ("opened", alert)
    else:
        if existing and existing.status == "active":
            existing.status = "resolved"
            existing.resolved_at = now
            if existing.snoozed_until is not None and existing.snoozed_until > now:
                return None
            return ("resolved", existing)
        return None


def evaluate_heartbeat_alerts(db: Session, vm: VM, containers: list, services: list) -> tuple[list[Alert], list[Alert]]:
    """Returns (opened, resolved) -- the alerts that changed state on this
    heartbeat, in each direction."""
    opened, resolved = [], []

    def upsert(*args, quiet=False, **kwargs):
        result = _upsert_alert(db, *args, **kwargs)
        # quiet: the alert is being cleared because someone switched Monitor
        # off, not because the thing recovered -- an "all clear" email for
        # that would claim a recovery that never happened.
        if not result or quiet:
            return
        event, alert = result
        (opened if event == "opened" else resolved).append(alert)

    upsert(
        vm.id, "vm", vm.name, "vm_cpu_high",
        condition=vm.cpu_percent is not None and vm.cpu_percent >= CPU_HIGH_THRESHOLD,
        severity="warning", message=f"High CPU usage ({vm.cpu_percent}%)",
    )
    upsert(
        vm.id, "vm", vm.name, "vm_mem_high",
        condition=vm.mem_percent is not None and vm.mem_percent >= MEM_HIGH_THRESHOLD,
        severity="warning", message=f"High memory usage ({vm.mem_percent}%)",
    )
    upsert(
        vm.id, "vm", vm.name, "vm_disk_high",
        condition=vm.disk_percent is not None and vm.disk_percent >= DISK_HIGH_THRESHOLD,
        severity="critical", message=f"Low disk space ({vm.disk_percent}% used)",
    )

    unmonitored_containers = {
        row.name for row in db.query(ResourceSetting.name).filter(
            ResourceSetting.vm_id == vm.id, ResourceSetting.resource_type == "container",
            ResourceSetting.monitor_enabled == False,  # noqa: E712
        ).all()
    }
    for c in containers:
        if c.name in unmonitored_containers:
            upsert(vm.id, "container", c.name, "container_stopped", condition=False, quiet=True)
            upsert(vm.id, "container", c.name, "container_restart_loop", condition=False, quiet=True)
            continue
        stopped = not re.match(r"up", c.status or "", re.I)
        upsert(
            vm.id, "container", c.name, "container_stopped", condition=stopped,
            severity="warning", message=f"Container is not running (status: {c.status})",
        )
        restart_looping = (c.restart_count or 0) >= RESTART_LOOP_THRESHOLD
        upsert(
            vm.id, "container", c.name, "container_restart_loop", condition=restart_looping,
            severity="warning", message=f"Container has restarted {c.restart_count} times",
        )

    unmonitored_services = {
        row.name for row in db.query(ResourceSetting.name).filter(
            ResourceSetting.vm_id == vm.id, ResourceSetting.resource_type == "service",
            ResourceSetting.monitor_enabled == False,  # noqa: E712
        ).all()
    }
    for s in services:
        if s.name in unmonitored_services:
            upsert(vm.id, "service", s.name, "service_failed", condition=False, quiet=True)
            upsert(vm.id, "service", s.name, "service_inactive", condition=False, quiet=True)
            continue
        failed = s.status == "failed"
        upsert(
            vm.id, "service", s.name, "service_failed", condition=failed,
            severity="critical", message=f"Service failed (sub-state: {s.sub_state})",
        )

        # A service that's gone inactive/dead is presumed to have stopped
        # unexpectedly -- but only for application units. System/package
        # units sitting inactive/dead is completely normal (timers, oneshot
        # tasks between runs) and would otherwise flood every VM with false
        # positives, so this is deliberately scoped to custom units only.
        unexpectedly_stopped = s.custom is True and s.status == "inactive" and s.sub_state == "dead"
        upsert(
            vm.id, "service", s.name, "service_inactive", condition=unexpectedly_stopped,
            severity="warning", message="Application service is inactive (expected to be running)",
        )

    return opened, resolved


def sweep_vm_offline_alerts(db: Session) -> list[tuple[VM, str, Alert]]:
    """Returns (vm, event, alert) for every VM whose offline alert changed
    state this pass -- "opened" when it went quiet, "resolved" when its
    heartbeat came back."""
    now = datetime.utcnow()
    changes = []
    for vm in db.query(VM).all():
        # A VM that has never sent a heartbeat is "pending", not offline --
        # it just hasn't been installed on yet, which isn't alert-worthy.
        offline = vm.last_heartbeat is not None and (now - vm.last_heartbeat) > timedelta(seconds=OFFLINE_AFTER_SECONDS)
        result = _upsert_alert(
            db, vm.id, "vm", vm.name, "vm_offline", condition=offline,
            severity="critical", message="No heartbeat received",
        )
        if result:
            event, alert = result
            changes.append((vm, event, alert))
    db.commit()
    return changes
