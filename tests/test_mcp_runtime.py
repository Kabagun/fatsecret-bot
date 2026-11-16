from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fatsecret_bot.config import load_config
from fatsecret_bot.runtime_lock import runtime_lock
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import RecipeSyncEngine
from fatsecret_bot.models import FatSecretDeviceConfig


def test_stdio_starts_without_telegram_and_serves_scoped_group(tmp_path):
    db = tmp_path / "state.sqlite3"
    storage = Storage(db)
    storage.register_user(1001, "Synthetic owner")
    group = storage.create_group(1001, "Synthetic group")
    storage.close()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "TELEGRAM_BOT_TOKEN": "", "FATSECRET_BOT_DB_PATH": str(db),
           "FATSECRET_MCP_ENABLED": "false", "FATSECRET_MCP_GROUP_ID": group.id,
           "FATSECRET_MCP_GROUP_KEY": "synthetic-connection-key-1234567890",
           "FATSECRET_MCP_ISSUER_URL": "http://127.0.0.1:8765",
           "FATSECRET_BOT_LOG_PATH": str(tmp_path / "mcp.log")}

    async def scenario():
        async with stdio_client(StdioServerParameters(command=sys.executable,
                args=["-m", "fatsecret_bot.mcp_server", "--transport", "stdio"], env=env, cwd=str(tmp_path))) as (read, write):
            async with ClientSession(read, write) as session:
                assert (await session.initialize()).serverInfo.name == "FatSecret"
                result = await session.call_tool("get_group", {})
                assert not result.isError
                assert result.structuredContent["group"] == "Synthetic group"
    asyncio.run(scenario())


def test_runtime_lock_rejects_a_second_process_and_releases_after_exit(tmp_path):
    import subprocess

    db = tmp_path / "state.sqlite3"
    script = "from pathlib import Path; from fatsecret_bot.runtime_lock import runtime_lock; import sys; " \
             "ctx=runtime_lock(Path(sys.argv[1])); ctx.__enter__(); ctx.__exit__(None,None,None)"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    with runtime_lock(db):
        blocked = subprocess.run([sys.executable, "-c", script, str(db)], env=env, capture_output=True)
        assert blocked.returncode != 0
        assert "RuntimeError" in blocked.stderr.decode(errors="replace")
    released = subprocess.run([sys.executable, "-c", script, str(db)], env=env, capture_output=True)
    assert released.returncode == 0


def test_common_config_without_telegram_only_when_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_USER_ID", raising=False)
    assert load_config(tmp_path / "missing.env", require_telegram=False).telegram_token == ""
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        load_config(tmp_path / "missing.env")


def test_engine_serializes_background_tasks_and_allows_same_task_reentry(tmp_path):
    storage = Storage(tmp_path / "state.sqlite3")
    device = FatSecretDeviceConfig("1", "6", "30", "11", "Synthetic", "1920x1080", "synthetic")
    engine = RecipeSyncEngine(storage, device)

    async def scenario():
        first_entered, release_first, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        inherited_entered = asyncio.Event()
        inherited_tasks = []

        async def first():
            async with engine.operation():
                first_entered.set()
                # Nested engine methods reenter in the same task, while inherited
                # background tasks must acquire their own operation lease.
                async with engine.operation():
                    assert engine._operation_owner is asyncio.current_task()
                async def inherited():
                    async with engine.operation():
                        inherited_entered.set()
                inherited_tasks.append(asyncio.create_task(inherited()))
                await release_first.wait()

        async def second():
            await first_entered.wait()
            async with engine.operation():
                second_entered.set()

        a, b = asyncio.create_task(first()), asyncio.create_task(second())
        await first_entered.wait()
        await asyncio.sleep(0.01)
        assert not second_entered.is_set()
        assert not inherited_entered.is_set()
        release_first.set()
        await asyncio.wait_for(asyncio.gather(a, b, *inherited_tasks), 1)
        assert second_entered.is_set()
        assert inherited_entered.is_set()
    try:
        asyncio.run(scenario())
    finally:
        storage.close()


def test_telegram_account_changes_wait_for_mcp_operation(tmp_path):
    from fatsecret_bot.telegram_bot import TelegramRecipeBot

    storage = Storage(tmp_path / "state.sqlite3")
    device = FatSecretDeviceConfig("1", "6", "30", "11", "Synthetic", "1920x1080", "synthetic")
    engine = RecipeSyncEngine(storage, device)
    bot = TelegramRecipeBot("synthetic-token", "BY", "ru", None, storage, engine)

    async def scenario():
        called = asyncio.Event()
        async def account_callback(update, context):
            called.set()
            async with engine.operation():
                return "changed"
        async with engine.operation():
            task = asyncio.create_task(bot._serialized_handler(account_callback)(None, None))
            await asyncio.sleep(0.01)
            assert not called.is_set()
        assert await asyncio.wait_for(task, 1) == "changed"
    try:
        asyncio.run(scenario())
    finally:
        storage.close()


def test_embedded_http_server_starts_and_stops(tmp_path):
    import socket
    import httpx
    from fatsecret_bot.mcp_config import MCPConfig
    from fatsecret_bot.mcp_server import MCPRuntime

    storage = Storage(tmp_path / "state.sqlite3")
    storage.register_user(1001, "Synthetic")
    group = storage.create_group(1001, "Synthetic group")
    device = FatSecretDeviceConfig("1", "6", "30", "11", "Synthetic", "1920x1080", "synthetic")
    engine = RecipeSyncEngine(storage, device)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = MCPConfig(group.id, "synthetic-connection-key-1234567890", f"http://127.0.0.1:{port}", port=port)

    async def scenario():
        runtime = MCPRuntime(storage, engine, config)
        try:
            await runtime.start()
            assert runtime.server.started
            async with httpx.AsyncClient(base_url=config.issuer_url) as client:
                response = await client.get("/.well-known/oauth-authorization-server")
                assert response.status_code == 200
                response = await client.post("/mcp", json={})
                assert response.status_code == 401
        finally:
            await runtime.stop()
        assert runtime.task is None
    try:
        asyncio.run(scenario())
    finally:
        storage.close()
