import logging
import os
import random
import time

from database import SessionLocal, Base, engine
from job_queue import dequeue_job
from models import Job, JobStatus

MIN_WORK_SECONDS = float(os.environ.get("MIN_WORK_SECONDS", "1"))
MAX_WORK_SECONDS = float(os.environ.get("MAX_WORK_SECONDS", "4"))
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0.2"))

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


def simulate_work(job):
    duration = random.uniform(MIN_WORK_SECONDS, MAX_WORK_SECONDS)
    time.sleep(duration)
    if random.random() < FAILURE_RATE:
        raise RuntimeError("simulated failure")
    return {"message": "job completed", "duration_seconds": round(duration, 2)}


def process_job(db, job):
    log.info("processing job %s (attempt %s)", job.id, job.attempts)
    try:
        result = simulate_work(job)
        job.status = JobStatus.success
        job.result = result
        log.info("job %s succeeded", job.id)
    except Exception as exc:
        job.status = JobStatus.failed
        job.result = {"error": str(exc)}
        log.warning("job %s failed: %s", job.id, exc)
    db.commit()


def main():
    Base.metadata.create_all(bind=engine)
    log.info("worker starting, waiting for jobs on the queue")
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
