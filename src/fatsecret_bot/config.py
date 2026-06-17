from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import FatSecretDeviceConfig


@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    allowed_user_ids: set[int]
    db_path: Path
    default_market: str
    default_language: str
    timezone: str
    device: FatSecretDeviceConfig


def _getenv(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str) -> str:
    value = _getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _allowed_user_ids(value: str) -> set[int]:
    if not value.strip():
        return set()
    ids: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise RuntimeError(f"Invalid Telegram user id in TELEGRAM_ALLOWED_USER_IDS: {part}") from exc
    return ids


def load_config(env_file: str | Path = ".env") -> BotConfig:
    load_dotenv(env_file)

    default_market = _getenv("FATSECRET_MKT", "BY")
    default_language = _getenv("FATSECRET_LANG", "ru")
    timezone = _getenv("FATSECRET_BOT_TIMEZONE", "Europe/Minsk")
    db_path = Path(_getenv("FATSECRET_BOT_DB_PATH", "temp/state/fatsecret_bot.sqlite3"))

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
        allowed_user_ids=_allowed_user_ids(_getenv("TELEGRAM_ALLOWED_USER_IDS")),
        db_path=db_path,
        default_market=default_market,
        default_language=default_language,
        timezone=timezone,
        device=device,
    )
