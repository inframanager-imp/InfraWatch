"""S3 inventory for the AWS accounts already registered for logs.

Hangs off the same account records and the same per-user grants as the log
viewer, because it is the same account and the same assumed role -- giving
S3 its own account list would mean registering every account twice and
keeping two sets of permissions in step.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..s3 import S3Error, list_buckets
from ..security import get_current_user
# Imported rather than re-implemented: one definition of who may see an
# account, so a change to the grant rules cannot miss this page.
from .aws_logs import _readable_account_or_403, _readable_accounts

router = APIRouter(tags=["s3"])


@router.get("/s3/accounts")
def s3_accounts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The accounts this user may inspect — the same list the log viewer uses."""
    return [{"id": a.id, "name": a.name, "region": a.region} for a in _readable_accounts(db, user)]


@router.get("/s3/accounts/{account_id}/buckets")
async def s3_buckets(
    account_id: str,
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    account = _readable_account_or_403(db, user, account_id)
    try:
        # Several AWS round trips per bucket; off the event loop so one slow
        # account cannot stall every other request on the server.
        buckets = await asyncio.to_thread(list_buckets, account, refresh)
    except S3Error as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "buckets": buckets,
        "total_bytes": sum(b["size_bytes"] or 0 for b in buckets),
        "total_objects": sum(b["object_count"] or 0 for b in buckets),
        "flagged": sum(1 for b in buckets if b["issues"]),
    }
