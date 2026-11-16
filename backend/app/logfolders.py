"""Keeping a folder entry's file rows in step with what is on the VM.

A folder entry is a registered path with a wildcard in it. Syncing one asks
the VM what matches, writes a row for anything new, and removes the rows
whose file is no longer there -- a row pointing at a file logrotate deleted
is worse than no row, because it is only discovered by clicking it.
"""
import asyncio
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from .models import LogSource, VM
from .streams import scan_queues

# Seconds to wait for an agent to answer. Generous: the scan is a readdir on
# a folder that may hold a year of files, over a socket shared with every
# live log viewer on that VM.
SCAN_TIMEOUT = 25

# A folder nobody prunes holds a year of files, and a list that long is one
# nobody can use. The newest get rows of their own; the older ones are still
# reachable through the folder itself, which is what a date-range export
# reads.
MAX_SYNCED_FILES = 200


class ScanUnavailable(Exception):
    """The agent could not be asked, or did not answer.

    Not a failure of the folder: almost always a VM that is offline, or an
    agent still running a build from before folders existed, which does not
    recognise the question and so says nothing at all.
    """


def is_pattern(path: str) -> bool:
    return any(c in path for c in "*?[")


def folders_for_sync(db: Session):
    """Every folder entry, with the name of the VM that would be asked."""
    rows = (db.query(LogSource, VM.name)
              .join(VM, VM.id == LogSource.vm_id)
              .filter(LogSource.parent_id.is_(None))
              .all())
    return [(source, vm_name) for source, vm_name in rows if is_pattern(source.path)]


async def scan_folder(agent_ws, pattern: str, container: str | None) -> list[str]:
    """Ask one agent what matches, and wait for the single reply."""
    stream_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    scan_queues[stream_id] = queue
    try:
        try:
            await agent_ws.send_json({
                "action": "list_files", "stream_id": stream_id,
                "pattern": pattern, "container": container,
            })
        except Exception:
            raise ScanUnavailable("Could not reach the agent on this VM")
        try:
            files = await asyncio.wait_for(queue.get(), timeout=SCAN_TIMEOUT)
        except asyncio.TimeoutError:
            raise ScanUnavailable(
                "The agent did not answer. It is probably still running an older "
                "build -- it updates itself, so try again shortly."
            )
    finally:
        scan_queues.pop(stream_id, None)
    return [p for p in files if isinstance(p, str) and p.strip()]


def apply_scan(db: Session, source: LogSource, files: list[str]):
    """Write the difference. Returns (added, removed, kept, truncated)."""
    # Oldest first from the agent, so the cap keeps the newest.
    truncated = len(files) > MAX_SYNCED_FILES
    files = files[-MAX_SYNCED_FILES:]

    existing = {c.path: c for c in
                db.query(LogSource).filter(LogSource.parent_id == source.id).all()}
    added = 0
    for path in files:
        if existing.pop(path, None) is not None:
            continue
        db.add(LogSource(vm_id=source.vm_id, parent_id=source.id, path=path,
                         name=path.rsplit("/", 1)[-1], container=source.container))
        added += 1

    # Whatever is left did not come back from the scan.
    removed = len(existing)
    for stale in existing.values():
        db.delete(stale)

    source.synced_at = datetime.utcnow()
    db.commit()
    return added, removed, len(files), truncated
