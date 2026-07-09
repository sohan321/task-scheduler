import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Must match worker/job_queue.py's READY_KEY exactly - it's the same Redis
# list, consumed by the worker via BLPOP.
JOB_QUEUE_KEY = "jobs:pending"

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def enqueue_job(job_id):
    # No dedup here by design: the worker's claim_job (status=pending filter +
    # row lock) is what prevents double-processing if a job_id is ever pushed
    # more than once, not this function.
    redis_client.rpush(JOB_QUEUE_KEY, str(job_id))
