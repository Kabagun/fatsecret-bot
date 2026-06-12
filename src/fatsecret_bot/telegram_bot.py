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
from .storage import Storage, normalize_title
from .sync import RecipeSyncEngine

logger = logging.getLogger(__name__)
RECIPES_PAGE_SIZE = 8
EDIT_FIELD_LABELS = {
    "title": "Название",
    "portions": "Порции",
    "prep": "Подготовка, мин",
    "cook": "Готовка, мин",
    "description": "Описание",
}


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Рецепты", "Поиск"],
        ["Обновить", "Аккаунты"],
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
        f"- {html.escape(item.title)}: {html.escape(_format_ingredient_amount(item.amount, item.portion_description))}"
        for item in recipe.ingredients
    )
    if not ingredients:
        ingredients = "Ингредиентов пока нет."
    description = f"\n\n{html.escape(recipe.description)}" if recipe.description else ""
    return (
        f"<b>{html.escape(recipe.title)}</b>\n"
        f"Порций: {_format_decimal_plain(recipe.portions)}; "
        f"подготовка: {recipe.prep_time} мин; готовка: {recipe.cook_time} мин"
        f"{description}\n\n"
        f"<b>Ингредиенты</b>\n{ingredients}"
    )


def _format_decimal_plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _format_ingredient_unit(amount: Decimal, portion_description: str) -> str:
    unit = portion_description.strip()
    normalized = unit.casefold()
    if normalized in {"g", "gram", "grams", "гр", "г"}:
        return "г"
    if normalized in {"ml", "milliliter", "milliliters", "мл"}:
        return "мл"
    if normalized in {"serving", "servings"}:
        return "порция" if amount == Decimal("1") else "порции"
    return unit


def _format_ingredient_amount(amount: Decimal, portion_description: str) -> str:
    number = _format_decimal_plain(amount)
    unit = _format_ingredient_unit(amount, portion_description)
    if not unit:
        return number
    if unit in {"г", "мл"}:
        return f"{number}{unit}"
    return f"{number} {unit}"


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


def _recipe_actions_keyboard(recipe_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Синхронизировать", callback_data=f"sync:{recipe_id}"),
                InlineKeyboardButton("Удалить в FatSecret", callback_data=f"delete:{recipe_id}"),
            ],
            [InlineKeyboardButton("К списку", callback_data="list:0")],
        ]
    )


def _recipe_edit_keyboard(recipe_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Название", callback_data=f"editfield:title:{recipe_id}"),
                InlineKeyboardButton("Порции", callback_data=f"editfield:portions:{recipe_id}"),
            ],
            [
                InlineKeyboardButton("Подготовка", callback_data=f"editfield:prep:{recipe_id}"),
                InlineKeyboardButton("Готовка", callback_data=f"editfield:cook:{recipe_id}"),
            ],
            [InlineKeyboardButton("Описание", callback_data=f"editfield:description:{recipe_id}")],
            [InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{recipe_id}")],
        ]
    )


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
        app.add_handler(CommandHandler("search", self.search_recipes))
        app.add_handler(CommandHandler("refresh", self.refresh))
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
            "Готов. Создавай и редактируй рецепты в FatSecret, а здесь обновляй список и синхронизируй выбранный рецепт.",
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
                "Рецептов пока нет. Нажми «Обновить», чтобы загрузить их из FatSecret.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        await update.effective_message.reply_text(
            "Общий список рецептов:",
            reply_markup=self._recipe_list_keyboard(recipes, page, "list"),
        )

    def _recipe_list_keyboard(self, recipes: list[Recipe], page: int, page_action: str) -> InlineKeyboardMarkup:
        page = max(0, page)
        total_pages = max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        current = recipes[start : start + RECIPES_PAGE_SIZE]
        buttons = [[InlineKeyboardButton(recipe.title[:55], callback_data=f"open:{recipe.id}")] for recipe in current]
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"{page_action}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"{page_action}:{page}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Дальше", callback_data=f"{page_action}:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append(
            [
                InlineKeyboardButton("Поиск", callback_data="search:0"),
                InlineKeyboardButton("Обновить", callback_data="refresh:0"),
            ]
        )
        buttons.append([InlineKeyboardButton("Удалить несколько", callback_data=f"batchdel:{page}")])
        return InlineKeyboardMarkup(buttons)

    def _filter_recipes(self, query: str) -> list[Recipe]:
        terms = normalize_title(query).split()
        if not terms:
            return []
        matches: list[Recipe] = []
        for recipe in self.storage.list_recipes():
            haystack = normalize_title(
                " ".join([recipe.title, recipe.description, *(item.title for item in recipe.ingredients)])
            )
            if all(term in haystack for term in terms):
                matches.append(recipe)
        return matches

    async def search_recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        context.user_data["mode"] = "recipe_search"
        await update.effective_message.reply_text("Что искать в рецептах? Пришли часть названия или ингредиента.")

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
            context.user_data.clear()
            await self._open_recipe(query, value)
        elif action == "accounts":
            context.user_data.clear()
            await self._edit_accounts(query, update.effective_user.id)
        elif action == "account_add":
            await self._start_account_add(query, context, update.effective_user.id)
        elif action == "account_remove":
            context.user_data.clear()
            removed = self.storage.delete_fatsecret_account_for_user(update.effective_user.id)
            await query.edit_message_text(
                "FatSecret аккаунт удален." if removed else "У тебя нет подключенного FatSecret аккаунта.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
        elif action == "list":
            context.user_data.clear()
            await self._edit_recipe_list(query, int(value or "0"))
        elif action == "search":
            context.user_data.clear()
            context.user_data["mode"] = "recipe_search"
            await query.edit_message_text("Что искать в рецептах? Пришли часть названия или ингредиента.")
        elif action == "searchpage":
            await self._edit_search_results(query, context, int(value or "0"))
        elif action == "refresh":
            context.user_data.clear()
            await self._refresh_from_callback(query)
        elif action == "sync":
            context.user_data.clear()
            await self._open_sync_menu(query, value)
        elif action == "syncfrom":
            context.user_data.clear()
            source_key, _, recipe_id = value.partition(":")
            await self._sync_recipe_message(query, recipe_id, source_key)
        elif action == "batchdel":
            await self._open_batch_delete(query, context, int(value or "0"))
        elif action == "bdtoggle":
            await self._toggle_batch_delete(query, context, value)
        elif action == "bdconfirm":
            await self._confirm_batch_delete(query, context, int(value or "0"))
        elif action == "bdexecute":
            await self._execute_batch_delete(query, context)
        elif action == "bdcancel":
            context.user_data.clear()
            await self._edit_recipe_list(query, 0)
        elif action == "delete":
            context.user_data.clear()
            await self._confirm_delete_recipe(query, value)
        elif action == "delete_confirm":
            context.user_data.clear()
            await self._delete_recipe(query, value)
        else:
            await query.edit_message_text(
                "Это действие устарело. Открой список рецептов заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )

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
        await query.edit_message_text(
            "Общий список рецептов:",
            reply_markup=self._recipe_list_keyboard(recipes, page, "list"),
        )

    async def _edit_search_results(self, query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        search_query = context.user_data.get("recipe_search_query")
        if not search_query:
            await query.edit_message_text(
                "Поиск устарел. Запусти поиск заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Поиск", callback_data="search:0")]]),
            )
            return
        recipes = self._filter_recipes(search_query)
        if not recipes:
            await query.edit_message_text(
                f"По запросу «{html.escape(search_query)}» ничего не найдено.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Новый поиск", callback_data="search:0")]]),
                parse_mode=ParseMode.HTML,
            )
            return
        await query.edit_message_text(
            f"Найдено рецептов: {len(recipes)}",
            reply_markup=self._recipe_list_keyboard(recipes, page, "searchpage"),
        )

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
        await query.edit_message_text(
            _format_recipe(recipe),
            reply_markup=_recipe_actions_keyboard(recipe.id),
            parse_mode=ParseMode.HTML,
        )

    async def _open_edit_menu(self, query, recipe_id: str) -> None:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        await query.edit_message_text(
            f"Что изменить в рецепте «{html.escape(recipe.title)}»?",
            reply_markup=_recipe_edit_keyboard(recipe_id),
            parse_mode=ParseMode.HTML,
        )

    async def _start_edit_field(self, query, context: ContextTypes.DEFAULT_TYPE, value: str) -> None:
        field, _, recipe_id = value.partition(":")
        if field not in EDIT_FIELD_LABELS or not recipe_id:
            await query.edit_message_text("Не понял, какое поле нужно изменить.")
            return
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        current = {
            "title": recipe.title,
            "portions": _format_decimal_plain(recipe.portions),
            "prep": str(recipe.prep_time),
            "cook": str(recipe.cook_time),
            "description": recipe.description or "-",
        }[field]
        context.user_data.clear()
        context.user_data["mode"] = f"edit_{field}"
        context.user_data["recipe_id"] = recipe_id
        await query.edit_message_text(
            f"{EDIT_FIELD_LABELS[field]}\nСейчас: {html.escape(current)}\n\nПришли новое значение.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{recipe_id}")]]),
            parse_mode=ParseMode.HTML,
        )

    async def _open_sync_menu(self, query, recipe_id: str) -> None:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        accounts = {account.key: account.label for account in self.storage.list_fatsecret_accounts()}
        source_keys = [key for key in recipe.remote_ids if key in accounts]
        if not source_keys:
            await query.edit_message_text(
                "У рецепта нет привязки к подключенным FatSecret аккаунтам. Нажми «Обновить» и попробуй снова.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Обновить", callback_data="refresh:0")]]),
            )
            return
        if len(source_keys) == 1:
            await self._sync_recipe_message(query, recipe_id, source_keys[0])
            return
        buttons = [
            [InlineKeyboardButton(f"Из {accounts[key]}", callback_data=f"syncfrom:{key}:{recipe_id}")]
            for key in source_keys
        ]
        buttons.append([InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{recipe_id}")])
        await query.edit_message_text(
            "На каком FatSecret аккаунте сейчас правильная версия рецепта?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _sync_recipe_message(self, query, recipe_id: str, source_account_key: str) -> None:
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts()}
        source_label = account_labels.get(source_account_key, source_account_key)
        await query.edit_message_text(f"Синхронизирую рецепт из FatSecret аккаунта «{source_label}»...")
        try:
            results = await self.sync_engine.sync_recipe_from_source(recipe_id, source_account_key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync failed")
            await query.edit_message_text(f"Ошибка синхронизации: {exc}")
            return
        lines = [
            f"{account_labels.get(result.account_key, result.account_key)}: {'OK' if result.ok else 'ERROR'}"
            f" {result.remote_recipe_id or ''} {result.message}"
            for result in results
        ]
        await query.edit_message_text(
            "Синхронизация завершена:\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Открыть рецепт", callback_data=f"open:{recipe_id}")],
                    [InlineKeyboardButton("К списку", callback_data="list:0")],
                ]
            ),
        )

    async def _confirm_delete_recipe(self, query, recipe_id: str) -> None:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        await query.edit_message_text(
            f"Удалить «{html.escape(recipe.title)}» из FatSecret на всех привязанных аккаунтах?\n\n"
            "После успешного удаления бот уберет рецепт из своего списка.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Удалить в FatSecret", callback_data=f"delete_confirm:{recipe_id}")],
                    [InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{recipe_id}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _delete_recipe(self, query, recipe_id: str) -> None:
        await query.edit_message_text("Удаляю рецепт в FatSecret...")
        try:
            results = await self.sync_engine.delete_recipe_everywhere(recipe_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("delete failed")
            await query.edit_message_text(f"Ошибка удаления: {exc}")
            return
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts()}
        lines = [
            f"{account_labels.get(result.account_key, result.account_key)}: "
            f"{'OK' if result.ok else 'ERROR'} {result.remote_recipe_id or ''} {result.message}"
            for result in results
        ]
        await query.edit_message_text(
            "Удаление завершено:\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("К списку", callback_data="list:0")],
                    [InlineKeyboardButton("Обновить", callback_data="refresh:0")],
                ]
            ),
        )

    def _batch_delete_ids(self, context: ContextTypes.DEFAULT_TYPE) -> set[str]:
        selected = context.user_data.setdefault("batch_delete_ids", set())
        if not isinstance(selected, set):
            selected = set(selected)
            context.user_data["batch_delete_ids"] = selected
        return selected

    async def _open_batch_delete(self, query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        recipes = self.storage.list_recipes()
        if not recipes:
            await query.edit_message_text("Рецептов пока нет.")
            return
        context.user_data["mode"] = "batch_delete"
        selected = self._batch_delete_ids(context)
        selected.intersection_update({recipe.id for recipe in recipes})
        await query.edit_message_text(
            f"Выбери рецепты для удаления из FatSecret. Отмечено: {len(selected)}",
            reply_markup=self._batch_delete_keyboard(recipes, page, selected),
        )

    async def _toggle_batch_delete(self, query, context: ContextTypes.DEFAULT_TYPE, value: str) -> None:
        recipe_id, _, page_text = value.partition(":")
        selected = self._batch_delete_ids(context)
        if recipe_id in selected:
            selected.remove(recipe_id)
        else:
            selected.add(recipe_id)
        await self._open_batch_delete(query, context, int(page_text or "0"))

    def _batch_delete_keyboard(self, recipes: list[Recipe], page: int, selected: set[str]) -> InlineKeyboardMarkup:
        page = max(0, page)
        total_pages = max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        current = recipes[start : start + RECIPES_PAGE_SIZE]
        buttons = [
            [
                InlineKeyboardButton(
                    f"{'[x]' if recipe.id in selected else '[ ]'} {recipe.title[:48]}",
                    callback_data=f"bdtoggle:{recipe.id}:{page}",
                )
            ]
            for recipe in current
        ]
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"batchdel:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"batchdel:{page}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("Дальше", callback_data=f"batchdel:{page + 1}"))
        if nav:
            buttons.append(nav)
        if selected:
            buttons.append([InlineKeyboardButton(f"Удалить выбранные: {len(selected)}", callback_data=f"bdconfirm:{page}")])
        buttons.append([InlineKeyboardButton("Отмена", callback_data="bdcancel:0")])
        return InlineKeyboardMarkup(buttons)

    async def _confirm_batch_delete(self, query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        selected = self._batch_delete_ids(context)
        selected_recipes = [recipe for recipe in self.storage.list_recipes() if recipe.id in selected]
        if not selected_recipes:
            await query.edit_message_text(
                "Ничего не выбрано.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад к выбору", callback_data=f"batchdel:{page}")]]),
            )
            return
        preview = "\n".join(f"- {html.escape(recipe.title)}" for recipe in selected_recipes[:10])
        if len(selected_recipes) > 10:
            preview += f"\n...и еще {len(selected_recipes) - 10}"
        await query.edit_message_text(
            f"<b>Удалить из FatSecret рецептов: {len(selected_recipes)}?</b>\n\n"
            f"{preview}\n\n"
            "Удаление пройдет по всем привязанным аккаунтам.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Да, удалить в FatSecret", callback_data="bdexecute:0")],
                    [InlineKeyboardButton("Назад к выбору", callback_data=f"batchdel:{page}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _execute_batch_delete(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        selected_set = self._batch_delete_ids(context)
        recipes = self.storage.list_recipes()
        selected = [recipe.id for recipe in recipes if recipe.id in selected_set]
        if not selected:
            await query.edit_message_text("Ничего не выбрано.")
            return
        title_by_id = {recipe.id: recipe.title for recipe in recipes}
        await query.edit_message_text(f"Удаляю рецепты в FatSecret: {len(selected)}...")
        try:
            results_by_recipe = await self.sync_engine.delete_recipes_everywhere(selected)
        except Exception as exc:  # noqa: BLE001
            logger.exception("batch delete failed")
            await query.edit_message_text(f"Ошибка batch удаления: {exc}")
            return
        context.user_data.clear()
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts()}
        ok_count = 0
        error_count = 0
        lines: list[str] = []
        for recipe_id in selected:
            results = results_by_recipe.get(recipe_id, [])
            ok = bool(results) and all(result.ok for result in results)
            ok_count += int(ok)
            error_count += int(not ok)
            result_text = "; ".join(
                f"{account_labels.get(result.account_key, result.account_key)} "
                f"{'OK' if result.ok else 'ERROR'} {result.message}"
                for result in results
            )
            lines.append(f"- {title_by_id.get(recipe_id, recipe_id)}: {'OK' if ok else 'ERROR'}; {result_text}")
        text = (
            f"Массовое удаление завершено. OK: {ok_count}; ошибок: {error_count}.\n\n"
            + "\n".join(lines)
        )
        if len(text) > 3800:
            text = text[:3700].rstrip() + "\n...результат обрезан, часть строк не помещается в Telegram."
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("К списку", callback_data="list:0")],
                    [InlineKeyboardButton("Обновить", callback_data="refresh:0")],
                ]
            ),
        )

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
        if mode is not None and text.casefold() in {"отмена", "назад"}:
            await self._cancel_mode(update, context)
            return
        if mode is None and text == "Рецепты":
            await self._send_recipe_list(update, context, page=0)
            return
        if mode is None and text == "Поиск":
            await self.search_recipes(update, context)
            return
        if mode is None and text == "Обновить":
            await self.refresh(update, context)
            return
        if mode is None and text == "Аккаунты":
            await self.accounts(update, context)
            return
        if mode == "recipe_search":
            await self._handle_recipe_search(update, context, text)
        elif mode == "fatsecret_login":
            await self._handle_fatsecret_login(update, context, text)
        elif mode == "fatsecret_password":
            await self._handle_fatsecret_password(update, context, text)
        else:
            await update.effective_message.reply_text(
                "Выбери действие кнопками ниже.",
                reply_markup=MAIN_KEYBOARD,
            )

    async def _cancel_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        recipe_id = context.user_data.get("recipe_id")
        context.user_data.clear()
        if recipe_id and (recipe := self.storage.get_recipe(recipe_id)):
            await update.effective_message.reply_text(
                _format_recipe(recipe),
                reply_markup=_recipe_actions_keyboard(recipe.id),
                parse_mode=ParseMode.HTML,
            )
            return
        await update.effective_message.reply_text("Ок, отменил.", reply_markup=MAIN_KEYBOARD)

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
        await status.edit_text("FatSecret аккаунт подключен. Загружаю рецепты из этого аккаунта...")
        try:
            imported = await self.sync_engine.refresh_account_recipes(account)
        except Exception as exc:  # noqa: BLE001
            logger.exception("FatSecret cookbook import failed after account connect")
            await status.edit_text(
                f"Аккаунт подключен, но рецепты не загрузились: {exc}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
            return
        await status.edit_text(
            f"FatSecret аккаунт подключен. Загружено/смёржено рецептов: {imported}.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Рецепты", callback_data="list:0")],
                    [InlineKeyboardButton("Аккаунты", callback_data="accounts:0")],
                ]
            ),
        )

    async def _handle_recipe_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        recipes = self._filter_recipes(text)
        context.user_data["recipe_search_query"] = text
        if not recipes:
            await update.effective_message.reply_text(
                f"По запросу «{html.escape(text)}» ничего не найдено.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Новый поиск", callback_data="search:0")]]),
                parse_mode=ParseMode.HTML,
            )
            return
        context.user_data.pop("mode", None)
        await update.effective_message.reply_text(
            f"Найдено рецептов: {len(recipes)}",
            reply_markup=self._recipe_list_keyboard(recipes, 0, "searchpage"),
        )

    async def _handle_edit_field(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        mode = str(context.user_data.get("mode", ""))
        field = mode.removeprefix("edit_")
        recipe_id = context.user_data.get("recipe_id")
        recipe = self.storage.get_recipe(recipe_id) if recipe_id else None
        if recipe is None or field not in EDIT_FIELD_LABELS:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст редактирования потерян.")
            return

        title = recipe.title
        description = recipe.description
        portions = recipe.portions
        prep_time = recipe.prep_time
        cook_time = recipe.cook_time
        try:
            if field == "title":
                if not text:
                    raise ValueError("Название не может быть пустым.")
                title = text
            elif field == "description":
                description = "" if text == "-" else text
            elif field == "portions":
                portions = Decimal(text.replace(",", "."))
            elif field == "prep":
                prep_time = int(text)
            elif field == "cook":
                cook_time = int(text)
        except (InvalidOperation, ValueError) as exc:
            await update.effective_message.reply_text(f"Не понял значение: {exc}")
            return

        self.storage.update_recipe_meta(
            recipe_id=recipe.id,
            title=title,
            description=description,
            portions=portions,
            prep_time=prep_time,
            cook_time=cook_time,
            updated_by=update.effective_user.id,
        )
        context.user_data.clear()
        await update.effective_message.reply_text("Сохранил локально. Синхронизирую в оба аккаунта...")
        await self._sync_after_text(update, recipe.id)
        if updated := self.storage.get_recipe(recipe.id):
            await update.effective_message.reply_text(
                _format_recipe(updated),
                reply_markup=_recipe_actions_keyboard(updated.id),
                parse_mode=ParseMode.HTML,
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
