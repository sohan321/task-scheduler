from prometheus_client import Counter, Histogram

JOBS_PROCESSED_TOTAL = Counter(
    "worker_jobs_processed_total", "Jobs processed by outcome", ["outcome"]
)
JOB_DURATION_SECONDS = Histogram(
    "worker_job_duration_seconds", "Job processing duration in seconds"
)
RETRIES_SCHEDULED_TOTAL = Counter(
    "worker_retries_scheduled_total", "Total retry attempts scheduled after a failure"
)
DEAD_LETTERS_TOTAL = Counter(
    "worker_dead_letters_total", "Total jobs dead-lettered after exhausting retries"
)
LEASES_EXPIRED_TOTAL = Counter(
    "worker_leases_expired_total", "Total leases reaped as expired (crashed/stalled workers)"
)
ORPHANS_RECLAIMED_TOTAL = Counter(
    "worker_orphans_reclaimed_total", "Total jobs reclaimed by the orphan sweep backstop"
)
