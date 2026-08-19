from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import FatSecretDeviceConfig


@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    db_path: Path
    default_market: str
    default_language: str
    timezone: str
    log_path: Path
    log_retention_days: int
    device: FatSecretDeviceConfig


def _getenv(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str) -> str:
    value = _getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    value = _getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid positive integer in {name}: {value}") from exc
    if parsed < 1:
        raise RuntimeError(f"Invalid positive integer in {name}: {value}")
    return parsed


def load_config(env_file: str | Path = ".env") -> BotConfig:
    load_dotenv(env_file)

    default_market = _getenv("FATSECRET_MKT", "BY")
    default_language = _getenv("FATSECRET_LANG", "ru")
    timezone = _getenv("FATSECRET_BOT_TIMEZONE", "Europe/Minsk")
    db_path = Path(_getenv("FATSECRET_BOT_DB_PATH", "temp/state/fatsecret_bot.sqlite3"))
    configured_log_path = _getenv("FATSECRET_BOT_LOG_PATH")
    log_path = Path(configured_log_path) if configured_log_path else db_path.with_name("fatsecret_bot.log")
    log_retention_days = _positive_int("FATSECRET_BOT_LOG_RETENTION_DAYS", 10)

    device = FatSecretDeviceConfig(
        app_version=_getenv("FATSECRET_APP_VERSION", "11.5.0.4"),
        device=_getenv("FATSECRET_DEVICE", "6"),
        build_sdk=_getenv("FATSECRET_BUILD_SDK", "30"),
        build_api=_getenv("FATSECRET_BUILD_API", "11"),
        build_model=_getenv("FATSECRET_BUILD_MODEL", "NE2211"),
        build_resolution=_getenv("FATSECRET_BUILD_RESOLUTION", "1920x1080"),
        device_identifier=_getenv("FATSECRET_DEVICE_IDENTIFIER", "NE2211"),
        authorization=_getenv("FATSECRET_AUTHORIZATION"),
        c_desc=_getenv("FATSECRET_C_DESC"),
        user_agent=_getenv("FATSECRET_USER_AGENT", "FatSecretBot/0.1"),
    )

    return BotConfig(
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        db_path=db_path,
        default_market=default_market,
        default_language=default_language,
        timezone=timezone,
        log_path=log_path,
        log_retention_days=log_retention_days,
        device=device,
    )
