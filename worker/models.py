# Kept in sync by hand with api/models.py (separate Docker build contexts,
# same Postgres schema) - a change here needs the identical edit there too.
import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from database import Base

RESIZE_IMAGE_TYPE = "resize_image"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"  # unreachable in current worker logic; kept for rows/consumers predating retry+dead-letter
    dead_letter = "dead_letter"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payload = Column(JSON, nullable=False)
    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.pending)
    attempts = Column(Integer, nullable=False, default=0)
    result = Column(JSON, nullable=True)
    run_at = Column(DateTime, nullable=True)
    priority = Column(Integer, nullable=False, default=0)
    webhook_url = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class DeadLetterJob(Base):
    __tablename__ = "dead_letter_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), nullable=False)
    payload = Column(JSON, nullable=False)
    attempts = Column(Integer, nullable=False)
    last_error = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WebhookStatus(str, enum.Enum):
    pending = "pending"
    success = "success"
    dead_letter = "dead_letter"


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), nullable=False)
    url = Column(String, nullable=False)
    event = Column(String, nullable=False)  # "job.success" or "job.dead_letter"
    payload = Column(JSON, nullable=False)
    status = Column(Enum(WebhookStatus), nullable=False, default=WebhookStatus.pending)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
