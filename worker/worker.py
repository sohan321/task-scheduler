import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta

import requests
from prometheus_client import start_http_server
from sqlalchemy import text

from database import SessionLocal, Base, engine
from job_queue import (
    dequeue_job,
    dequeue_webhook_delivery,
    enqueue_webhook_delivery,
    has_lease,
    mark_inflight,
    clear_inflight,
    migrate_legacy_ready_queue,
    pop_expired_leases,
    promote_due_retries,
    promote_due_scheduled,
    promote_due_webhook_retries,
    requeue,
    schedule_retry,
    schedule_webhook_retry,
)
from logging_config import configure_logging
from metrics import (
    DEAD_LETTERS_TOTAL,
    JOB_DURATION_SECONDS,
    JOBS_PROCESSED_TOTAL,
    LEASES_EXPIRED_TOTAL,
    ORPHANS_RECLAIMED_TOTAL,
    RETRIES_SCHEDULED_TOTAL,
    WEBHOOK_DEAD_LETTERS_TOTAL,
    WEBHOOK_DELIVERY_DURATION_SECONDS,
    WEBHOOK_RETRIES_SCHEDULED_TOTAL,
    WEBHOOKS_DELIVERED_TOTAL,
)
from models import DeadLetterJob, Job, JobStatus, WebhookDelivery, WebhookStatus

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
WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "5"))
MAX_WEBHOOK_ATTEMPTS = int(os.environ.get("MAX_WEBHOOK_ATTEMPTS", "5"))
WEBHOOK_BASE_BACKOFF_SECONDS = float(os.environ.get("WEBHOOK_BASE_BACKOFF_SECONDS", "2"))
WEBHOOK_MAX_BACKOFF_SECONDS = float(os.environ.get("WEBHOOK_MAX_BACKOFF_SECONDS", "60"))
WEBHOOK_ORPHAN_GRACE_SECONDS = float(
    os.environ.get("WEBHOOK_ORPHAN_GRACE_SECONDS", str(WEBHOOK_MAX_BACKOFF_SECONDS + 60))
)

configure_logging()
log = logging.getLogger("worker")


def run_schema_migrations(engine):
    # create_all() only creates missing tables/types; on a database whose
    # jobstatus enum already existed pre-Phase-3, it never adds new labels.
    with engine.connect() as conn:
        conn.execute(text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'dead_letter'"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS webhook_url VARCHAR"))
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


def compute_backoff(attempt, base_seconds, max_seconds):
    capped = min(max_seconds, base_seconds * (2 ** (attempt - 1)))
    return random.uniform(0, capped)


def backoff_delay(attempts):
    return compute_backoff(attempts, BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS)


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
    # Every dead-letter path (exhausted retries, lease expiry, orphan sweep)
    # funnels through here, so this is the one place a "job.dead_letter"
    # webhook needs to fire from. Uses the safe wrapper so a webhook-queueing
    # failure can't propagate up through fail_attempt() and abort whatever
    # batch loop (reap_expired_leases/sweep_stale_jobs) called it.
    safe_queue_webhook_delivery(db, job, "job.dead_letter")


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


def _job_webhook_payload(job, event):
    return {
        "event": event,
        "job": {
            "id": str(job.id),
            "status": job.status.value,
            "attempts": job.attempts,
            "payload": job.payload,
            "result": job.result,
            "priority": job.priority,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        },
    }


def queue_webhook_delivery(db, job, event):
    if not job.webhook_url:
        return
    # Snapshot the payload now rather than re-serializing job state at send
    # time, so retries deliver an identical body regardless of what happens
    # to the job row afterward.
    delivery = WebhookDelivery(
        job_id=job.id,
        url=job.webhook_url,
        event=event,
        payload=_job_webhook_payload(job, event),
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    enqueue_webhook_delivery(delivery.id)
    log.info(
        "webhook delivery queued",
        extra={"job_id": str(job.id), "delivery_id": str(delivery.id), "event": event},
    )


def safe_queue_webhook_delivery(db, job, event):
    # Called after the job's own terminal state (success/dead_letter) is
    # already committed - a failure here (Redis blip, transient DB error)
    # must never propagate into the caller's exception handling, or it gets
    # mistaken for the job itself failing (see process_job/dead_letter).
    # queue_webhook_delivery's own db.commit() failing here would also leave
    # `db` in a state needing rollback for subsequent use by the caller.
    try:
        queue_webhook_delivery(db, job, event)
    except Exception:
        db.rollback()
        log.exception(
            "failed to queue webhook delivery",
            extra={"job_id": str(job.id), "event": event},
        )


def dead_letter_webhook(db, delivery, error):
    delivery.status = WebhookStatus.dead_letter
    delivery.last_error = {"error": error}
    db.commit()
    WEBHOOK_DEAD_LETTERS_TOTAL.inc()
    WEBHOOKS_DELIVERED_TOTAL.labels(outcome="dead_letter").inc()
    log.error(
        "webhook dead-lettered",
        extra={
            "delivery_id": str(delivery.id),
            "job_id": str(delivery.job_id),
            "attempts": delivery.attempts,
            "error": error,
        },
    )


def fail_webhook_delivery(db, delivery, error):
    if delivery.attempts >= MAX_WEBHOOK_ATTEMPTS:
        dead_letter_webhook(db, delivery, error)
        return
    delay = compute_backoff(delivery.attempts, WEBHOOK_BASE_BACKOFF_SECONDS, WEBHOOK_MAX_BACKOFF_SECONDS)
    delivery.last_error = {"error": error}
    db.commit()
    schedule_webhook_retry(str(delivery.id), delay)
    WEBHOOK_RETRIES_SCHEDULED_TOTAL.inc()
    WEBHOOKS_DELIVERED_TOTAL.labels(outcome="retry").inc()
    log.warning(
        "webhook delivery failed, retrying",
        extra={
            "delivery_id": str(delivery.id),
            "job_id": str(delivery.job_id),
            "attempt": delivery.attempts,
            "max_attempts": MAX_WEBHOOK_ATTEMPTS,
            "retry_delay_seconds": round(delay, 1),
            "error": error,
        },
    )


def deliver_webhook(db, delivery):
    delivery.attempts += 1
    start = time.perf_counter()
    # Delivery is at-least-once, not exactly-once: e.g. this POST can
    # succeed but the db.commit() below can still fail, which schedules a
    # resend of the same event. delivery_id/attempt are included so a
    # receiver can dedupe by delivery_id if that matters to it.
    body = {**delivery.payload, "delivery_id": str(delivery.id), "attempt": delivery.attempts}
    try:
        response = requests.post(delivery.url, json=body, timeout=WEBHOOK_TIMEOUT_SECONDS)
        response.raise_for_status()
        delivery.status = WebhookStatus.success
        delivery.last_error = None
        db.commit()
        WEBHOOKS_DELIVERED_TOTAL.labels(outcome="success").inc()
        log.info(
            "webhook delivered",
            extra={
                "delivery_id": str(delivery.id),
                "job_id": str(delivery.job_id),
                "attempt": delivery.attempts,
                "status_code": response.status_code,
            },
        )
    except Exception as exc:
        fail_webhook_delivery(db, delivery, str(exc))
    finally:
        WEBHOOK_DELIVERY_DURATION_SECONDS.observe(time.perf_counter() - start)


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
    succeeded = False
    try:
        mark_inflight(str(job.id), job.attempts, LEASE_SECONDS)
        result = simulate_work()
        job.status = JobStatus.success
        job.result = result
        db.commit()
        succeeded = True
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
    # Outside the try/except: the job's own success is already committed by
    # this point, so a webhook-queueing failure must never be able to route
    # back into fail_attempt() and overwrite that already-final outcome.
    if succeeded:
        safe_queue_webhook_delivery(db, job, "job.success")


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


def sweep_stale_webhook_deliveries(db):
    # Same class of gap sweep_stale_jobs() closes for jobs, applied to
    # webhook_deliveries: a row can be committed to Postgres but never make
    # it onto webhooks:ready (crash/Redis error between queue_webhook_delivery's
    # commit and its enqueue_webhook_delivery call), or be popped off
    # webhooks:ready and then lost if the worker crashes before deliver_webhook
    # commits a final status. Either way the row is stuck at pending with
    # nothing in Redis pointing to it, so nothing else will ever revisit it.
    cutoff = datetime.utcnow() - timedelta(seconds=WEBHOOK_ORPHAN_GRACE_SECONDS)
    stale = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.status == WebhookStatus.pending, WebhookDelivery.updated_at < cutoff)
        .all()
    )
    for delivery in stale:
        log.warning(
            "webhook delivery orphaned, re-enqueueing",
            extra={"delivery_id": str(delivery.id), "job_id": str(delivery.job_id)},
        )
        # Bump updated_at so this doesn't keep re-matching every tick before
        # a worker has a chance to actually claim and resolve it - same
        # reasoning as sweep_stale_jobs's pending-orphan branch.
        delivery.updated_at = datetime.utcnow()
        db.commit()
        enqueue_webhook_delivery(delivery.id)


def scheduler_loop():
    # When manually verifying the queue drains after a test run, check
    # jobs:scheduled alongside jobs:ready/inflight/retry - a stuck delayed
    # job accumulates there silently since nothing else surfaces it.
    while True:
        try:
            promote_due_retries()
            promote_due_scheduled()
            promote_due_webhook_retries()
            db = SessionLocal()
            try:
                reap_expired_leases(db)
                sweep_stale_jobs(db)
                sweep_stale_webhook_deliveries(db)
            finally:
                db.close()
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def webhook_delivery_loop():
    while True:
        try:
            delivery_id = dequeue_webhook_delivery()
        except Exception:
            # Unlike main()'s job loop (whose thread crashing takes down the
            # whole process and gets restarted by the container runtime),
            # this runs as a daemon thread - an uncaught exception here would
            # silently kill webhook delivery for the rest of this process's
            # life with no restart and no visible signal beyond a stderr
            # traceback. Must never be allowed to escape the loop.
            log.exception("webhook delivery dequeue failed")
            time.sleep(1)
            continue
        if not delivery_id:
            continue
        db = SessionLocal()
        try:
            # No row lock: webhooks:ready (a list, popped atomically by
            # BLPOP) and webhooks:retry (a zset keyed by delivery_id, so a
            # given delivery can't appear twice) structurally can't enqueue
            # the same delivery_id concurrently, unlike jobs:ready. A plain
            # status=pending filter is enough, and avoids holding a Postgres
            # row lock/open transaction for the duration of the HTTP call in
            # deliver_webhook() below.
            delivery = (
                db.query(WebhookDelivery)
                .filter(WebhookDelivery.id == delivery_id, WebhookDelivery.status == WebhookStatus.pending)
                .first()
            )
            if delivery:
                deliver_webhook(db, delivery)
            else:
                log.warning("webhook delivery not found or already resolved", extra={"delivery_id": delivery_id})
        except Exception:
            log.exception("unhandled error delivering webhook", extra={"delivery_id": delivery_id})
        finally:
            db.close()


def main():
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    migrate_legacy_ready_queue()
    start_http_server(METRICS_PORT)
    log.info("worker starting, waiting for jobs on the queue", extra={"metrics_port": METRICS_PORT})
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=webhook_delivery_loop, daemon=True).start()
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
