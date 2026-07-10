from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func
from sqlalchemy.orm import Session

from job_queue import READY_KEY, SCHEDULED_KEY, redis_client
from models import Job, JobStatus

# worker-owned queue keys - the API doesn't manage leases/retries, but the
# depth gauges below report on them too, so the raw key names are mirrored
# here rather than importing worker/job_queue.py across the build-context
# boundary.
# INFLIGHT_KEY must match worker/job_queue.py's INFLIGHT_KEY exactly.
INFLIGHT_KEY = "jobs:inflight"
# RETRY_KEY must match worker/job_queue.py's RETRY_KEY exactly.
RETRY_KEY = "jobs:retry"
# WEBHOOK_READY_KEY must match worker/job_queue.py's WEBHOOK_READY_KEY
# exactly - it's a plain list (LLEN, not ZCARD).
WEBHOOK_READY_KEY = "webhooks:ready"
# WEBHOOK_RETRY_KEY must match worker/job_queue.py's WEBHOOK_RETRY_KEY exactly.
WEBHOOK_RETRY_KEY = "webhooks:retry"

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds", "HTTP request duration in seconds", ["method", "path"]
)
JOBS_CREATED_TOTAL = Counter("jobs_created_total", "Total jobs created via POST /jobs")
JOBS_BY_STATUS = Gauge("jobs_by_status", "Current job count by status", ["status"])
QUEUE_DEPTH = Gauge("queue_depth", "Current queue depth by queue name", ["queue"])


def render_metrics(db: Session) -> bytes:
    counts = dict(db.query(Job.status, func.count(Job.id)).group_by(Job.status).all())
    for status in JobStatus:
        JOBS_BY_STATUS.labels(status=status.value).set(counts.get(status, 0))

    pipe = redis_client.pipeline()
    pipe.zcard(READY_KEY)
    pipe.zcard(SCHEDULED_KEY)
    pipe.zcard(INFLIGHT_KEY)
    pipe.zcard(RETRY_KEY)
    pipe.llen(WEBHOOK_READY_KEY)
    pipe.zcard(WEBHOOK_RETRY_KEY)
    ready, scheduled, inflight, retry, webhooks_ready, webhooks_retry = pipe.execute()

    QUEUE_DEPTH.labels(queue="ready").set(ready)
    QUEUE_DEPTH.labels(queue="scheduled").set(scheduled)
    QUEUE_DEPTH.labels(queue="inflight").set(inflight)
    QUEUE_DEPTH.labels(queue="retry").set(retry)
    QUEUE_DEPTH.labels(queue="webhooks_ready").set(webhooks_ready)
    QUEUE_DEPTH.labels(queue="webhooks_retry").set(webhooks_retry)

    return generate_latest()
