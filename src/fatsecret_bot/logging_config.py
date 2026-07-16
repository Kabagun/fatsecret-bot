from __future__ import annotations

import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _delete_expired_rotated_logs(log_path: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 24 * 60 * 60
    for candidate in log_path.parent.glob(f"{log_path.name}.*"):
        try:
            if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            logging.getLogger(__name__).warning("Could not remove expired log file %s", candidate, exc_info=True)


def configure_logging(log_path: str | Path, retention_days: int = 10) -> None:
    """Configure console logging and a daily debug log retained for the requested number of days."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    resolved_path = Path(log_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    _delete_expired_rotated_logs(resolved_path, retention_days)

    formatter = logging.Formatter(LOG_FORMAT)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        resolved_path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[console_handler, file_handler],
        force=True,
    )
    for noisy_logger in ("asyncio", "httpcore", "httpx", "telegram", "telegram.ext"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    logging.getLogger(__name__).info(
        "Logging configured file=%s retention_days=%d rotation=daily",
        resolved_path,
        retention_days,
    )
