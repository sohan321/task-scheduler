from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from database import engine, get_db
from job_queue import enqueue_job

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Task Scheduler")


@app.post("/jobs", response_model=schemas.JobResponse, status_code=201)
def create_job(body: schemas.JobCreate, db: Session = Depends(get_db)):
    job = models.Job(payload=body.payload)
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue_job(job.id)
    return job


@app.get("/jobs/{job_id}", response_model=schemas.JobResponse)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
