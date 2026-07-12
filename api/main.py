import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import text
from sqlalchemy.orm import Session

import models
import schemas
from database import engine, get_db
from job_queue import enqueue_job, schedule_job
from logging_config import configure_logging
from metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    JOBS_CREATED_TOTAL,
    render_metrics,
)

configure_logging()
log = logging.getLogger("api")

IMAGE_OUTPUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR", "/data/images")

models.Base.metadata.create_all(bind=engine)

# create_all() only creates missing tables/types/columns on a fresh database;
# it never alters an already-existing enum or table.
with engine.connect() as _conn:
    _conn.execute(text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'dead_letter'"))
    _conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_at TIMESTAMP"))
    _conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0"))
    _conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS webhook_url VARCHAR"))
    _conn.commit()

app = FastAPI(title="Task Scheduler")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration = time.perf_counter() - start
        # Prefer the matched route template ("/jobs/{job_id}") over the raw
        # path so per-job UUIDs (or, on a 404, arbitrary scanner-supplied
        # paths from the public ALB) don't blow up label cardinality.
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        HTTP_REQUESTS_TOTAL.labels(request.method, path, status_code).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(request.method, path).observe(duration)
        log.info(
            "http request",
            extra={
                "http_method": request.method,
                "http_path": path,
                "http_status": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )


@app.get("/healthz")
def healthz():
    # Deliberately independent of Postgres/Redis - this is what the ALB
    # target group health-checks, so a DB/Redis blip shouldn't look like a
    # dead task and trigger ECS to cycle it. Use /metrics for a check that
    # exercises dependencies.
    return {"status": "ok"}


@app.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    return Response(content=render_metrics(db), media_type=CONTENT_TYPE_LATEST)


@app.post("/jobs", response_model=schemas.JobResponse, status_code=201)
def create_job(body: schemas.JobCreate, db: Session = Depends(get_db)):
    job = models.Job(
        payload=body.payload,
        run_at=body.run_at,
        priority=body.priority,
        webhook_url=body.webhook_url,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    if job.run_at and job.run_at > datetime.utcnow():
        # run_at is naive-UTC (see schemas._normalize_run_at); .timestamp()
        # on a naive datetime assumes *local* time, so tag it UTC explicitly
        # to get an epoch that agrees with time.time() elsewhere in the queue.
        run_at_epoch = job.run_at.replace(tzinfo=timezone.utc).timestamp()
        schedule_job(job.id, job.priority, run_at_epoch)
    else:
        enqueue_job(job.id, job.priority)
    JOBS_CREATED_TOTAL.inc()
    log.info("job created", extra={"job_id": str(job.id), "priority": job.priority, "run_at": job.run_at})
    return job


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found")


def _get_job_or_404(db: Session, job_id: str) -> models.Job:
    job_uuid = _parse_job_id(job_id)
    job = db.query(models.Job).filter(models.Job.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}", response_model=schemas.JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)):
    return _get_job_or_404(db, job_id)


@app.get("/jobs/{job_id}/output")
def get_job_output(job_id: str, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    if job.payload.get("type") != models.RESIZE_IMAGE_TYPE:
        raise HTTPException(status_code=404, detail="This job does not produce a downloadable output")
    if job.status != models.JobStatus.success or not isinstance(job.result, dict) or "filename" not in job.result:
        raise HTTPException(status_code=404, detail="No output file available for this job")
    # filename is worker-generated (uuid4 + ".png"), never taken from request
    # input, so there's no path-traversal surface in joining it here.
    path = os.path.join(IMAGE_OUTPUT_DIR, job.result["filename"])
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(path, media_type=job.result.get("content_type", "application/octet-stream"))


@app.get("/jobs/{job_id}/webhooks", response_model=list[schemas.WebhookDeliveryResponse])
def get_webhook_deliveries(job_id: str, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    return (
        db.query(models.WebhookDelivery)
        .filter(models.WebhookDelivery.job_id == job.id)
        .order_by(models.WebhookDelivery.created_at)
        .all()
    )
