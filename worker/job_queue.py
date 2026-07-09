import os
import time

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

READY_KEY = "jobs:pending"
INFLIGHT_KEY = "jobs:inflight"
RETRY_KEY = "jobs:retry"
BLPOP_TIMEOUT_SECONDS = 5

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def dequeue_job():
    result = redis_client.blpop(READY_KEY, timeout=BLPOP_TIMEOUT_SECONDS)
    if result is None:
        return None
    _, job_id = result
    return job_id


def requeue(job_id):
    redis_client.rpush(READY_KEY, job_id)


def mark_inflight(job_id, lease_seconds):
    redis_client.zadd(INFLIGHT_KEY, {job_id: time.time() + lease_seconds})


def clear_inflight(job_id):
    redis_client.zrem(INFLIGHT_KEY, job_id)


def pop_expired_leases():
    return _pop_due(INFLIGHT_KEY)


def schedule_retry(job_id, delay_seconds):
    redis_client.zadd(RETRY_KEY, {job_id: time.time() + delay_seconds})


def promote_due_retries():
    for job_id in _pop_due(RETRY_KEY):
        requeue(job_id)


def _pop_due(key):
    due = redis_client.zrangebyscore(key, 0, time.time())
    popped = []
    for job_id in due:
        if redis_client.zrem(key, job_id):
            popped.append(job_id)
    return popped
