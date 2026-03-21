"""Structured error logging with in-memory ring buffer.

Security notes:
- Filenames from users are sanitized (only alphanum, dots, hyphens, underscores)
- Server paths are stripped from tracebacks
- Endpoint requires API key via X-Log-Key header
"""

import os
import re
import time
import traceback
from collections import deque
from dataclasses import dataclass, asdict

MAX_ENTRIES = 100
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\-]")
_PATH_STRIP_RE = re.compile(r"(?:/[a-z0-9/_\-]+/|[A-Z]:\\[^\s:]+\\)", re.IGNORECASE)
LOG_API_KEY = os.environ.get("LOG_API_KEY", "")


@dataclass
class ErrorEntry:
    timestamp: float
    endpoint: str
    error_type: str
    error_message: str
    traceback_safe: str
    file_info: str
    extra: dict


_buffer: deque[ErrorEntry] = deque(maxlen=MAX_ENTRIES)


def _sanitize_filename(name: str) -> str:
    if not name:
        return "(empty)"
    safe = _SAFE_FILENAME_RE.sub("_", name)
    return safe[:80]


def _sanitize_traceback(tb: str) -> str:
    return _PATH_STRIP_RE.sub("[path]/", tb)[-2000:]


def log_error(
    endpoint: str,
    exception: Exception,
    filename: str = "",
    extra: dict | None = None,
):
    tb = traceback.format_exception(type(exception), exception, exception.__traceback__)
    tb_str = "".join(tb)

    entry = ErrorEntry(
        timestamp=time.time(),
        endpoint=endpoint,
        error_type=type(exception).__name__,
        error_message=str(exception)[:500],
        traceback_safe=_sanitize_traceback(tb_str),
        file_info=_sanitize_filename(filename),
        extra=extra or {},
    )
    _buffer.append(entry)


def get_recent(n: int = 20) -> list[dict]:
    entries = list(_buffer)[-n:]
    return [asdict(e) for e in entries]


def verify_key(key: str) -> bool:
    if not LOG_API_KEY:
        return False
    return key == LOG_API_KEY
