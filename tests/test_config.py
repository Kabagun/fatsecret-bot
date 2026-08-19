from __future__ import annotations

import pytest

from fatsecret_bot.config import load_config


def test_load_config_accepts_optional_telegram_admin_user_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", "123456")

    config = load_config(tmp_path / "missing.env")

    assert config.telegram_admin_user_id == 123456


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_load_config_rejects_invalid_telegram_admin_user_id(tmp_path, monkeypatch, value: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_USER_ID", value)

    with pytest.raises(RuntimeError, match="TELEGRAM_ADMIN_USER_ID"):
        load_config(tmp_path / "missing.env")
