import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Must match worker/job_queue.py's READY_KEY exactly - it's the same Redis
# sorted set, consumed by the worker via BZPOPMIN.
READY_KEY = "jobs:ready"
# Must match worker/job_queue.py's SCHEDULED_KEY - holds jobs whose run_at is
# still in the future, keyed by "job_id:priority".
SCHEDULED_KEY = "jobs:scheduled"
# Must match worker/job_queue.py's SEQUENCE_KEY - see the comment there.
SEQUENCE_KEY = "jobs:ready:seq"

# Must match worker/job_queue.py's scoring exactly - see the comment there.
PRIORITY_WEIGHT = 100_000_000_000

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def _next_sequence():
    return redis_client.incr(SEQUENCE_KEY)


def _ready_score(priority):
    return -priority * PRIORITY_WEIGHT + _next_sequence()


def enqueue_job(job_id, priority=0):
    # No dedup here by design: the worker's claim_job (status=pending filter +
    # row lock) is what prevents double-processing if a job_id is ever pushed
    # more than once, not this function.
    redis_client.zadd(READY_KEY, {str(job_id): _ready_score(priority)})


def schedule_job(job_id, priority, run_at_epoch):
    redis_client.zadd(SCHEDULED_KEY, {f"{job_id}:{priority}": run_at_epoch})
