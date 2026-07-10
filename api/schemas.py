import ipaddress
import socket
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, field_validator

from models import JobStatus, WebhookStatus


def _is_blocked_address(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable - block conservatively
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class JobCreate(BaseModel):
    payload: dict[str, Any]
    run_at: Optional[datetime] = None
    priority: int = 0
    webhook_url: Optional[str] = None

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

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook_url(cls, value):
        if value is None:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("webhook_url must start with http:// or https://")
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("webhook_url must include a host")
        # The worker makes a server-side POST to this URL on job completion
        # (from inside the deploy's own network - e.g. an AWS ECS task, where
        # 169.254.169.254 is a real credentials endpoint) - block it from
        # targeting loopback/private/link-local/reserved addresses (SSRF).
        # Resolving here is a best-effort check at creation time, not a
        # guarantee against DNS rebinding between now and actual delivery.
        try:
            resolved_ip = socket.gethostbyname(hostname)
        except socket.gaierror:
            raise ValueError("webhook_url host could not be resolved")
        if _is_blocked_address(resolved_ip):
            raise ValueError("webhook_url may not target a local/internal address")
        return value


class JobResponse(BaseModel):
    id: UUID
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    result: Optional[dict[str, Any]]
    run_at: Optional[datetime]
    priority: int
    webhook_url: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WebhookDeliveryResponse(BaseModel):
    id: UUID
    job_id: UUID
    url: str
    event: str
    status: WebhookStatus
    attempts: int
    last_error: Optional[dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
