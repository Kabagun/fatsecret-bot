from __future__ import annotations

from .config import load_config
from .logging_config import configure_logging
from .runtime_lock import runtime_lock
from .storage import Storage
from .sync import RecipeSyncEngine
from .telegram_bot import TelegramRecipeBot


def main() -> None:
    config = load_config()
    configure_logging(config.log_path, config.log_retention_days)
    with runtime_lock(config.db_path):
        storage = Storage(config.db_path)
        sync_engine = RecipeSyncEngine(storage, config.device, timezone=config.timezone)
        bot = TelegramRecipeBot(
            token=config.telegram_token,
            default_market=config.default_market,
            default_language=config.default_language,
            admin_user_id=config.telegram_admin_user_id,
            storage=storage,
            sync_engine=sync_engine,
        )
        app = bot.build()
        from .mcp_config import load_mcp_config
        mcp_config = load_mcp_config()
        if mcp_config is not None:
            from .mcp_server import MCPRuntime
            runtime = MCPRuntime(storage, sync_engine, mcp_config)

            async def post_init(application):
                await runtime.start()
                await bot._post_init(application)

            async def post_shutdown(application):
                try:
                    await runtime.stop()
                finally:
                    await bot._post_shutdown(application)

            app.post_init, app.post_shutdown = post_init, post_shutdown
        try:
            app.run_polling(close_loop=False)
        finally:
            storage.close()


if __name__ == "__main__":
    main()
