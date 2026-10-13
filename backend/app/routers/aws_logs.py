import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from .. import schemas
from ..audit import log_action
from ..cloudwatch import (
    CloudWatchError, check_connection, discover_ecs_services, fetch_backlog, forget,
    list_log_groups, tail,
)
from ..database import SessionLocal, get_db
from ..models import AwsLogAccount, AwsLogLabel, User
from ..security import get_current_user, get_user_from_token, require_admin

router = APIRouter(tags=["aws-logs"])

BACKLOG_LINES = 3000


def _get_account(db: Session, account_id: str) -> AwsLogAccount:
    account = db.query(AwsLogAccount).filter(AwsLogAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="AWS account not found")
    return account


def _apply(account: AwsLogAccount, payload: schemas.AwsLogAccountIn) -> None:
    name = payload.name.strip()
    role_arn = payload.role_arn.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required")
    if not role_arn.startswith("arn:aws:iam::"):
        raise HTTPException(status_code=400, detail="Role ARN should look like arn:aws:iam::<account>:role/<name>")
    if not payload.region.strip():
        raise HTTPException(status_code=400, detail="A region is required")
    account.name = name
    account.role_arn = role_arn
    account.external_id = (payload.external_id or "").strip() or None
    account.region = payload.region.strip()
    account.enabled = payload.enabled


def _readable_accounts(db: Session, user: User):
    """Admins see every account; everyone else sees only what they were given.

    Default-deny: a read-only user with no grants sees nothing. Logs routinely
    carry more than metrics do, so the quiet default has to be nothing.
    """
    query = db.query(AwsLogAccount)
    if user.role != "admin":
        allowed = {a.account_id for a in user.aws_log_access}
        if not allowed:
            return []
        query = query.filter(AwsLogAccount.id.in_(allowed))
    return query.order_by(AwsLogAccount.name).all()


def _readable_account_or_403(db: Session, user: User, account_id: str) -> AwsLogAccount:
    account = _get_account(db, account_id)
    if user.role != "admin" and account.id not in {a.account_id for a in user.aws_log_access}:
        raise HTTPException(status_code=403, detail="Not authorized for this AWS account")
    return account


@router.get("/aws-log-accounts", response_model=list[schemas.AwsLogAccountOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _readable_accounts(db, user)


@router.post("/aws-log-accounts", response_model=schemas.AwsLogAccountOut)
def create_account(payload: schemas.AwsLogAccountIn, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    if db.query(AwsLogAccount).filter(AwsLogAccount.name == payload.name.strip()).first():
        raise HTTPException(status_code=400, detail="An account with this name already exists")
    account = AwsLogAccount()
    _apply(account, payload)
    db.add(account)
    db.commit()
    db.refresh(account)
    log_action(db, admin.email, "aws_log_account.create", target=account.name, detail=account.role_arn)
    return account


@router.patch("/aws-log-accounts/{account_id}", response_model=schemas.AwsLogAccountOut)
def update_account(account_id: str, payload: schemas.AwsLogAccountIn,
                   db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    account = _get_account(db, account_id)
    clash = (
        db.query(AwsLogAccount)
        .filter(AwsLogAccount.name == payload.name.strip(), AwsLogAccount.id != account.id)
        .first()
    )
    if clash:
        raise HTTPException(status_code=400, detail="An account with this name already exists")
    _apply(account, payload)
    db.commit()
    db.refresh(account)
    # Role or region may have changed, so any cached credentials are now wrong.
    forget(account.id)
    log_action(db, admin.email, "aws_log_account.update", target=account.name, detail=account.role_arn)
    return account


@router.delete("/aws-log-accounts/{account_id}", status_code=204)
def delete_account(account_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    account = _get_account(db, account_id)
    name = account.name
    db.delete(account)
    db.commit()
    forget(account_id)
    log_action(db, admin.email, "aws_log_account.delete", target=name)


@router.post("/aws-log-accounts/{account_id}/check", response_model=schemas.AwsLogAccountOut)
async def check_account(account_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Assume the role and read a page of log groups. Both halves of the IAM
    setup fail differently, and the message says which."""
    account = _get_account(db, account_id)
    forget(account.id)
    result = await asyncio.to_thread(check_connection, account)
    from datetime import datetime
    account.last_checked_at = datetime.utcnow()
    account.last_check_ok = result["ok"]
    account.last_check_detail = result["detail"]
    db.commit()
    db.refresh(account)
    return account


@router.get("/aws-log-accounts/{account_id}/log-groups", response_model=list[schemas.LogGroupOut])
async def account_log_groups(account_id: str, prefix: str | None = Query(None),
                             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = _readable_account_or_403(db, user, account_id)
    try:
        groups = await asyncio.to_thread(list_log_groups, account, prefix)
    except CloudWatchError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Returns [] if the role lacks ecs:*, which leaves the raw names showing
    # rather than failing the whole listing.
    services = await asyncio.to_thread(discover_ecs_services, account)
    by_group: dict[str, list[dict]] = {}
    for item in services:
        if item.get("log_group"):
            by_group.setdefault(item["log_group"], []).append(item)

    labels = {
        row.log_group: row.label
        for row in db.query(AwsLogLabel).filter(AwsLogLabel.account_id == account.id).all()
    }

    out = []
    for group in groups:
        matches = by_group.get(group["name"], [])
        out.append(schemas.LogGroupOut(
            name=group["name"],
            stored_bytes=group.get("stored_bytes"),
            retention_days=group.get("retention_days"),
            label=labels.get(group["name"]),
            services=sorted({m["service"] for m in matches if m.get("service")}),
            cluster=next((m["cluster"] for m in matches if m.get("cluster")), None),
        ))
    return out


@router.put("/aws-log-accounts/{account_id}/log-groups/label", response_model=schemas.LogGroupOut)
def set_log_group_label(account_id: str, payload: schemas.LogGroupLabelIn,
                        db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Name a log group something a human recognises. A blank label removes the
    override, so the ECS service name (or the raw name) shows again."""
    account = _get_account(db, account_id)
    label = payload.label.strip()
    row = (
        db.query(AwsLogLabel)
        .filter(AwsLogLabel.account_id == account.id, AwsLogLabel.log_group == payload.log_group)
        .first()
    )
    if not label:
        if row:
            db.delete(row)
            db.commit()
        return schemas.LogGroupOut(name=payload.log_group)

    if row:
        row.label = label
    else:
        db.add(AwsLogLabel(account_id=account.id, log_group=payload.log_group, label=label))
    db.commit()
    log_action(db, admin.email, "aws_log_label.set", target=payload.log_group, detail=label)
    return schemas.LogGroupOut(name=payload.log_group, label=label)


@router.websocket("/aws-log-accounts/{account_id}/logs/ws")
async def aws_logs_ws(websocket: WebSocket, account_id: str, token: str, log_group: str):
    """Backlog then live tail, over the same socket shape the agent log viewer
    uses, so the browser side is the same component."""
    await websocket.accept()

    db = SessionLocal()
    try:
        user = get_user_from_token(token, db)
        if not user:
            await websocket.close(code=4401)
            return
        account = db.query(AwsLogAccount).filter(AwsLogAccount.id == account_id).first()
        if not account:
            await websocket.close(code=4404)
            return
        if user.role != "admin" and account.id not in {a.account_id for a in user.aws_log_access}:
            await websocket.close(code=4403)
            return
        # Detached copy: the polling thread outlives this session.
        connection = schemas.AwsLogAccountOut.model_validate(account)
    finally:
        db.close()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
    stop = threading.Event()

    def emit(line: str) -> None:
        # Called from the polling thread; hop back to the loop to enqueue.
        loop.call_soon_threadsafe(lambda: queue.put_nowait(line) if not queue.full() else None)

    def worker() -> None:
        try:
            for line in fetch_backlog(connection, log_group, limit=BACKLOG_LINES):
                emit(line)
        except CloudWatchError as e:
            emit(f"[InfraWatch] {e}")
            return
        tail(connection, log_group, stop, emit)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def pump() -> None:
        while True:
            line = await queue.get()
            await websocket.send_text(line)

    pumping = asyncio.create_task(pump())
    try:
        while True:
            await websocket.receive_text()  # the browser sends nothing; this waits for disconnect
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stop.set()
        pumping.cancel()
