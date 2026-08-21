"""Resource-history storage: downsampling for the chart endpoint, and the
retention prune that keeps metric_samples from growing unbounded.

Write path lives in routers/agent.py (one MetricSample row per heartbeat) --
this module only reads and prunes.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import MetricSample

METRIC_RETENTION_DAYS = 8  # a bit more than the longest UI range (7d), so
                           # that range's oldest edge is never half-empty

RANGE_SPECS = {
    "1h": (timedelta(hours=1), 12),    # 5-min buckets
    "24h": (timedelta(hours=24), 24),  # 1-hour buckets
    "7d": (timedelta(days=7), 28),     # 6-hour buckets
}


def prune_old_metric_samples(db: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(days=METRIC_RETENTION_DAYS)
    deleted = db.query(MetricSample).filter(MetricSample.recorded_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return deleted


def bucketed_history(db: Session, vm_id: str, range_key: str):
    """Downsamples raw per-heartbeat samples into evenly-spaced buckets for
    charting -- averages whatever samples land in each bucket. A bucket with
    no samples is simply omitted (a freshly added VM has fewer points on the
    wider ranges, not zero-filled ones -- the frontend handles a short or
    empty series rather than this pretending data exists)."""
    window, bucket_count = RANGE_SPECS[range_key]
    since = datetime.utcnow() - window
    bucket_seconds = window.total_seconds() / bucket_count

    samples = (
        db.query(MetricSample)
        .filter(MetricSample.vm_id == vm_id, MetricSample.recorded_at >= since)
        .order_by(MetricSample.recorded_at)
        .all()
    )

    buckets = {}
    for s in samples:
        elapsed = (s.recorded_at - since).total_seconds()
        idx = min(int(elapsed // bucket_seconds), bucket_count - 1)
        buckets.setdefault(idx, []).append(s)

    def avg(bucket_samples, attr):
        vals = [getattr(s, attr) for s in bucket_samples if getattr(s, attr) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    points = []
    for idx in sorted(buckets):
        bucket_samples = buckets[idx]
        points.append({
            "t": since + timedelta(seconds=(idx + 0.5) * bucket_seconds),
            "cpu_percent": avg(bucket_samples, "cpu_percent"),
            "mem_percent": avg(bucket_samples, "mem_percent"),
            "disk_percent": avg(bucket_samples, "disk_percent"),
        })
    return points
