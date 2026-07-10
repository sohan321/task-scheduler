import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta

from prometheus_client import start_http_server
from sqlalchemy import text

from database import SessionLocal, Base, engine
from job_queue import (
    dequeue_job,
    has_lease,
    mark_inflight,
    clear_inflight,
    migrate_legacy_ready_queue,
    pop_expired_leases,
    promote_due_retries,
    promote_due_scheduled,
    requeue,
    schedule_retry,
)
from logging_config import configure_logging
from metrics import (
    DEAD_LETTERS_TOTAL,
    JOB_DURATION_SECONDS,
    JOBS_PROCESSED_TOTAL,
    LEASES_EXPIRED_TOTAL,
    ORPHANS_RECLAIMED_TOTAL,
    RETRIES_SCHEDULED_TOTAL,
)
from models import DeadLetterJob, Job, JobStatus

MIN_WORK_SECONDS = float(os.environ.get("MIN_WORK_SECONDS", "1"))
MAX_WORK_SECONDS = float(os.environ.get("MAX_WORK_SECONDS", "4"))
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0.2"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
BASE_BACKOFF_SECONDS = float(os.environ.get("BASE_BACKOFF_SECONDS", "2"))
MAX_BACKOFF_SECONDS = float(os.environ.get("MAX_BACKOFF_SECONDS", "60"))
LEASE_SECONDS = float(os.environ.get("LEASE_SECONDS", "30"))
SCHEDULER_INTERVAL_SECONDS = float(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "1"))
PENDING_ORPHAN_GRACE_SECONDS = float(
    os.environ.get("PENDING_ORPHAN_GRACE_SECONDS", str(MAX_BACKOFF_SECONDS + 60))
)
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))

configure_logging()
log = logging.getLogger("worker")


def run_schema_migrations(engine):
    # create_all() only creates missing tables/types; on a database whose
    # jobstatus enum already existed pre-Phase-3, it never adds new labels.
    with engine.connect() as conn:
        conn.execute(text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'dead_letter'"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0"))
        conn.commit()


def claim_job(db, job_id):
    # Load-bearing, not redundant with Redis: BLPOP only guarantees a given
    # list *element* goes to one caller, not that a job_id *value* is never
    # enqueued twice (enqueue_job does no dedup). This status=pending filter
    # + row lock is what actually stops a second caller from reprocessing it.
    job = (
        db.query(Job)
        .filter(Job.id == job_id, Job.status == JobStatus.pending)
        .with_for_update(skip_locked=True)
        .first()
    )
    if job:
        job.status = JobStatus.running
        job.attempts += 1
        db.commit()
        db.refresh(job)
    return job


def simulate_work():
    duration = random.uniform(MIN_WORK_SECONDS, MAX_WORK_SECONDS)
    time.sleep(duration)
    if random.random() < FAILURE_RATE:
        raise RuntimeError("simulated failure")
    return {"message": "job completed", "duration_seconds": round(duration, 2)}


def backoff_delay(attempts):
    capped = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)))
    return random.uniform(0, capped)


def dead_letter(db, job, error):
    job.status = JobStatus.dead_letter
    job.result = {"error": error}
    db.add(
        DeadLetterJob(
            job_id=job.id,
            payload=job.payload,
            attempts=job.attempts,
            last_error={"error": error},
        )
    )
    db.commit()
    DEAD_LETTERS_TOTAL.inc()
    JOBS_PROCESSED_TOTAL.labels(outcome="dead_letter").inc()
    log.error(
        "job dead-lettered",
        extra={"job_id": str(job.id), "attempts": job.attempts, "error": error},
    )


def fail_attempt(db, job, error):
    if job.attempts >= MAX_ATTEMPTS:
        dead_letter(db, job, error)
        return
    delay = backoff_delay(job.attempts)
    job.status = JobStatus.pending
    job.result = {"error": error}
    db.commit()
    schedule_retry(str(job.id), job.priority, delay)
    RETRIES_SCHEDULED_TOTAL.inc()
    JOBS_PROCESSED_TOTAL.labels(outcome="retry").inc()
    log.warning(
        "job failed, retrying",
        extra={
            "job_id": str(job.id),
            "attempt": job.attempts,
            "max_attempts": MAX_ATTEMPTS,
            "retry_delay_seconds": round(delay, 1),
            "error": error,
        },
    )


def _heartbeat_loop(job_id, attempt, stop_event):
    interval = max(LEASE_SECONDS / 3, 1)
    while not stop_event.wait(interval):
        mark_inflight(job_id, attempt, LEASE_SECONDS)


def process_job(db, job):
    log.info("processing job", extra={"job_id": str(job.id), "attempt": job.attempts})
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop, args=(str(job.id), job.attempts, stop_heartbeat), daemon=True
    )
    heartbeat.start()
    start = time.perf_counter()
    try:
        mark_inflight(str(job.id), job.attempts, LEASE_SECONDS)
        result = simulate_work()
        job.status = JobStatus.success
        job.result = result
        db.commit()
        JOBS_PROCESSED_TOTAL.labels(outcome="success").inc()
        log.info(
            "job succeeded",
            extra={"job_id": str(job.id), "attempt": job.attempts, "duration_seconds": result["duration_seconds"]},
        )
    except Exception as exc:
        fail_attempt(db, job, str(exc))
    finally:
        JOB_DURATION_SECONDS.observe(time.perf_counter() - start)
        stop_heartbeat.set()
        heartbeat.join(timeout=1)
        clear_inflight(str(job.id), job.attempts)


def reap_expired_leases(db):
    for job_id, attempt in pop_expired_leases():
        job = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status == JobStatus.running, Job.attempts == attempt)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            # already finished, or superseded by a newer attempt
            continue
        LEASES_EXPIRED_TOTAL.inc()
        log.warning("job lease expired", extra={"job_id": str(job.id), "attempt": job.attempts})
        fail_attempt(db, job, "lease expired: worker crashed or exceeded visibility timeout")


def sweep_stale_jobs(db):
    # Backstop for the window between a Postgres commit and the matching Redis
    # write (claim -> mark_inflight, retry/reap -> schedule_retry/requeue): if
    # that write never lands (crash, transient Redis error), the row is stuck
    # with no Redis entry and no other mechanism will ever revisit it.
    running_cutoff = datetime.utcnow() - timedelta(seconds=LEASE_SECONDS)
    stale_running = (
        db.query(Job)
        .filter(Job.status == JobStatus.running, Job.updated_at < running_cutoff)
        .with_for_update(skip_locked=True)
        .all()
    )
    for job in stale_running:
        if has_lease(str(job.id), job.attempts):
            # under normal lease management: fresh, heartbeat-renewed, or
            # about to be picked up by reap_expired_leases
            continue
        ORPHANS_RECLAIMED_TOTAL.inc()
        log.warning("job orphaned (running, no lease), reclaiming", extra={"job_id": str(job.id)})
        fail_attempt(db, job, "running with no active lease (orphan sweep)")

    now = datetime.utcnow()
    pending_cutoff = now - timedelta(seconds=PENDING_ORPHAN_GRACE_SECONDS)
    # A pending row is only an orphan candidate if it's BOTH stale (untouched
    # past the grace period - the actual orphan signal) AND not intentionally
    # waiting on a future run_at (updated_at never changes after creation, so
    # a legitimately-delayed job would otherwise look "stale" long before
    # it's due). These are two separate questions kept as one query for a
    # single Postgres round-trip; don't conflate changes to one with the other.
    is_stale = Job.updated_at < pending_cutoff
    is_due_to_run = (Job.run_at.is_(None)) | (Job.run_at <= now)
    stale_pending = (
        db.query(Job)
        .filter(Job.status == JobStatus.pending, is_stale, is_due_to_run)
        .all()
    )
    for job in stale_pending:
        ORPHANS_RECLAIMED_TOTAL.inc()
        log.warning("job orphaned (pending, stale), re-enqueueing", extra={"job_id": str(job.id)})
        # requeue() only touches Redis - without bumping updated_at here too,
        # this job keeps matching is_stale on every future tick until a
        # worker claims it, re-firing the counter and re-enqueueing it once
        # per second for as long as the queue is backed up.
        job.updated_at = datetime.utcnow()
        db.commit()
        requeue(str(job.id), job.priority)


def scheduler_loop():
    # When manually verifying the queue drains after a test run, check
    # jobs:scheduled alongside jobs:ready/inflight/retry - a stuck delayed
    # job accumulates there silently since nothing else surfaces it.
    while True:
        try:
            promote_due_retries()
            promote_due_scheduled()
            db = SessionLocal()
            try:
                reap_expired_leases(db)
                sweep_stale_jobs(db)
            finally:
                db.close()
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def main():
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    migrate_legacy_ready_queue()
    start_http_server(METRICS_PORT)
    log.info("worker starting, waiting for jobs on the queue", extra={"metrics_port": METRICS_PORT})
    threading.Thread(target=scheduler_loop, daemon=True).start()
    while True:
        job_id = dequeue_job()
        if not job_id:
            continue
        db = SessionLocal()
        try:
            job = claim_job(db, job_id)
            if job:
                process_job(db, job)
            else:
                log.warning("job not found or already claimed", extra={"job_id": job_id})
        except Exception:
            log.exception("unhandled error processing job", extra={"job_id": job_id})
        finally:
            db.close()


if __name__ == "__main__":
    main()
