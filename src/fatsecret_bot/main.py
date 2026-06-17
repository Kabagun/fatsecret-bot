from __future__ import annotations

import logging

from .config import load_config
from .storage import Storage
from .sync import RecipeSyncEngine
from .telegram_bot import TelegramRecipeBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_config()
    storage = Storage(config.db_path)
    sync_engine = RecipeSyncEngine(storage, config.device, timezone=config.timezone)
    bot = TelegramRecipeBot(
        token=config.telegram_token,
        allowed_user_ids=config.allowed_user_ids,
        default_market=config.default_market,
        default_language=config.default_language,
        storage=storage,
        sync_engine=sync_engine,
    )
    app = bot.build()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
