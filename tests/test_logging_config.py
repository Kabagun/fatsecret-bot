from __future__ import annotations

import logging
import os
import time

from fatsecret_bot.logging_config import configure_logging


def test_configure_logging_writes_debug_and_removes_files_older_than_retention(tmp_path) -> None:
    log_path = tmp_path / "fatsecret_bot.log"
    stale_log = tmp_path / "fatsecret_bot.log.2026-06-01"
    recent_log = tmp_path / "fatsecret_bot.log.2026-07-15"
    stale_log.write_text("stale", encoding="utf-8")
    recent_log.write_text("recent", encoding="utf-8")
    now = time.time()
    os.utime(stale_log, (now - 11 * 24 * 60 * 60, now - 11 * 24 * 60 * 60))
    os.utime(recent_log, (now - 9 * 24 * 60 * 60, now - 9 * 24 * 60 * 60))

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        configure_logging(log_path, retention_days=10)
        logging.getLogger("fatsecret_bot.test").debug("debug trace marker")
        for handler in root.handlers:
            handler.flush()

        assert log_path.exists()
        assert "debug trace marker" in log_path.read_text(encoding="utf-8")
        assert not stale_log.exists()
        assert recent_log.exists()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)


def test_configure_logging_rejects_non_positive_retention(tmp_path) -> None:
    try:
        configure_logging(tmp_path / "fatsecret_bot.log", retention_days=0)
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("expected ValueError")
