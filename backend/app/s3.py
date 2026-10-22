"""Reading S3 inventory and posture from the same accounts as the logs.

Reuses cloudwatch.session_for deliberately: these are the same AWS accounts,
the same roles and the same temporary credentials, so there is one place that
assumes a role rather than two that can drift apart.

Nothing here reads object data. Sizes come from the metrics AWS already
publishes daily, and posture from bucket configuration calls, so listing a
bucket holding a billion objects costs exactly what an empty one costs.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .cloudwatch import CloudWatchError, _friendly, session_for

# A bucket listing is several calls per bucket, and the numbers behind it only
# move once a day, so the whole page is cached rather than re-fetched on every
# visit. Short enough that a posture fix shows up while someone is still
# looking at the screen.
_CACHE_TTL = 10 * 60
_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()

_CLIENT_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20)

# Bucket calls are independent and almost entirely latency, so they overlap.
# Kept modest: this runs inside a request thread, and AWS throttles per account.
_WORKERS = 8

# GetMetricData takes up to 500 queries per call; two per bucket, left well
# under the limit so a region with many buckets still batches cleanly.
_METRIC_BATCH = 100


class S3Error(CloudWatchError):
    """Same shape as the logs errors, so routers handle one exception type."""


def forget(connection_id: str) -> None:
    """Drop the cached listing for an account whose settings changed."""
    with _lock:
        _cache.pop(str(connection_id), None)


def _client(connection, service: str, region: str | None = None):
    kwargs = {"config": _CLIENT_CONFIG}
    if region:
        kwargs["region_name"] = region
    return session_for(connection).client(service, **kwargs)


def _iso(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


# --------------------------------------------------------------- posture ----

def _public_access(s3, name: str) -> tuple[bool | None, bool | None]:
    """(fully blocked, policy makes it public) — either may be unknown.

    Unknown is not the same as bad: a role without s3:GetBucketPolicyStatus
    must not make a locked-down bucket look exposed, so the caller is told it
    could not tell rather than being handed a guess.
    """
    blocked = None
    try:
        cfg = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
        blocked = all(cfg.get(k) for k in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # No block configuration at all is a real answer: nothing is blocked.
        blocked = False if code == "NoSuchPublicAccessBlockConfiguration" else None
    except BotoCoreError:
        blocked = None

    policy_public = None
    try:
        policy_public = bool(s3.get_bucket_policy_status(Bucket=name)["PolicyStatus"]["IsPublic"])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # No policy means no policy-granted access, which is a real answer too.
        policy_public = False if code == "NoSuchBucketPolicy" else None
    except BotoCoreError:
        policy_public = None

    return blocked, policy_public


def _encryption(s3, name: str) -> str | None:
    """The SSE algorithm, or "none". None means we were not allowed to look."""
    try:
        rules = s3.get_bucket_encryption(Bucket=name)["ServerSideEncryptionConfiguration"]["Rules"]
        for rule in rules:
            algo = rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
            if algo:
                return algo
        return "none"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ServerSideEncryptionConfigurationNotFoundError":
            return "none"
        return None
    except BotoCoreError:
        return None


def _versioning(s3, name: str) -> str | None:
    try:
        # An absent Status has never had versioning turned on, which reads
        # better as "disabled" than as a missing value.
        return s3.get_bucket_versioning(Bucket=name).get("Status") or "Disabled"
    except (ClientError, BotoCoreError):
        return None


def _region(s3, name: str) -> str:
    try:
        # us-east-1 is reported as null for historical reasons.
        return s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
    except (ClientError, BotoCoreError):
        return "us-east-1"


def _inspect(connection, name: str) -> dict:
    s3 = _client(connection, "s3")
    region = _region(s3, name)
    # Bucket config calls must go to the bucket's own region or S3 answers
    # with a redirect that boto3 surfaces as a failure.
    regional = _client(connection, "s3", region) if region else s3
    blocked, policy_public = _public_access(regional, name)
    return {
        "name": name,
        "region": region,
        "block_public_access": blocked,
        "policy_public": policy_public,
        "encryption": _encryption(regional, name),
        "versioning": _versioning(regional, name),
    }


# --------------------------------------------------------------- metrics ----

def _storage_metrics(connection, region: str, names: list[str]) -> dict[str, dict]:
    """Size and object count from the daily metrics AWS publishes for free.

    These are reported once a day, so the window reaches back far enough to
    survive a bucket whose figures have not been refreshed yet, and the most
    recent datapoint wins.
    """
    out: dict[str, dict] = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=3)

    for batch in [names[i:i + _METRIC_BATCH] for i in range(0, len(names), _METRIC_BATCH)]:
        queries = []
        for n, name in enumerate(batch):
            for label, metric, storage in (
                ("size", "BucketSizeBytes", "StandardStorage"),
                ("count", "NumberOfObjects", "AllStorageTypes"),
            ):
                queries.append({
                    "Id": f"{label}_{n}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/S3",
                            "MetricName": metric,
                            "Dimensions": [
                                {"Name": "BucketName", "Value": name},
                                {"Name": "StorageType", "Value": storage},
                            ],
                        },
                        "Period": 86400,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                })
        try:
            cw = _client(connection, "cloudwatch", region)
            results = cw.get_metric_data(
                MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampDescending",
            ).get("MetricDataResults", [])
        except (ClientError, BotoCoreError):
            # Metrics are a nice-to-have; posture is the part worth failing on.
            continue

        for result in results:
            label, _, index = result["Id"].partition("_")
            values, stamps = result.get("Values") or [], result.get("Timestamps") or []
            if not values:
                continue
            name = batch[int(index)]
            entry = out.setdefault(name, {})
            entry["size_bytes" if label == "size" else "object_count"] = int(values[0])
            entry["measured_at"] = _iso(stamps[0]) if stamps else None
    return out


# ------------------------------------------------------------------ public ----

def list_buckets(connection, refresh: bool = False) -> list[dict]:
    """Every bucket in the account, with its size, object count and posture."""
    key = str(connection.id)
    now = time.monotonic()
    if not refresh:
        with _lock:
            cached = _cache.get(key)
            if cached and cached[0] > now:
                return cached[1]

    try:
        s3 = _client(connection, "s3")
        listing = s3.list_buckets().get("Buckets", [])
    except (ClientError, BotoCoreError) as e:
        raise S3Error(_friendly(e)) from e

    created = {b["Name"]: _iso(b.get("CreationDate")) for b in listing}
    names = sorted(created)
    if not names:
        with _lock:
            _cache[key] = (now + _CACHE_TTL, [])
        return []

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        buckets = list(pool.map(lambda n: _inspect(connection, n), names))

    by_region: dict[str, list[str]] = {}
    for bucket in buckets:
        by_region.setdefault(bucket["region"], []).append(bucket["name"])

    metrics: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for part in pool.map(lambda item: _storage_metrics(connection, item[0], item[1]),
                             list(by_region.items())):
            metrics.update(part)

    for bucket in buckets:
        bucket["created"] = created.get(bucket["name"])
        bucket.update(metrics.get(bucket["name"], {}))
        bucket.setdefault("size_bytes", None)
        bucket.setdefault("object_count", None)
        bucket.setdefault("measured_at", None)
        bucket["issues"] = _issues(bucket)

    # Anything with a finding first: the page exists to surface those, and a
    # bucket needing attention should not be below the fold behind forty
    # healthy ones.
    buckets.sort(key=lambda b: (not b["issues"], b["name"]))
    with _lock:
        _cache[key] = (now + _CACHE_TTL, buckets)
    return buckets


def _issues(bucket: dict) -> list[str]:
    """Posture findings, worst first. Silence means checked and fine."""
    found = []
    if bucket.get("policy_public"):
        found.append("Public: the bucket policy grants access to everyone")
    elif bucket.get("block_public_access") is False:
        found.append("Public access is not fully blocked")
    if bucket.get("encryption") == "none":
        found.append("No default encryption")
    if bucket.get("versioning") == "Disabled":
        found.append("Versioning off — an overwrite or delete cannot be undone")
    return found


def _bucket_region(connection, bucket: str) -> str:
    """Prefer the region already learned by the listing over asking again."""
    with _lock:
        cached = _cache.get(str(connection.id))
    if cached:
        for entry in cached[1]:
            if entry["name"] == bucket:
                return entry["region"]
    return _region(_client(connection, "s3"), bucket)


def list_objects(connection, bucket: str, prefix: str = "",
                 token: str | None = None, limit: int = 1000) -> dict:
    """The folders and objects directly under one prefix.

    The delimiter is what makes this affordable: S3 collapses everything
    below the current level into a prefix entry, so a page of a bucket
    holding a hundred million objects costs exactly what a page of a bucket
    holding ten does. Walking the whole tree would not, which is why there
    is no call here that does.

    Deliberately uncached. Objects change under you, and a listing is cheap
    enough that serving a stale one would be the worse trade.
    """
    region = _bucket_region(connection, bucket)
    s3 = _client(connection, "s3", region)
    kwargs = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": max(1, min(limit, 1000))}
    if prefix:
        kwargs["Prefix"] = prefix
    if token:
        kwargs["ContinuationToken"] = token

    try:
        page = s3.list_objects_v2(**kwargs)
    except (ClientError, BotoCoreError) as e:
        raise S3Error(_friendly(e)) from e

    objects = []
    for item in page.get("Contents", []):
        key = item["Key"]
        # A zero-byte key ending in "/" is how the console represents a
        # folder. It already appears as a prefix, so listing it again as a
        # file would show the same folder twice.
        if key == prefix or (key.endswith("/") and item.get("Size") == 0):
            continue
        objects.append({
            "key": key,
            "name": key[len(prefix):],
            "size": item.get("Size"),
            "modified": _iso(item.get("LastModified")),
            "storage_class": item.get("StorageClass") or "STANDARD",
        })

    folders = []
    for entry in page.get("CommonPrefixes", []):
        full = entry["Prefix"]
        folders.append({"prefix": full, "name": full[len(prefix):].rstrip("/")})

    return {
        "bucket": bucket,
        "prefix": prefix,
        "region": region,
        "folders": folders,
        "objects": objects,
        "next_token": page.get("NextContinuationToken"),
        "truncated": bool(page.get("IsTruncated")),
    }
