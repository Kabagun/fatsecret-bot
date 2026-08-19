from __future__ import annotations

from .config import load_config
from .logging_config import configure_logging
from .storage import Storage
from .sync import RecipeSyncEngine
from .telegram_bot import TelegramRecipeBot


def main() -> None:
    config = load_config()
    configure_logging(config.log_path, config.log_retention_days)
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
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
