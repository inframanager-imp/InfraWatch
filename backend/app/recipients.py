"""Who receives alert email for a given VM.

Recipients come from the alert groups assigned to that VM, so a database
box can page the DBAs while a build runner pages nobody but the build team.
Addresses are free-form rather than InfraWatch users, which lets a group
point at a distribution list or an on-call address that has no account here.

A VM with no group assigned falls back to every admin. Silence is the one
outcome that must never arrive by accident -- forgetting to assign a group
should over-notify, never under-notify -- and it keeps behaviour identical
for every VM that existed before groups did.
"""
from sqlalchemy.orm import Session

from .models import User, VM


def recipients_for_vm(db: Session, vm: VM) -> list[str]:
    emails = {
        member.email.strip()
        for link in vm.alert_groups
        for member in link.group.members
        if member.email and member.email.strip()
    }
    if emails:
        return sorted(emails)
    return sorted({u.email for u in db.query(User).filter(User.role == "admin").all()})
