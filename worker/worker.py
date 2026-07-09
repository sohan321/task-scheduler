import logging
import os
import random
import threading
import time

from database import SessionLocal, Base, engine
from job_queue import (
    dequeue_job,
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
log = logging.getLogger("worker")


def claim_job(db, job_id):
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


def retry(db, job, error):
    delay = backoff_delay(job.attempts)
    job.status = JobStatus.pending
    job.result = {"error": error}
    db.commit()
    schedule_retry(str(job.id), delay)
    log.warning(
        "job %s failed (attempt %s/%s), retrying in %.1fs: %s",
        job.id, job.attempts, MAX_ATTEMPTS, delay, error,
    )


def process_job(db, job):
    log.info("processing job %s (attempt %s)", job.id, job.attempts)
    mark_inflight(str(job.id), LEASE_SECONDS)
    try:
        result = simulate_work()
        job.status = JobStatus.success
        job.result = result
        db.commit()
        log.info("job %s succeeded", job.id)
    except Exception as exc:
        if job.attempts >= MAX_ATTEMPTS:
            dead_letter(db, job, str(exc))
        else:
            retry(db, job, str(exc))
    finally:
        clear_inflight(str(job.id))


def reap_expired_leases(db):
    for job_id in pop_expired_leases():
        job = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status == JobStatus.running)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not job:
            continue
        log.warning("job %s lease expired (attempt %s)", job.id, job.attempts)
        if job.attempts >= MAX_ATTEMPTS:
            dead_letter(db, job, "lease expired: worker crashed or exceeded visibility timeout")
        else:
            job.status = JobStatus.pending
            db.commit()
            requeue(str(job.id))


def scheduler_loop():
    while True:
        promote_due_retries()
        db = SessionLocal()
        try:
            reap_expired_leases(db)
        finally:
            db.close()
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def main():
    Base.metadata.create_all(bind=engine)
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
        finally:
            db.close()


if __name__ == "__main__":
    main()
