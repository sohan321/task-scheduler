import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from database import SessionLocal, Base, engine
from job_queue import (
    dequeue_job,
    has_lease,
    mark_inflight,
    clear_inflight,
    pop_expired_leases,
    promote_due_retries,
    requeue,
    schedule_retry,
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
log = logging.getLogger("worker")


def run_schema_migrations(engine):
    # create_all() only creates missing tables/types; on a database whose
    # jobstatus enum already existed pre-Phase-3, it never adds new labels.
    with engine.connect() as conn:
        conn.execute(text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'dead_letter'"))
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
    log.error("job %s dead-lettered after %s attempts: %s", job.id, job.attempts, error)


def fail_attempt(db, job, error):
    if job.attempts >= MAX_ATTEMPTS:
        dead_letter(db, job, error)
        return
    delay = backoff_delay(job.attempts)
    job.status = JobStatus.pending
    job.result = {"error": error}
    db.commit()
    schedule_retry(str(job.id), delay)
    log.warning(
        "job %s failed (attempt %s/%s), retrying in %.1fs: %s",
        job.id, job.attempts, MAX_ATTEMPTS, delay, error,
    )


def _heartbeat_loop(job_id, attempt, stop_event):
    interval = max(LEASE_SECONDS / 3, 1)
    while not stop_event.wait(interval):
        mark_inflight(job_id, attempt, LEASE_SECONDS)


def process_job(db, job):
    log.info("processing job %s (attempt %s)", job.id, job.attempts)
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop, args=(str(job.id), job.attempts, stop_heartbeat), daemon=True
    )
    heartbeat.start()
    try:
        mark_inflight(str(job.id), job.attempts, LEASE_SECONDS)
        result = simulate_work()
        job.status = JobStatus.success
        job.result = result
        db.commit()
        log.info("job %s succeeded", job.id)
    except Exception as exc:
        fail_attempt(db, job, str(exc))
    finally:
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
        log.warning("job %s lease expired (attempt %s)", job.id, job.attempts)
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
        log.warning("job %s running with no lease entry, reclaiming (orphan sweep)", job.id)
        fail_attempt(db, job, "running with no active lease (orphan sweep)")

    pending_cutoff = datetime.utcnow() - timedelta(seconds=PENDING_ORPHAN_GRACE_SECONDS)
    stale_pending = (
        db.query(Job)
        .filter(Job.status == JobStatus.pending, Job.updated_at < pending_cutoff)
        .all()
    )
    for job in stale_pending:
        log.warning("job %s pending but stale, re-enqueueing (orphan sweep)", job.id)
        requeue(str(job.id))


def scheduler_loop():
    while True:
        try:
            promote_due_retries()
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
    log.info("worker starting, waiting for jobs on the queue")
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
                log.warning("job %s not found or already claimed", job_id)
        except Exception:
            log.exception("unhandled error processing job %s", job_id)
        finally:
            db.close()


if __name__ == "__main__":
    main()
