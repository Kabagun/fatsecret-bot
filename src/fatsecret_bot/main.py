from __future__ import annotations

import logging

from .config import load_config
from .fatsecret_client import FatSecretClient
from .storage import Storage
from .sync import RecipeSyncEngine
from .telegram_bot import TelegramRecipeBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    storage = Storage(config.db_path)
    clients = {
        account.key: FatSecretClient(account, config.device)
        for account in config.accounts
    }
    sync_engine = RecipeSyncEngine(storage, clients)
    bot = TelegramRecipeBot(
        token=config.telegram_token,
        allowed_user_ids=config.allowed_user_ids,
        storage=storage,
        sync_engine=sync_engine,
    )
    app = bot.build()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
