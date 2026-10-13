"""Reading CloudWatch Logs from other AWS accounts.

InfraWatch runs on EC2 with an instance role, and each account it reads from
has a read-only role trusting that one. So nothing here holds a long-lived
secret: boto3 picks up the instance role, assumes the target role, and the
credentials it gets back expire within the hour.

Fargate tasks have no host to put an agent on, which is why these logs are
pulled by the server rather than pushed by an agent like every other source.
"""
import threading
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# Assumed-role credentials last an hour; re-assuming on every call would add a
# round trip to each page of logs, so sessions are cached per account and
# retired early enough that none is ever used close to its expiry.
_SESSION_TTL = 45 * 60
_sessions: dict[str, tuple[float, boto3.Session]] = {}
_lock = threading.Lock()

_CLIENT_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20)


class CloudWatchError(Exception):
    """Anything AWS refused, phrased for an operator rather than a stack trace."""


def _friendly(e: Exception) -> str:
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        message = e.response.get("Error", {}).get("Message", str(e))
        if code == "AccessDenied":
            return f"Access denied — check the role's trust policy and external ID. ({message})"
        if code in ("ResourceNotFoundException",):
            return f"Not found — the log group may have been deleted. ({message})"
        if code in ("ExpiredTokenException", "InvalidClientTokenId"):
            return "Credentials expired or invalid — InfraWatch will retry on the next request."
        return f"{code}: {message}" if code else message
    return f"{type(e).__name__}: {e}"


def session_for(connection) -> boto3.Session:
    """A boto3 session holding temporary credentials for one account."""
    key = f"{connection.id}:{connection.role_arn}:{connection.region}"
    now = time.monotonic()
    with _lock:
        cached = _sessions.get(key)
        if cached and cached[0] > now:
            return cached[1]

    try:
        sts = boto3.client("sts", region_name=connection.region, config=_CLIENT_CONFIG)
        params = {
            "RoleArn": connection.role_arn,
            "RoleSessionName": "infrawatch",
            "DurationSeconds": 3600,
        }
        if connection.external_id:
            params["ExternalId"] = connection.external_id
        creds = sts.assume_role(**params)["Credentials"]
    except (ClientError, BotoCoreError) as e:
        raise CloudWatchError(_friendly(e)) from e

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=connection.region,
    )
    with _lock:
        _sessions[key] = (now + _SESSION_TTL, session)
    return session


def forget(connection_id: str) -> None:
    """Drop cached sessions for an account whose settings just changed."""
    with _lock:
        for key in [k for k in _sessions if k.startswith(f"{connection_id}:")]:
            _sessions.pop(key, None)


def check_connection(connection) -> dict:
    """Assume the role and read one page of log groups -- proves both the trust
    policy and the permissions, which fail for different reasons."""
    try:
        client = session_for(connection).client("logs", config=_CLIENT_CONFIG)
        groups = client.describe_log_groups(limit=5).get("logGroups", [])
        return {"ok": True, "detail": f"Connected. {len(groups)} log group(s) visible."}
    except CloudWatchError as e:
        return {"ok": False, "detail": str(e)}
    except (ClientError, BotoCoreError) as e:
        return {"ok": False, "detail": _friendly(e)}


def list_log_groups(connection, prefix: str | None = None, limit: int = 300) -> list[dict]:
    try:
        client = session_for(connection).client("logs", config=_CLIENT_CONFIG)
        paginator = client.get_paginator("describe_log_groups")
        kwargs = {"logGroupNamePrefix": prefix} if prefix else {}
        out = []
        for page in paginator.paginate(**kwargs):
            for group in page.get("logGroups", []):
                out.append({
                    "name": group["logGroupName"],
                    "stored_bytes": group.get("storedBytes"),
                    "retention_days": group.get("retentionInDays"),
                })
                if len(out) >= limit:
                    return out
        return out
    except (ClientError, BotoCoreError) as e:
        raise CloudWatchError(_friendly(e)) from e


def _format(event: dict, stream: str | None = None) -> str:
    """One CloudWatch event as a line, timestamped like the other log sources.

    The stream name is included because an ECS log group holds one stream per
    task, so without it a line gives no clue which task produced it.
    """
    ts = datetime.fromtimestamp(event["timestamp"] / 1000, tz=timezone.utc)
    name = stream or event.get("logStreamName") or ""
    short = name.rsplit("/", 1)[-1][:12] if name else ""
    prefix = f"{ts:%Y-%m-%d %H:%M:%S} " + (f"[{short}] " if short else "")
    return prefix + event.get("message", "").rstrip("\n")


def fetch_backlog(connection, log_group: str, limit: int = 3000, minutes: int = 180) -> list[str]:
    """Recent lines, oldest first, to fill the viewer when it opens.

    Bounded by time as well as count: FilterLogEvents bills by data scanned, so
    an open-ended sweep of a busy group would be slow and needlessly expensive.
    """
    try:
        client = session_for(connection).client("logs", config=_CLIENT_CONFIG)
        start = int((time.time() - minutes * 60) * 1000)
        events, token = [], None
        while len(events) < limit:
            kwargs = {"logGroupName": log_group, "startTime": start, "limit": min(10000, limit - len(events))}
            if token:
                kwargs["nextToken"] = token
            page = client.filter_log_events(**kwargs)
            events.extend(page.get("events", []))
            token = page.get("nextToken")
            if not token:
                break
        events.sort(key=lambda e: e["timestamp"])
        return [_format(e) for e in events[-limit:]]
    except (ClientError, BotoCoreError) as e:
        raise CloudWatchError(_friendly(e)) from e


def tail(connection, log_group: str, stop: threading.Event, on_line, poll_seconds: int = 3) -> None:
    """Follow a log group until `stop` is set, calling on_line for each line.

    Polls FilterLogEvents rather than using StartLiveTail: live tail bills per
    minute of an open session and is capped at a few concurrent sessions per
    account, which several people watching logs at once would exhaust. Polling
    costs only what it scans and has no such ceiling.
    """
    seen: set[str] = set()
    since = int((time.time() - 30) * 1000)
    while not stop.is_set():
        try:
            client = session_for(connection).client("logs", config=_CLIENT_CONFIG)
            page = client.filter_log_events(logGroupName=log_group, startTime=since, limit=1000)
            events = sorted(page.get("events", []), key=lambda e: e["timestamp"])
            for event in events:
                # Event ids rather than timestamps: several events can share a
                # millisecond, and advancing past them would lose lines.
                if event["eventId"] in seen:
                    continue
                seen.add(event["eventId"])
                on_line(_format(event))
                since = max(since, event["timestamp"])
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])
        except Exception as e:
            on_line(f"[InfraWatch] log stream error: {_friendly(e)}")
            stop.wait(10)
            continue
        stop.wait(poll_seconds)
