import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JOB_QUEUE_KEY = "jobs:pending"

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def enqueue_job(job_id):
    redis_client.rpush(JOB_QUEUE_KEY, str(job_id))
