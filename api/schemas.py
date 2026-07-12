import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from models import RESIZE_IMAGE_TYPE, JobStatus, WebhookStatus

MAX_RESIZE_TARGET_DIMENSION = 4000  # guards against a huge-allocation resize() bomb
DNS_RESOLVE_TIMEOUT_SECONDS = 3.0
# getaddrinfo() has no built-in timeout, and a slow/blackholed DNS server would
# otherwise block this request's FastAPI threadpool worker indefinitely.
# Resolving in a bounded background thread frees that worker after the
# timeout even if the OS resolver itself never returns; a global
# socket.setdefaulttimeout() was rejected because it isn't thread-local and
# would race with unrelated concurrent requests' DB/Redis sockets.
_dns_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-resolve")


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


def _resolve_all_ips(hostname: str):
    future = _dns_executor.submit(socket.getaddrinfo, hostname, None)
    try:
        return future.result(timeout=DNS_RESOLVE_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        raise ValueError(f"could not resolve host within {DNS_RESOLVE_TIMEOUT_SECONDS}s") from None


def _validate_public_url(value: str, field_name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field_name} must start with http:// or https://")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"{field_name} must include a host")
    # The worker makes a server-side request to this URL (POSTing a webhook,
    # or GETing an image to resize) - from inside the deploy's own network
    # (e.g. an AWS ECS task, where 169.254.169.254 is a real credentials
    # endpoint) - so block it from targeting loopback/private/link-local/
    # reserved addresses (SSRF). Resolving here is a best-effort check at
    # creation time, not a guarantee against DNS rebinding before delivery.
    try:
        resolved = _resolve_all_ips(hostname)
    except socket.gaierror:
        raise ValueError(f"{field_name} host could not be resolved")
    # getaddrinfo() (unlike gethostbyname(), which is IPv4-only) returns every
    # A and AAAA record - a hostname with both a public A record and a private
    # AAAA record must still be blocked, since the worker's actual connection
    # at fetch time may resolve to either family.
    if any(_is_blocked_address(info[4][0]) for info in resolved):
        raise ValueError(f"{field_name} may not target a local/internal address")
    return value


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
        return _validate_public_url(value, "webhook_url")

    @model_validator(mode="after")
    def _validate_resize_image_payload(self):
        if self.payload.get("type") != RESIZE_IMAGE_TYPE:
            return self
        image_url = self.payload.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            raise ValueError("resize_image payload requires a non-empty 'image_url' string")
        _validate_public_url(image_url, "image_url")
        target_size = self.payload.get("target_size")
        if (
            not isinstance(target_size, (list, tuple))
            or len(target_size) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in target_size)
        ):
            raise ValueError("resize_image payload requires 'target_size' as [width, height] positive integers")
        if any(n > MAX_RESIZE_TARGET_DIMENSION for n in target_size):
            raise ValueError(f"resize_image target_size dimensions must be <= {MAX_RESIZE_TARGET_DIMENSION}")
        return self


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
