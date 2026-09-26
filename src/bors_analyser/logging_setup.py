"""Logging: console + rotating UTF-8 file (logs/app.log)."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from bors_analyser.config import get_settings

_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        settings.log_dir / "app.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    # Windows consoles default to a legacy code page; force UTF-8 so Persian text never crashes logging.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level or settings.log_level)
    root.addHandler(file_handler)
    root.addHandler(console)
    for noisy in ("httpx", "httpcore", "aiohttp", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
