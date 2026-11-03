"""Structured logging that never leaks credentials.

Provisioning carries a WiFi passphrase and pairing carries one-time codes. Both
are registered with :func:`redact` so a stray ``%s`` cannot put them in the
journal. ``systemd`` captures stdout, so this is the only place that has to be
careful.
"""

from __future__ import annotations

import json
import logging
import sys
import time

_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "***")
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, matching the engine's own log format."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("session_id", "client_id", "event", "detail"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else _RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # websockets is chatty at DEBUG and logs the full handshake headers.
    logging.getLogger("websockets").setLevel(logging.WARNING)
