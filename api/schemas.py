from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator

from models import JobStatus


class JobCreate(BaseModel):
    payload: dict[str, Any]
    run_at: Optional[datetime] = None
    priority: int = 0

    @field_validator("run_at")
    @classmethod
    def _normalize_run_at(cls, value):
        # The rest of the codebase (datetime.utcnow(), Postgres TIMESTAMP
        # WITHOUT TIME ZONE) works in naive-UTC. A client-supplied run_at
        # with an explicit offset/'Z' would otherwise stay timezone-aware
        # and blow up comparisons against naive datetimes downstream.
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class JobResponse(BaseModel):
    id: UUID
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    result: Optional[dict[str, Any]]
    run_at: Optional[datetime]
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
