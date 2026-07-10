# Kept in sync by hand with api/logging_config.py (separate Docker build
# contexts) - a change here needs the identical edit there too.
import json
import logging
import time

# Fields present on every LogRecord regardless of the call site; anything
# else in record.__dict__ came from extra={...} and should be surfaced.
_RESERVED_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {"message"}


class JSONFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_FIELDS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
