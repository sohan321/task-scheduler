import os
import time

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# READY_KEY must match api/job_queue.py's READY_KEY exactly - it's the same
# Redis sorted set, enqueued by the API and consumed here via BZPOPMIN.
READY_KEY = "jobs:ready"
INFLIGHT_KEY = "jobs:inflight"
RETRY_KEY = "jobs:retry"
# SCHEDULED_KEY must match api/job_queue.py's SCHEDULED_KEY - holds jobs
# whose run_at is still in the future, keyed by "job_id:priority".
SCHEDULED_KEY = "jobs:scheduled"
# SEQUENCE_KEY must match api/job_queue.py's SEQUENCE_KEY - a shared counter
# both services draw from so ties are structurally impossible (see below).
SEQUENCE_KEY = "jobs:ready:seq"
BZPOPMIN_TIMEOUT_SECONDS = 5

# Ready-queue score: priority dominates (higher priority pops first). The
# tie-breaker is a Redis-atomic INCR, not a wall-clock timestamp - two
# same-priority jobs can never tie (unlike time.time(), whose resolution
# can coincide across back-to-back calls, especially in a tight promotion
# loop), so same-priority ordering is exactly FIFO by call order. Keeps
# ordering exact for priority roughly within +/-10,000 (float64 has ~15-17
# significant digits; this weight leaves headroom below 2**53).
PRIORITY_WEIGHT = 100_000_000_000

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def _next_sequence():
    return redis_client.incr(SEQUENCE_KEY)


def _ready_score(priority):
    return -priority * PRIORITY_WEIGHT + _next_sequence()


def dequeue_job():
    result = redis_client.bzpopmin(READY_KEY, timeout=BZPOPMIN_TIMEOUT_SECONDS)
    if result is None:
        return None
    _, job_id, _score = result
    return job_id


def requeue(job_id, priority=0):
    redis_client.zadd(READY_KEY, {job_id: _ready_score(priority)})


# Pre-Phase-4 key: the ready queue used to be a plain LIST under this name,
# consumed via BLPOP. Drain any leftovers from an in-flight deploy transition
# so they aren't silently stranded once the worker only reads READY_KEY.
LEGACY_READY_LIST_KEY = "jobs:pending"


def migrate_legacy_ready_queue():
    while True:
        job_id = redis_client.lpop(LEGACY_READY_LIST_KEY)
        if job_id is None:
            break
        requeue(job_id, 0)


def _lease_member(job_id, attempt):
    return f"{job_id}:{attempt}"


def mark_inflight(job_id, attempt, lease_seconds):
    redis_client.zadd(INFLIGHT_KEY, {_lease_member(job_id, attempt): time.time() + lease_seconds})


def clear_inflight(job_id, attempt):
    redis_client.zrem(INFLIGHT_KEY, _lease_member(job_id, attempt))


def has_lease(job_id, attempt):
    return redis_client.zscore(INFLIGHT_KEY, _lease_member(job_id, attempt)) is not None


def pop_expired_leases():
    leases = []
    for member in _pop_due(INFLIGHT_KEY):
        job_id, _, attempt = member.rpartition(":")
        leases.append((job_id, int(attempt)))
    return leases


def schedule_retry(job_id, priority, delay_seconds):
    redis_client.zadd(RETRY_KEY, {f"{job_id}:{priority}": time.time() + delay_seconds})


def promote_due_retries():
    _promote_due_priority(RETRY_KEY)


def promote_due_scheduled():
    _promote_due_priority(SCHEDULED_KEY)


def _promote_due_priority(key):
    for member in _pop_due(key):
        job_id, _, priority = member.rpartition(":")
        requeue(job_id, int(priority))


def _pop_due(key):
    due = redis_client.zrangebyscore(key, 0, time.time())
    popped = []
    for member in due:
        if redis_client.zrem(key, member):
            popped.append(member)
    return popped


# Webhook delivery queue: a plain list (not a priority sorted set like
# READY_KEY) since delivery order across different jobs' webhooks doesn't
# matter the way job dispatch order does.
WEBHOOK_READY_KEY = "webhooks:ready"
WEBHOOK_RETRY_KEY = "webhooks:retry"
BLPOP_TIMEOUT_SECONDS = 5


def enqueue_webhook_delivery(delivery_id):
    redis_client.rpush(WEBHOOK_READY_KEY, str(delivery_id))


def dequeue_webhook_delivery():
    result = redis_client.blpop(WEBHOOK_READY_KEY, timeout=BLPOP_TIMEOUT_SECONDS)
    if result is None:
        return None
    _, delivery_id = result
    return delivery_id


def schedule_webhook_retry(delivery_id, delay_seconds):
    redis_client.zadd(WEBHOOK_RETRY_KEY, {str(delivery_id): time.time() + delay_seconds})


def promote_due_webhook_retries():
    for delivery_id in _pop_due(WEBHOOK_RETRY_KEY):
        redis_client.rpush(WEBHOOK_READY_KEY, delivery_id)
