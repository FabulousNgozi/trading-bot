"""
logger_config.py — logging setup with token redaction.

Every log record passes through a redaction filter that scrubs anything that
looks like a Deriv token, an OTP, or an Authorization header. This is a
defence-in-depth measure: the code never intentionally logs a token, but a
stray ``repr()`` of a response object must not be able to leak one.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional

# Patterns that must never reach a log file or the terminal.
_REDACTIONS = [
    # Authorization: Bearer <token>
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.]{8,}", re.IGNORECASE), r"\1***REDACTED***"),
    # otp=... in a WebSocket URL
    (re.compile(r"(otp=)[A-Za-z0-9\-_\.]{4,}", re.IGNORECASE), r"\1***REDACTED***"),
    # "authorize": "..." / "token": "..." / "api_token": "..."
    (re.compile(r'("(?:authorize|token|api_token|access_token|otp)"\s*:\s*")[^"]{4,}"',
                re.IGNORECASE), r'\1***REDACTED***"'),
    # DERIV_API_TOKEN=... in a dumped environment
    (re.compile(r"(DERIV_API_TOKEN\s*=\s*)\S+"), r"\1***REDACTED***"),
]


class RedactionFilter(logging.Filter):
    """Scrub credential-shaped substrings from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        original = msg
        for pattern, repl in _REDACTIONS:
            msg = pattern.sub(repl, msg)
        if msg != original:
            record.msg = msg
            record.args = ()
        return True


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """
    Configure the root logger once and return the bot logger.

    Terminal output is human-readable; the file handler carries timestamps and
    the module name so a post-mortem is possible.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Idempotent: remove handlers we previously installed so repeated calls
    # (e.g. from tests) do not duplicate output.
    for h in list(root.handlers):
        if getattr(h, "_deriv_bot", False):
            root.removeHandler(h)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-7s  %(name)-18s  %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(RedactionFilter())
    console._deriv_bot = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactionFilter())
        fh._deriv_bot = True  # type: ignore[attr-defined]
        root.addHandler(fh)

    # The websocket-client library is extremely chatty at DEBUG.
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger("bot")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
