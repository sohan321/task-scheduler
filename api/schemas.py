from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel

from models import JobStatus


class JobCreate(BaseModel):
    payload: dict[str, Any]


class JobResponse(BaseModel):
    id: UUID
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    result: Optional[dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
