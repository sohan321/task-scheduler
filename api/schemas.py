from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


class JobCreate(BaseModel):
    payload: dict[str, Any]


class JobResponse(BaseModel):
    id: UUID
    payload: dict[str, Any]
    status: str
    attempts: int
    result: Optional[dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
