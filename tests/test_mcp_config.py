from __future__ import annotations

import pytest

from fatsecret_bot.mcp_config import MCPConfig, load_mcp_config


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith("FATSECRET_MCP_") or name == "TELEGRAM_BOT_TOKEN":
            monkeypatch.delenv(name, raising=False)


def configure(monkeypatch):
    monkeypatch.setenv("FATSECRET_MCP_GROUP_ID", "00000000-0000-4000-8000-000000000041")
    monkeypatch.setenv("FATSECRET_MCP_GROUP_KEY", "synthetic-connection-key-1234567890")
    monkeypatch.setenv("FATSECRET_MCP_ISSUER_URL", "https://mcp.example.test")


def test_disabled_by_default_and_no_telegram_token_required(tmp_path, monkeypatch):
    assert load_mcp_config(tmp_path / "missing.env") is None
    configure(monkeypatch)
    config = load_mcp_config(tmp_path / "missing.env", required=True)
    assert config is not None
    assert config.group_id == "00000000-0000-4000-8000-000000000041"
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.resource_url == "https://mcp.example.test/mcp"
    assert config.group_key not in repr(config)
    assert config.group_id not in repr(config)


def test_enabled_environment_and_dotenv(tmp_path):
    env = tmp_path / "synthetic.env"
    env.write_text("FATSECRET_MCP_ENABLED=yes\nFATSECRET_MCP_GROUP_ID=00000000-0000-4000-8000-000000000041\n"
                   "FATSECRET_MCP_GROUP_KEY=synthetic-connection-key-1234567890\n"
                   "FATSECRET_MCP_ISSUER_URL=http://127.0.0.1:8765/\n"
                   "FATSECRET_MCP_HOST=0.0.0.0\nFATSECRET_MCP_PORT=9876\n")
    config = load_mcp_config(env)
    assert config is not None
    assert config.issuer_url == "http://127.0.0.1:8765"
    assert config.host == "0.0.0.0"
    assert config.port == 9876


@pytest.mark.parametrize("name,value", [
    ("FATSECRET_MCP_ENABLED", "maybe"),
    ("FATSECRET_MCP_GROUP_ID", ""),
    ("FATSECRET_MCP_GROUP_ID", "has whitespace"),
    ("FATSECRET_MCP_GROUP_ID", "x" * 129),
    ("FATSECRET_MCP_GROUP_KEY", "short"),
    ("FATSECRET_MCP_GROUP_KEY", " synthetic-connection-key-1234567890"),
    ("FATSECRET_MCP_PORT", "65536"),
    ("FATSECRET_MCP_PORT", "0"),
    ("FATSECRET_MCP_PORT", "oops"),
    ("FATSECRET_MCP_HOST", "bad host"),
])
def test_invalid_settings_fail_closed(tmp_path, monkeypatch, name, value):
    configure(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        load_mcp_config(tmp_path / "missing.env", required=True)


@pytest.mark.parametrize("issuer", [
    "http://mcp.example.test", "http://127.0.0.1.example.test", "ftp://localhost", "https://",
    "https://user:password@mcp.example.test", "https://mcp.example.test/path",
    "https://mcp.example.test?value=1", "https://mcp.example.test#part", "https://localhost:bad",
])
def test_invalid_issuer(issuer):
    with pytest.raises(ValueError, match="FATSECRET_MCP_ISSUER_URL"):
        MCPConfig("synthetic-group", "synthetic-connection-key-1234567890", issuer)


def test_missing_config_is_required_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FATSECRET_MCP_ENABLED", "true")
    with pytest.raises(RuntimeError, match="FATSECRET_MCP_GROUP_ID"):
        load_mcp_config(tmp_path / "missing.env")
