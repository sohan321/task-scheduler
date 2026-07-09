import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, Integer, JSON
from sqlalchemy.dialects.postgresql import UUID

from database import Base


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    dead_letter = "dead_letter"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payload = Column(JSON, nullable=False)
    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.pending)
    attempts = Column(Integer, nullable=False, default=0)
    result = Column(JSON, nullable=True)
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
