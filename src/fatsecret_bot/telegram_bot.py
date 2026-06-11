from __future__ import annotations

import html
import logging
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .models import FatSecretAccountConfig, FoodSearchResult, Recipe
from .storage import Storage
from .sync import RecipeSyncEngine

logger = logging.getLogger(__name__)
RECIPES_PAGE_SIZE = 8


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Рецепты", "Обновить"],
        ["Новый рецепт", "Аккаунты"],
    ],
    resize_keyboard=True,
)


def _parse_recipe_meta(text: str) -> tuple[str, Decimal, int, int, str]:
    parts = [part.strip() for part in text.split("|", 4)]
    if len(parts) < 4:
        raise ValueError("Нужно: название | порции | подготовка_мин | готовка_мин | описание")
    title = parts[0]
    if not title:
        raise ValueError("Название не может быть пустым")
    try:
        portions = Decimal(parts[1].replace(",", "."))
        prep = int(parts[2])
        cook = int(parts[3])
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Порции должны быть числом, время - целыми минутами") from exc
    description = parts[4] if len(parts) > 4 else ""
    return title, portions, prep, cook, description


def _format_recipe(recipe: Recipe) -> str:
    ingredients = "\n".join(
        f"- {html.escape(item.title)}: {item.amount} {html.escape(item.portion_description or '')}".rstrip()
        for item in recipe.ingredients
    )
    if not ingredients:
        ingredients = "Ингредиентов пока нет."
    remote = ", ".join(f"{k}: {v}" for k, v in recipe.remote_ids.items()) or "нет"
    return (
        f"<b>{html.escape(recipe.title)}</b>\n"
        f"Порций: {recipe.portions}; подготовка: {recipe.prep_time} мин; готовка: {recipe.cook_time} мин\n"
        f"Remote: {html.escape(remote)}\n\n"
        f"{html.escape(recipe.description)}\n\n"
        f"<b>Ингредиенты</b>\n{ingredients}"
    )


def _format_decimal(value: Decimal | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    quantum = Decimal("1") if digits == 0 else Decimal("0." + ("0" * (digits - 1)) + "1")
    return str(value.quantize(quantum)).rstrip("0").rstrip(".")


def _format_food_macros(result: FoodSearchResult) -> str:
    kcal = _format_decimal(result.energy_per_portion, 0)
    protein = _format_decimal(result.protein_per_portion)
    fat = _format_decimal(result.fat_per_portion)
    carbs = _format_decimal(result.carbohydrate_per_portion)
    if kcal == protein == fat == carbs == "-":
        return "КБЖУ нет в выдаче"
    return f"{kcal} ккал; Б {protein}; Ж {fat}; У {carbs}"


def _format_food_button(result: FoodSearchResult) -> str:
    title = result.title.strip()
    macros = _format_food_macros(result)
    label = f"{title} | {macros}"
    return label[:90]


def _format_food_list(results: list[FoodSearchResult]) -> str:
    lines = ["<b>Результаты поиска</b>"]
    for index, result in enumerate(results, 1):
        description = f"\n   {html.escape(result.description[:90])}" if result.description else ""
        lines.append(
            f"{index}. <b>{html.escape(result.title)}</b>\n"
            f"   {html.escape(_format_food_macros(result))}{description}"
        )
    return "\n\n".join(lines)


class TelegramRecipeBot:
    def __init__(
        self,
        token: str,
        allowed_user_ids: set[int],
        default_market: str,
        default_language: str,
        storage: Storage,
        sync_engine: RecipeSyncEngine,
    ) -> None:
        self.token = token
        self.allowed_user_ids = allowed_user_ids
        self.default_market = default_market
        self.default_language = default_language
        self.storage = storage
        self.sync_engine = sync_engine

    def build(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("accounts", self.accounts))
        app.add_handler(CommandHandler("recipes", self.recipes))
        app.add_handler(CommandHandler("refresh", self.refresh))
        app.add_handler(CommandHandler("new", self.new_recipe))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app

    def _is_authorized(self, telegram_id: int) -> bool:
        if telegram_id in self.allowed_user_ids:
            return True
        if self.storage.is_registered_user(telegram_id):
            return True
        return not self.allowed_user_ids and self.storage.registered_user_count() < 2

    async def _require_user(self, update: Update) -> bool:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return False
        if not self._is_authorized(user.id):
            await message.reply_text("Этот бот закрыт для двух заданных пользователей.")
            return False
        self.storage.register_user(user.id, user.full_name or str(user.id))
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        await update.effective_message.reply_text(
            "Готов. Сначала подключи FatSecret в «Аккаунты», затем обнови рецепты.",
            reply_markup=MAIN_KEYBOARD,
        )

    def _accounts_text(self) -> str:
        accounts = self.storage.list_fatsecret_accounts()
        if not accounts:
            return "FatSecret аккаунты еще не подключены. Нужно подключить два аккаунта."
        lines = [f"<b>Подключено FatSecret аккаунтов: {len(accounts)}/2</b>"]
        for account in accounts:
            lines.append(f"- {html.escape(account.label)}: {html.escape(account.username)}")
        if len(accounts) < 2:
            lines.append("\nПодключи второй аккаунт, чтобы синхронизация шла в обе стороны.")
        return "\n".join(lines)

    def _accounts_keyboard(self, telegram_id: int) -> InlineKeyboardMarkup:
        buttons = [[InlineKeyboardButton("Подключить мой FatSecret", callback_data="account_add:0")]]
        if self.storage.get_fatsecret_account_by_telegram_id(telegram_id):
            buttons.append([InlineKeyboardButton("Удалить мой FatSecret", callback_data="account_remove:0")])
        buttons.append([InlineKeyboardButton("К списку рецептов", callback_data="list:0")])
        return InlineKeyboardMarkup(buttons)

    async def accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        await update.effective_message.reply_text(
            self._accounts_text(),
            reply_markup=self._accounts_keyboard(update.effective_user.id),
            parse_mode=ParseMode.HTML,
        )

    async def _edit_accounts(self, query, telegram_id: int) -> None:
        await query.edit_message_text(
            self._accounts_text(),
            reply_markup=self._accounts_keyboard(telegram_id),
            parse_mode=ParseMode.HTML,
        )

    async def refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        msg = await update.effective_message.reply_text("Обновляю рецепты из двух FatSecret аккаунтов...")
        try:
            imported = await self.sync_engine.refresh_remote_recipes()
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh failed")
            await msg.edit_text(f"Ошибка обновления: {exc}")
            return
        await msg.edit_text(f"Готово. Импортировано/смёржено записей: {imported}.")
        await self._send_recipe_list(update, context, page=0)

    async def recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        await self._send_recipe_list(update, context, page=0)

    async def _send_recipe_list(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        page: int,
    ) -> None:
        recipes = self.storage.list_recipes()
        if not recipes:
            await update.effective_message.reply_text(
                "Рецептов пока нет. Нажми «Обновить» или «Новый рецепт».",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        page = max(0, page)
        total_pages = max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        current = recipes[start : start + RECIPES_PAGE_SIZE]
        buttons = [
            [InlineKeyboardButton(recipe.title[:55], callback_data=f"open:{recipe.id}")]
            for recipe in current
        ]
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"list:{page}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Дальше", callback_data=f"list:{page + 1}"))
        buttons.append(nav)
        buttons.append(
            [
                InlineKeyboardButton("Обновить", callback_data="refresh:0"),
                InlineKeyboardButton("Новый рецепт", callback_data="new:0"),
            ]
        )
        await update.effective_message.reply_text(
            "Общий список рецептов:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def new_recipe(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data["mode"] = "new_recipe"
        await update.effective_message.reply_text(
            "Пришли рецепт одной строкой:\n"
            "Название | порции | подготовка_мин | готовка_мин | описание\n\n"
            "Пример: Омлет | 2 | 5 | 10 | Завтрак"
        )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        query = update.callback_query
        if query is None or not query.data:
            return
        await query.answer()
        action, _, value = query.data.partition(":")

        if action == "open":
            await self._open_recipe(query, value)
        elif action == "accounts":
            await self._edit_accounts(query, update.effective_user.id)
        elif action == "account_add":
            await self._start_account_add(query, context, update.effective_user.id)
        elif action == "account_remove":
            removed = self.storage.delete_fatsecret_account_for_user(update.effective_user.id)
            await query.edit_message_text(
                "FatSecret аккаунт удален." if removed else "У тебя нет подключенного FatSecret аккаунта.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
        elif action == "list":
            await self._edit_recipe_list(query, int(value or "0"))
        elif action == "refresh":
            await self._refresh_from_callback(query)
        elif action == "new":
            context.user_data["mode"] = "new_recipe"
            await query.edit_message_text(
                "Пришли рецепт одной строкой:\n"
                "Название | порции | подготовка_мин | готовка_мин | описание\n\n"
                "Пример: Омлет | 2 | 5 | 10 | Завтрак"
            )
        elif action == "add":
            context.user_data["mode"] = "ingredient_search"
            context.user_data["recipe_id"] = value
            await query.edit_message_text(
                "Введите название продукта/ингредиента для поиска.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{value}")]]),
            )
        elif action == "edit":
            context.user_data["mode"] = "edit_recipe"
            context.user_data["recipe_id"] = value
            await query.edit_message_text(
                "Пришли новые данные:\nНазвание | порции | подготовка_мин | готовка_мин | описание"
            )
        elif action == "sync":
            await self._sync_recipe_message(query, value)
        elif action == "food":
            await self._select_food(query, context, value)

    async def _start_account_add(self, query, context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> None:
        existing = self.storage.get_fatsecret_account_by_telegram_id(telegram_id)
        if existing is None and self.storage.fatsecret_account_count() >= 2:
            await query.edit_message_text("Уже подключены два FatSecret аккаунта. Сначала удали один из них.")
            return
        context.user_data.clear()
        context.user_data["mode"] = "fatsecret_login"
        await query.edit_message_text("Пришли логин или email от FatSecret. Сообщение я постараюсь удалить после чтения.")

    async def _edit_recipe_list(self, query, page: int) -> None:
        recipes = self.storage.list_recipes()
        if not recipes:
            await query.edit_message_text("Рецептов пока нет.")
            return
        total_pages = max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(max(0, page), total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        current = recipes[start : start + RECIPES_PAGE_SIZE]
        buttons = [
            [InlineKeyboardButton(recipe.title[:55], callback_data=f"open:{recipe.id}")]
            for recipe in current
        ]
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"list:{page}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Дальше", callback_data=f"list:{page + 1}"))
        buttons.append(nav)
        buttons.append(
            [
                InlineKeyboardButton("Обновить", callback_data="refresh:0"),
                InlineKeyboardButton("Новый рецепт", callback_data="new:0"),
            ]
        )
        await query.edit_message_text("Общий список рецептов:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _refresh_from_callback(self, query) -> None:
        await query.edit_message_text("Обновляю рецепты из двух FatSecret аккаунтов...")
        try:
            imported = await self.sync_engine.refresh_remote_recipes()
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh failed")
            await query.edit_message_text(f"Ошибка обновления: {exc}")
            return
        await query.edit_message_text(f"Готово. Импортировано/смёржено записей: {imported}.")

    async def _open_recipe(self, query, recipe_id: str) -> None:
        recipe = await self.sync_engine.hydrate_recipe_from_remote(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Добавить ингредиент", callback_data=f"add:{recipe.id}"),
                    InlineKeyboardButton("Изменить", callback_data=f"edit:{recipe.id}"),
                ],
                [
                    InlineKeyboardButton("Синхронизировать", callback_data=f"sync:{recipe.id}"),
                    InlineKeyboardButton("К списку", callback_data="list:0"),
                ],
            ]
        )
        await query.edit_message_text(_format_recipe(recipe), reply_markup=keyboard, parse_mode=ParseMode.HTML)

    async def _sync_recipe_message(self, query, recipe_id: str) -> None:
        await query.edit_message_text("Синхронизирую в оба FatSecret аккаунта...")
        try:
            results = await self.sync_engine.sync_recipe(recipe_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync failed")
            await query.edit_message_text(f"Ошибка синхронизации: {exc}")
            return
        lines = [
            f"{result.account_key}: {'OK' if result.ok else 'ERROR'}"
            f" {result.remote_recipe_id or ''} {result.message}"
            for result in results
        ]
        await query.edit_message_text("Синхронизация завершена:\n" + "\n".join(lines))

    async def _select_food(self, query, context: ContextTypes.DEFAULT_TYPE, value: str) -> None:
        choices: list[FoodSearchResult] = context.user_data.get("food_choices", [])
        try:
            choice = choices[int(value)]
        except (ValueError, IndexError):
            await query.edit_message_text("Выбор продукта устарел. Повтори поиск.")
            return
        context.user_data["selected_food"] = choice
        context.user_data["mode"] = "ingredient_amount"
        await query.edit_message_text(
            f"<b>{html.escape(choice.title)}</b>\n"
            f"{html.escape(_format_food_macros(choice))}\n\n"
            "Количество для добавления. Например: 100",
            parse_mode=ParseMode.HTML,
        )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        mode = context.user_data.get("mode")
        text = update.effective_message.text.strip()
        if mode is None and text == "Рецепты":
            await self._send_recipe_list(update, context, page=0)
            return
        if mode is None and text == "Обновить":
            await self.refresh(update, context)
            return
        if mode is None and text == "Новый рецепт":
            await self.new_recipe(update, context)
            return
        if mode is None and text == "Аккаунты":
            await self.accounts(update, context)
            return
        if mode == "new_recipe":
            await self._handle_new_recipe(update, context, text)
        elif mode == "edit_recipe":
            await self._handle_edit_recipe(update, context, text)
        elif mode == "ingredient_search":
            await self._handle_ingredient_search(update, context, text)
        elif mode == "ingredient_amount":
            await self._handle_ingredient_amount(update, context, text)
        elif mode == "fatsecret_login":
            await self._handle_fatsecret_login(update, context, text)
        elif mode == "fatsecret_password":
            await self._handle_fatsecret_password(update, context, text)
        else:
            await update.effective_message.reply_text(
                "Выбери действие кнопками ниже.",
                reply_markup=MAIN_KEYBOARD,
            )

    async def _delete_user_message(self, update: Update) -> None:
        try:
            await update.effective_message.delete()
        except Exception:  # noqa: BLE001 - message deletion is best-effort only.
            logger.debug("could not delete user message", exc_info=True)

    async def _handle_fatsecret_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        await self._delete_user_message(update)
        if not text:
            await update.effective_chat.send_message("Логин пустой. Пришли логин или email от FatSecret.")
            return
        context.user_data["fatsecret_username"] = text
        context.user_data["mode"] = "fatsecret_password"
        await update.effective_chat.send_message("Теперь пришли пароль от FatSecret. Я тоже постараюсь удалить это сообщение.")

    async def _handle_fatsecret_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        await self._delete_user_message(update)
        user = update.effective_user
        username = context.user_data.get("fatsecret_username", "")
        if user is None or not username or not text:
            context.user_data.clear()
            await update.effective_chat.send_message("Контекст подключения потерян. Нажми «Аккаунты» и начни заново.")
            return
        existing = self.storage.get_fatsecret_account_by_telegram_id(user.id)
        if existing is None and self.storage.fatsecret_account_count() >= 2:
            context.user_data.clear()
            await update.effective_chat.send_message("Уже подключены два FatSecret аккаунта. Сначала удали один из них.")
            return

        account = FatSecretAccountConfig(
            key=f"tg{user.id}",
            label=user.full_name or str(user.id),
            username=username,
            password=text,
            market=self.default_market,
            language=self.default_language,
        )
        status = await update.effective_chat.send_message("Проверяю логин в FatSecret...")
        try:
            await self.sync_engine.validate_account(account)
        except Exception as exc:  # noqa: BLE001
            logger.exception("FatSecret account validation failed")
            context.user_data.clear()
            await status.edit_text(f"FatSecret не принял логин/пароль: {exc}")
            return

        self.storage.upsert_fatsecret_account(
            telegram_id=user.id,
            label=account.label,
            username=account.username,
            password=account.password,
            market=account.market,
            language=account.language,
        )
        context.user_data.clear()
        await status.edit_text(
            "FatSecret аккаунт подключен.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
        )

    async def _handle_new_recipe(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        try:
            title, portions, prep, cook, description = _parse_recipe_meta(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        recipe_id = self.storage.create_recipe(
            title=title,
            description=description,
            portions=portions,
            prep_time=prep,
            cook_time=cook,
            updated_by=update.effective_user.id,
        )
        context.user_data.clear()
        await update.effective_message.reply_text("Создал локально. Синхронизирую в оба аккаунта...")
        await self._sync_after_text(update, recipe_id)

    async def _handle_edit_recipe(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        recipe_id = context.user_data.get("recipe_id")
        if not recipe_id:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст редактирования потерян.")
            return
        try:
            title, portions, prep, cook, description = _parse_recipe_meta(text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        self.storage.update_recipe_meta(
            recipe_id=recipe_id,
            title=title,
            description=description,
            portions=portions,
            prep_time=prep,
            cook_time=cook,
            updated_by=update.effective_user.id,
        )
        context.user_data.clear()
        await update.effective_message.reply_text("Сохранил локально. Синхронизирую в оба аккаунта...")
        await self._sync_after_text(update, recipe_id)

    async def _handle_ingredient_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        try:
            results = await self.sync_engine.search_food(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("food search failed")
            await update.effective_message.reply_text(f"Ошибка поиска: {exc}")
            return
        if not results:
            await update.effective_message.reply_text("Ничего не найдено.")
            return
        context.user_data["food_choices"] = results
        buttons = [
            [InlineKeyboardButton(_format_food_button(result), callback_data=f"food:{index}")]
            for index, result in enumerate(results)
        ]
        buttons.append([InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{context.user_data['recipe_id']}")])
        await update.effective_message.reply_text(
            _format_food_list(results),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_ingredient_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        recipe_id = context.user_data.get("recipe_id")
        selected: FoodSearchResult | None = context.user_data.get("selected_food")
        if not recipe_id or selected is None:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст добавления ингредиента потерян.")
            return
        try:
            amount = Decimal(text.replace(",", "."))
        except InvalidOperation:
            await update.effective_message.reply_text("Количество должно быть числом.")
            return
        try:
            detail = await self.sync_engine.resolve_food(selected)
        except Exception:
            logger.exception("food resolve failed")
            detail = selected
        self.storage.add_ingredient(
            recipe_id=recipe_id,
            food_id=detail.food_id,
            title=detail.title,
            portion_id=detail.default_portion_id or "0",
            amount=amount,
        )
        context.user_data.clear()
        await update.effective_message.reply_text("Ингредиент добавлен локально. Синхронизирую...")
        await self._sync_after_text(update, recipe_id)

    async def _sync_after_text(self, update: Update, recipe_id: str) -> None:
        try:
            results = await self.sync_engine.sync_recipe(recipe_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync failed")
            await update.effective_message.reply_text(f"Ошибка синхронизации: {exc}")
            return
        lines = [f"{r.account_key}: {'OK' if r.ok else 'ERROR'} {r.message}" for r in results]
        await update.effective_message.reply_text("\n".join(lines))
