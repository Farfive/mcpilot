"""Audit sinks. Events carry no arguments, results, URLs or credentials (see AuditEvent)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .models import AuditEvent


class JsonlAuditSink:
    """Append-only JSON Lines file with mode 0600; one event per line, safe across threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def __call__(self, event: AuditEvent) -> None:
        line = (event.model_dump_json() + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
