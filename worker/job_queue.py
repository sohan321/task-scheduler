import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_QUEUE_KEY = "jobs:pending"
BLPOP_TIMEOUT_SECONDS = 5

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def dequeue_job():
    result = redis_client.blpop(JOB_QUEUE_KEY, timeout=BLPOP_TIMEOUT_SECONDS)
    if result is None:
        return None
    _, job_id = result
    return job_id
