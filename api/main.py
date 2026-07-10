import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

import models
import schemas
from database import engine, get_db
from job_queue import enqueue_job, schedule_job

models.Base.metadata.create_all(bind=engine)

# create_all() only creates missing tables/types/columns on a fresh database;
# it never alters an already-existing enum or table.
with engine.connect() as _conn:
    _conn.execute(text("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'dead_letter'"))
    _conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_at TIMESTAMP"))
    _conn.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0"))
    _conn.commit()

app = FastAPI(title="Task Scheduler")


@app.post("/jobs", response_model=schemas.JobResponse, status_code=201)
def create_job(body: schemas.JobCreate, db: Session = Depends(get_db)):
    job = models.Job(payload=body.payload, run_at=body.run_at, priority=body.priority)
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
    return job


@app.get("/jobs/{job_id}", response_model=schemas.JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)):
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found")
    job = db.query(models.Job).filter(models.Job.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
