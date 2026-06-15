from __future__ import annotations

import html
import logging
import re
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

from .models import FatSecretAccountConfig, Recipe, RecipeGroup
from .storage import Storage, normalize_title
from .sync import RecipeListItem, RecipeSyncEngine, ResolvedRecipeListItem

logger = logging.getLogger(__name__)
RECIPES_PAGE_SIZE = 8
RECIPE_LIST_CANDIDATES_PAGE_SIZE = 10
MAIN_BUTTONS = {"Рецепты", "Группы", "Аккаунты"}
RECIPE_LIST_LINE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<grams>\d+(?:[,.]\d+)?)$")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Рецепты"],
        ["Группы", "Аккаунты"],
    ],
    resize_keyboard=True,
)


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


def _recipe_owner_text(recipe: Recipe, account_labels: dict[str, str]) -> str:
    owners = [account_labels.get(key, key) for key in recipe.remote_ids if key in account_labels]
    if not owners and recipe.remote_ids:
        owners = list(recipe.remote_ids)
    if not owners:
        return "без аккаунта"
    return ", ".join(owners)


def _recipe_list_button_text(recipe: Recipe, account_labels: dict[str, str], prefix: str = "") -> str:
    text = f"{prefix}{recipe.title} - {_recipe_owner_text(recipe, account_labels)}"
    return text[:90]


def _default_account_label(username: str) -> str:
    label = username.strip().split("@", 1)[0].strip()
    return label[:24] or "FatSecret"


def _parse_recipe_list_lines(text: str) -> tuple[list[RecipeListItem], list[str]]:
    items: list[RecipeListItem] = []
    bad_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = RECIPE_LIST_LINE_RE.match(line)
        if match is None:
            bad_lines.append(line)
            continue
        try:
            grams = Decimal(match.group("grams").replace(",", "."))
        except InvalidOperation:
            bad_lines.append(line)
            continue
        if grams <= 0:
            bad_lines.append(line)
            continue
        items.append(RecipeListItem(query=match.group("name").strip(), grams=grams))
    return items, bad_lines


def _format_decimal(value: Decimal | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    quantum = Decimal("1") if digits == 0 else Decimal("0." + ("0" * (digits - 1)) + "1")
    return str(value.quantize(quantum)).rstrip("0").rstrip(".")


def _scaled_macro(value: Decimal | None, grams: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value * grams / Decimal("100")


def _format_item_title(item: ResolvedRecipeListItem) -> str:
    title = item.ingredient.title.strip()
    brand = item.brand.strip()
    if brand and brand.casefold() not in title.casefold():
        title = f"{title} ({brand[:60]})"
    return html.escape(title)


def _format_macros_per_100g(item: ResolvedRecipeListItem) -> str:
    return (
        f"{_format_decimal(item.energy_per_100g, 0)}/"
        f"{_format_decimal(item.protein_per_100g)}/"
        f"{_format_decimal(item.fat_per_100g)}/"
        f"{_format_decimal(item.carbohydrate_per_100g)}"
    )


def _format_resolved_item(item: ResolvedRecipeListItem) -> str:
    return f"- {_format_item_title(item)} | 100г: {_format_macros_per_100g(item)} | масса: {_format_decimal(item.grams)}г"


def _sum_known_macros(values: list[Decimal | None]) -> Decimal | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known, Decimal("0"))


def _format_recipe_list_draft(title: str, items: list[ResolvedRecipeListItem]) -> str:
    energy = _sum_known_macros([_scaled_macro(item.energy_per_100g, item.grams) for item in items])
    protein = _sum_known_macros([_scaled_macro(item.protein_per_100g, item.grams) for item in items])
    fat = _sum_known_macros([_scaled_macro(item.fat_per_100g, item.grams) for item in items])
    carbs = _sum_known_macros([_scaled_macro(item.carbohydrate_per_100g, item.grams) for item in items])
    lines = [
        f"<b>Рецепт: {html.escape(title)}</b>",
        f"Итого ккал/Б/Ж/У: {_format_decimal(energy, 0)}/{_format_decimal(protein)}/{_format_decimal(fat)}/{_format_decimal(carbs)}",
        "",
        "<b>Ингредиенты</b>",
        *(_format_resolved_item(item) for item in items),
    ]
    return "\n".join(lines)


def _recipe_list_draft_keyboard(items: list[ResolvedRecipeListItem]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"Заменить: {item.ingredient.title[:42]}",
                callback_data=f"recipe_list_replace:{index}",
            )
        ]
        for index, item in enumerate(items)
    ]
    buttons.append([InlineKeyboardButton("Создать рецепт", callback_data="recipe_list_confirm:0")])
    buttons.append([InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")])
    return InlineKeyboardMarkup(buttons)


def _recipe_list_candidate_keyboard(
    candidates: list[ResolvedRecipeListItem],
    page: int,
    has_next: bool,
) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"{page * RECIPE_LIST_CANDIDATES_PAGE_SIZE + index + 1}. {item.ingredient.title[:46]}",
                callback_data=f"recipe_list_pick:{index}",
            )
        ]
        for index, item in enumerate(candidates)
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Назад", callback_data=f"recipe_list_cpage:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}", callback_data=f"recipe_list_cpage:{page}"))
    if has_next:
        nav.append(InlineKeyboardButton("Дальше", callback_data=f"recipe_list_cpage:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")])
    return InlineKeyboardMarkup(buttons)


def _format_recipe_list_candidates(
    query: str,
    grams: Decimal,
    candidates: list[ResolvedRecipeListItem],
    page: int,
) -> str:
    lines = [
        f"Варианты для <b>{html.escape(query)}</b>. Масса останется {_format_decimal(grams)}г.",
        "",
    ]
    for index, item in enumerate(candidates, start=1):
        number = page * RECIPE_LIST_CANDIDATES_PAGE_SIZE + index
        lines.append(f"{number}. {_format_resolved_item(item)[2:]}")
    return "\n".join(lines)


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
        app.add_handler(CommandHandler("groups", self.groups))
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

    async def _require_active_group(self, update: Update) -> RecipeGroup | None:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return None
        group = self.storage.active_group_for_user(user.id)
        if group is None:
            await message.reply_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id),
                parse_mode=ParseMode.HTML,
            )
            return None
        return group

    async def _require_active_group_query(self, query, telegram_id: int) -> RecipeGroup | None:
        group = self.storage.active_group_for_user(telegram_id)
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(telegram_id),
                parse_mode=ParseMode.HTML,
            )
            return None
        return group

    async def _require_recipe_in_active_group(self, query, recipe: Recipe | None) -> bool:
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return False
        user = query.from_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None or recipe.group_id != group.id:
            await query.edit_message_text(
                "Этот рецепт не из активной группы. Переключи группу и открой рецепт из списка заново.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return False
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        if self.storage.active_group_for_user(update.effective_user.id) is None:
            await update.effective_message.reply_text(
                "Готов. Для синхронизации рецептов нужна группа.",
                reply_markup=self._groups_keyboard(update.effective_user.id),
                parse_mode=ParseMode.HTML,
            )
            return
        await update.effective_message.reply_text(
            "Готов. Создавай и редактируй рецепты в FatSecret, а здесь обновляй список и синхронизируй выбранный рецепт.",
            reply_markup=MAIN_KEYBOARD,
        )

    def _groups_text(self, telegram_id: int) -> str:
        active = self.storage.active_group_for_user(telegram_id)
        if active is None:
            return "Ты пока не в группе. Создай группу или подключись по коду."
        lines = [
            "<b>Моя группа</b>",
            f"Название: {html.escape(active.name)}",
            f"Код для подключения: <code>{html.escape(active.invite_code)}</code>",
            "",
            "<b>Участники</b>",
        ]
        for member in self.storage.group_members(active.id):
            account = (
                f" - {html.escape(member.fatsecret_label)}"
                if member.fatsecret_label
                else " - FatSecret не подключен"
            )
            lines.append(f"- {html.escape(member.display_name)}{account}")
        return "\n".join(lines)

    def _groups_keyboard(self, telegram_id: int) -> InlineKeyboardMarkup:
        active = self.storage.active_group_for_user(telegram_id)
        if active is not None:
            buttons = []
            if self.storage.active_group_created_by(telegram_id):
                buttons.append([InlineKeyboardButton("Переименовать группу", callback_data="group_rename:0")])
            buttons.append([InlineKeyboardButton("Отключиться от группы", callback_data="group_leave:0")])
            return InlineKeyboardMarkup(buttons)
        buttons: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton("Создать группу", callback_data="group_create:0"),
                InlineKeyboardButton("Подключиться", callback_data="group_join:0"),
            ]
        ]
        return InlineKeyboardMarkup(buttons)

    async def groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        await update.effective_message.reply_text(
            self._groups_text(update.effective_user.id),
            reply_markup=self._groups_keyboard(update.effective_user.id),
            parse_mode=ParseMode.HTML,
        )

    def _accounts_text(self, group: RecipeGroup) -> str:
        accounts = self.storage.list_fatsecret_accounts(group.id)
        if not accounts:
            return f"<b>{html.escape(group.name)}</b>\nFatSecret аккаунты в этой группе еще не подключены."
        lines = [f"<b>{html.escape(group.name)}</b>\nПодключено FatSecret аккаунтов: {len(accounts)}/2"]
        for account in accounts:
            lines.append(f"- {html.escape(account.label)}: {html.escape(account.username)}")
        if len(accounts) < 2:
            lines.append("\nПодключи второй аккаунт, чтобы синхронизация шла в обе стороны.")
        return "\n".join(lines)

    def _accounts_keyboard(self, telegram_id: int, group: RecipeGroup) -> InlineKeyboardMarkup:
        accounts = self.storage.list_fatsecret_accounts(group.id)
        existing = self.storage.get_fatsecret_account_by_telegram_id(telegram_id)
        buttons: list[list[InlineKeyboardButton]] = []
        if existing is None and len(accounts) < 2:
            buttons.append([InlineKeyboardButton("Подключить FatSecret", callback_data="account_add:0")])
        for account in accounts:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Поменять ник: {account.label[:32]}",
                        callback_data=f"account_label:{account.key}",
                    )
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Выйти: {account.label[:42]}",
                        callback_data=f"account_logout:{account.key}",
                    )
                ]
            )
        return InlineKeyboardMarkup(buttons)

    def _active_group_account(
        self,
        telegram_id: int,
        account_key: str,
    ) -> tuple[RecipeGroup | None, FatSecretAccountConfig | None]:
        group = self.storage.active_group_for_user(telegram_id)
        if group is None:
            return None, None
        accounts = {account.key: account for account in self.storage.list_fatsecret_accounts(group.id)}
        return group, accounts.get(account_key)

    async def accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        group = await self._require_active_group(update)
        if group is None:
            return
        await update.effective_message.reply_text(
            self._accounts_text(group),
            reply_markup=self._accounts_keyboard(update.effective_user.id, group),
            parse_mode=ParseMode.HTML,
        )

    async def _edit_accounts(self, query, telegram_id: int) -> None:
        group = self.storage.active_group_for_user(telegram_id)
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(telegram_id),
            )
            return
        await query.edit_message_text(
            self._accounts_text(group),
            reply_markup=self._accounts_keyboard(telegram_id, group),
            parse_mode=ParseMode.HTML,
        )

    async def refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        group = await self._require_active_group(update)
        if group is None:
            return
        msg = await update.effective_message.reply_text(f"Обновляю рецепты группы «{group.name}» из FatSecret...")
        try:
            imported = await self.sync_engine.refresh_remote_recipes(group.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh failed")
            await msg.edit_text(f"Ошибка обновления: {exc}")
            return
        await msg.edit_text(f"Готово. Импортировано/смёржено записей: {imported}.")
        await self._send_recipe_list(update, context, page=0)

    async def recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        if await self._require_active_group(update) is None:
            return
        await self._send_recipe_list(update, context, page=0)

    async def _send_recipe_list(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        page: int,
    ) -> None:
        group = self.storage.active_group_for_user(update.effective_user.id)
        if group is None:
            await update.effective_message.reply_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(update.effective_user.id),
            )
            return
        recipes = self.storage.list_recipes(group.id)
        if not recipes:
            await update.effective_message.reply_text(
                "Рецептов пока нет. Создай рецепт в FatSecret и обнови список командой /refresh.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Создать из списка", callback_data="recipe_list_create:0")]]
                ),
            )
            return
        await update.effective_message.reply_text(
            "Общий список рецептов:",
            reply_markup=self._recipe_list_keyboard(recipes, page, "list", self._account_labels_for_group(group.id)),
        )

    def _account_labels_for_group(self, group_id: str | None) -> dict[str, str]:
        return {account.key: account.label for account in self.storage.list_fatsecret_accounts(group_id)}

    def _recipe_list_keyboard(
        self,
        recipes: list[Recipe],
        page: int,
        page_action: str,
        account_labels: dict[str, str] | None = None,
    ) -> InlineKeyboardMarkup:
        account_labels = account_labels or {}
        page = max(0, page)
        total_pages = max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        current = recipes[start : start + RECIPES_PAGE_SIZE]
        buttons = [
            [InlineKeyboardButton(_recipe_list_button_text(recipe, account_labels), callback_data=f"open:{recipe.id}")]
            for recipe in current
        ]
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
                InlineKeyboardButton("Создать из списка", callback_data="recipe_list_create:0"),
            ]
        )
        buttons.append([InlineKeyboardButton("Удалить несколько", callback_data=f"batchdel:{page}")])
        return InlineKeyboardMarkup(buttons)

    def _filter_recipes(self, query: str, group_id: str) -> list[Recipe]:
        terms = normalize_title(query).split()
        if not terms:
            return []
        matches: list[Recipe] = []
        for recipe in self.storage.list_recipes(group_id):
            haystack = normalize_title(
                " ".join([recipe.title, recipe.description, *(item.title for item in recipe.ingredients)])
            )
            if all(term in haystack for term in terms):
                matches.append(recipe)
        return matches

    async def search_recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        group = await self._require_active_group(update)
        if group is None:
            return
        context.user_data.clear()
        context.user_data["mode"] = "recipe_search"
        context.user_data["group_id"] = group.id
        await update.effective_message.reply_text("Что искать в рецептах? Пришли часть названия или ингредиента.")

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
        elif action == "groups":
            context.user_data.clear()
            await query.edit_message_text(
                self._groups_text(update.effective_user.id),
                reply_markup=self._groups_keyboard(update.effective_user.id),
                parse_mode=ParseMode.HTML,
            )
        elif action == "group_create":
            context.user_data.clear()
            context.user_data["mode"] = "group_create"
            await query.edit_message_text(
                "Пришли название новой группы.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="groups:0")]]),
            )
        elif action == "group_join":
            context.user_data.clear()
            context.user_data["mode"] = "group_join"
            await query.edit_message_text(
                "Пришли код группы.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="groups:0")]]),
            )
        elif action == "group_rename":
            context.user_data.clear()
            context.user_data["mode"] = "group_rename"
            await query.edit_message_text(
                "Пришли новое название группы.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="groups:0")]]),
            )
        elif action == "group_leave":
            context.user_data.clear()
            left = self.storage.leave_active_group(update.effective_user.id)
            await query.edit_message_text(
                "Отключился от группы." if left else "Ты сейчас не в группе.",
                reply_markup=self._groups_keyboard(update.effective_user.id),
                parse_mode=ParseMode.HTML,
            )
        elif action == "accounts":
            context.user_data.clear()
            await self._edit_accounts(query, update.effective_user.id)
        elif action == "account_add":
            await self._start_account_add(query, context, update.effective_user.id)
        elif action == "account_label":
            context.user_data.clear()
            _, account = self._active_group_account(update.effective_user.id, value)
            if account is None:
                await query.edit_message_text(
                    "Этот FatSecret аккаунт не из твоей активной группы.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
                )
                return
            context.user_data["mode"] = "account_label"
            context.user_data["account_label_key"] = account.key
            await query.edit_message_text(
                f"Пришли новый короткий ник для «{html.escape(account.label)}».",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Назад к аккаунтам", callback_data="accounts:0")]]
                ),
                parse_mode=ParseMode.HTML,
            )
        elif action in {"account_logout", "account_remove"}:
            context.user_data.clear()
            _, account = self._active_group_account(update.effective_user.id, value)
            if account is None:
                await query.edit_message_text(
                    "Этот FatSecret аккаунт не из твоей активной группы.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
                )
                return
            await query.edit_message_text(
                f"Выйти из FatSecret аккаунта «{html.escape(account.label)}» в боте?\n"
                "Сам аккаунт и рецепты в FatSecret не удалятся.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Да, выйти", callback_data=f"account_logout_confirm:{account.key}")],
                        [InlineKeyboardButton("Назад к аккаунтам", callback_data="accounts:0")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
        elif action in {"account_logout_confirm", "account_remove_confirm"}:
            context.user_data.clear()
            _, account = self._active_group_account(update.effective_user.id, value)
            if account is None:
                await query.edit_message_text(
                    "Этот FatSecret аккаунт не из твоей активной группы.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
                )
                return
            removed = self.storage.delete_fatsecret_account(value)
            await query.edit_message_text(
                "Вышел из FatSecret аккаунта в боте." if removed else "FatSecret аккаунт уже отключен или не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
        elif action == "list":
            context.user_data.clear()
            await self._edit_recipe_list(query, int(value or "0"))
        elif action == "search":
            context.user_data.clear()
            group = await self._require_active_group_query(query, update.effective_user.id)
            if group is None:
                return
            context.user_data["mode"] = "recipe_search"
            context.user_data["group_id"] = group.id
            await query.edit_message_text("Что искать в рецептах? Пришли часть названия или ингредиента.")
        elif action == "searchpage":
            await self._edit_search_results(query, context, int(value or "0"))
        elif action == "recipe_list_create":
            context.user_data.clear()
            if await self._require_active_group_query(query, update.effective_user.id) is None:
                return
            context.user_data["mode"] = "recipe_list_title"
            await query.edit_message_text(
                "Пришли название рецепта.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="list:0")]]),
            )
        elif action == "recipe_list_confirm":
            await self._create_recipe_list_from_draft(query, context, update.effective_user.id)
        elif action == "recipe_list_replace":
            await self._start_recipe_list_replace(query, context, int(value or "0"))
        elif action == "recipe_list_pick":
            await self._pick_recipe_list_candidate(query, context, int(value or "0"))
        elif action == "recipe_list_cpage":
            await self._show_recipe_list_replacements(query, context, int(value or "0"))
        elif action == "recipe_list_back":
            await self._edit_recipe_list_draft(query, context)
        elif action == "recipe_list_cancel":
            context.user_data.clear()
            await self._edit_recipe_list(query, 0)
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
        group = self.storage.active_group_for_user(telegram_id)
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(telegram_id),
            )
            return
        existing = self.storage.get_fatsecret_account_by_telegram_id(telegram_id)
        if existing is None and self.storage.fatsecret_account_count(group.id) >= 2:
            await query.edit_message_text("Уже подключены два FatSecret аккаунта. Сначала удали один из них.")
            return
        context.user_data.clear()
        context.user_data["mode"] = "fatsecret_login"
        context.user_data["group_id"] = group.id
        await query.edit_message_text("Пришли логин или email от FatSecret. Сообщение я постараюсь удалить после чтения.")

    async def _edit_recipe_list(self, query, page: int) -> None:
        user = query.from_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return
        recipes = self.storage.list_recipes(group.id)
        if not recipes:
            await query.edit_message_text("Рецептов пока нет.")
            return
        await query.edit_message_text(
            "Общий список рецептов:",
            reply_markup=self._recipe_list_keyboard(recipes, page, "list", self._account_labels_for_group(group.id)),
        )

    async def _edit_search_results(self, query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        search_query = context.user_data.get("recipe_search_query")
        group_id = context.user_data.get("group_id")
        if not search_query:
            await query.edit_message_text(
                "Поиск устарел. Запусти поиск заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Поиск", callback_data="search:0")]]),
            )
            return
        if not group_id:
            await query.edit_message_text("Группа поиска устарела. Запусти поиск заново.")
            return
        recipes = self._filter_recipes(search_query, group_id)
        if not recipes:
            await query.edit_message_text(
                f"По запросу «{html.escape(search_query)}» ничего не найдено.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Новый поиск", callback_data="search:0")]]),
                parse_mode=ParseMode.HTML,
            )
            return
        await query.edit_message_text(
            f"Найдено рецептов: {len(recipes)}",
            reply_markup=self._recipe_list_keyboard(recipes, page, "searchpage", self._account_labels_for_group(group_id)),
        )

    async def _refresh_from_callback(self, query) -> None:
        user = query.from_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return
        await query.edit_message_text(f"Обновляю рецепты группы «{group.name}» из FatSecret...")
        try:
            imported = await self.sync_engine.refresh_remote_recipes(group.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh failed")
            await query.edit_message_text(f"Ошибка обновления: {exc}")
            return
        await query.edit_message_text(f"Готово. Импортировано/смёржено записей: {imported}.")

    async def _open_recipe(self, query, recipe_id: str) -> None:
        local_recipe = self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, local_recipe):
            return
        recipe = await self.sync_engine.hydrate_recipe_from_remote(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        await query.edit_message_text(
            _format_recipe(recipe),
            reply_markup=_recipe_actions_keyboard(recipe.id),
            parse_mode=ParseMode.HTML,
        )

    async def _open_sync_menu(self, query, recipe_id: str) -> None:
        recipe = self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        accounts = {account.key: account.label for account in self.storage.list_fatsecret_accounts(recipe.group_id)}
        source_keys = [key for key in recipe.remote_ids if key in accounts]
        if not source_keys:
            await query.edit_message_text(
                "У рецепта нет привязки к подключенным FatSecret аккаунтам. Обнови группу из основной клавиатуры и попробуй снова.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
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
        recipe = self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        account_labels = {
            account.key: account.label
            for account in self.storage.list_fatsecret_accounts(recipe.group_id)
        }
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
        if not await self._require_recipe_in_active_group(query, recipe):
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
        recipe = self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts(recipe.group_id)}
        await query.edit_message_text("Удаляю рецепт в FatSecret...")
        try:
            results = await self.sync_engine.delete_recipe_everywhere(recipe_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("delete failed")
            await query.edit_message_text(f"Ошибка удаления: {exc}")
            return
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
        user = query.from_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return
        recipes = self.storage.list_recipes(group.id)
        if not recipes:
            await query.edit_message_text("Рецептов пока нет.")
            return
        context.user_data["mode"] = "batch_delete"
        context.user_data["group_id"] = group.id
        selected = self._batch_delete_ids(context)
        selected.intersection_update({recipe.id for recipe in recipes})
        await query.edit_message_text(
            f"Выбери рецепты для удаления из FatSecret. Отмечено: {len(selected)}",
            reply_markup=self._batch_delete_keyboard(
                recipes,
                page,
                selected,
                self._account_labels_for_group(group.id),
            ),
        )

    async def _toggle_batch_delete(self, query, context: ContextTypes.DEFAULT_TYPE, value: str) -> None:
        recipe_id, _, page_text = value.partition(":")
        selected = self._batch_delete_ids(context)
        if recipe_id in selected:
            selected.remove(recipe_id)
        else:
            selected.add(recipe_id)
        await self._open_batch_delete(query, context, int(page_text or "0"))

    def _batch_delete_keyboard(
        self,
        recipes: list[Recipe],
        page: int,
        selected: set[str],
        account_labels: dict[str, str],
    ) -> InlineKeyboardMarkup:
        page = max(0, page)
        total_pages = max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        current = recipes[start : start + RECIPES_PAGE_SIZE]
        buttons = [
            [
                InlineKeyboardButton(
                    _recipe_list_button_text(
                        recipe,
                        account_labels,
                        prefix=f"{'[x]' if recipe.id in selected else '[ ]'} ",
                    ),
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
        group_id = context.user_data.get("group_id")
        if not group_id:
            await query.edit_message_text("Группа выбора устарела. Начни batch-удаление заново.")
            return
        selected_recipes = [recipe for recipe in self.storage.list_recipes(group_id) if recipe.id in selected]
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
        group_id = context.user_data.get("group_id")
        if not group_id:
            await query.edit_message_text("Группа выбора устарела. Начни batch-удаление заново.")
            return
        recipes = self.storage.list_recipes(group_id)
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
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts(group_id)}
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
                ]
            ),
        )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        mode = context.user_data.get("mode")
        text = update.effective_message.text.strip()
        if mode is not None and text.casefold() in {"отмена", "назад"}:
            await self._cancel_mode(update, context)
            return
        if text in MAIN_BUTTONS:
            context.user_data.clear()
            mode = None
        if mode is None and text == "Рецепты":
            await self._send_recipe_list(update, context, page=0)
            return
        if mode is None and text == "Аккаунты":
            await self.accounts(update, context)
            return
        if mode is None and text == "Группы":
            await self.groups(update, context)
            return
        if mode == "recipe_search":
            await self._handle_recipe_search(update, context, text)
        elif mode == "recipe_list_title":
            await self._handle_recipe_list_title(update, context, text)
        elif mode == "recipe_list_items":
            await self._handle_recipe_list_items(update, context, text)
        elif mode == "recipe_list_replace_query":
            await self._handle_recipe_list_replace_query(update, context, text)
        elif mode == "group_create":
            await self._handle_group_create(update, context, text)
        elif mode == "group_join":
            await self._handle_group_join(update, context, text)
        elif mode == "group_rename":
            await self._handle_group_rename(update, context, text)
        elif mode == "fatsecret_login":
            await self._handle_fatsecret_login(update, context, text)
        elif mode == "fatsecret_password":
            await self._handle_fatsecret_password(update, context, text)
        elif mode == "fatsecret_label":
            await self._handle_fatsecret_label(update, context, text)
        elif mode == "account_label":
            await self._handle_account_label(update, context, text)
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

    async def _handle_group_create(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        if user is None:
            return
        group = self.storage.create_group(user.id, text)
        context.user_data.clear()
        await update.effective_message.reply_text(
            f"Группа создана: {html.escape(group.name)}\nКод для второго пользователя: <code>{group.invite_code}</code>",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )

    async def _handle_group_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        if user is None:
            return
        group = self.storage.join_group_by_code(user.id, text)
        if group is None:
            await update.effective_message.reply_text("Не нашел группу с таким кодом. Проверь код и пришли еще раз.")
            return
        context.user_data.clear()
        await update.effective_message.reply_text(
            f"Подключился к группе: {html.escape(group.name)}.",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )

    async def _handle_group_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        if user is None:
            return
        group = self.storage.rename_active_group(user.id, text)
        if group is None:
            await update.effective_message.reply_text("Переименовать группу может только создатель. Название не должно быть пустым.")
            return
        context.user_data.clear()
        await update.effective_message.reply_text(
            f"Группа переименована: {html.escape(group.name)}.",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML,
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
        group_id = context.user_data.get("group_id")
        group = self.storage.active_group_for_user(user.id)
        group_id = group_id or (group.id if group else None)
        if group_id is None:
            context.user_data.clear()
            await update.effective_chat.send_message("Сначала создай группу или подключись к группе.")
            return
        if existing is None and self.storage.fatsecret_account_count(group_id) >= 2:
            context.user_data.clear()
            await update.effective_chat.send_message("Уже подключены два FatSecret аккаунта. Сначала удали один из них.")
            return

        account = FatSecretAccountConfig(
            key=f"tg{user.id}",
            label=_default_account_label(username),
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

        context.user_data.clear()
        context.user_data["mode"] = "fatsecret_label"
        context.user_data["fatsecret_pending"] = {
            "username": account.username,
            "password": account.password,
            "market": account.market,
            "language": account.language,
            "group_id": group_id,
            "default_label": account.label,
        }
        await status.edit_text(
            "Логин принят. Пришли короткий ник для кнопок и списков.\n"
            "Потом его можно поменять в «Аккаунтах».\n"
            f"Например: <code>{html.escape(account.label)}</code>\n"
            "Отправь <code>-</code>, чтобы взять этот вариант.",
            parse_mode=ParseMode.HTML,
        )
        return

    async def _handle_fatsecret_label(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        pending = context.user_data.get("fatsecret_pending")
        if user is None or not isinstance(pending, dict):
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст подключения потерян. Нажми «Аккаунты» и начни заново.")
            return
        default_label = str(pending.get("default_label") or "FatSecret")
        label = default_label if text.strip() == "-" else text.strip()
        if not label:
            await update.effective_message.reply_text("Ник не должен быть пустым. Пришли короткое имя или `-`.")
            return
        account = FatSecretAccountConfig(
            key=f"tg{user.id}",
            label=label[:32],
            username=str(pending["username"]),
            password=str(pending["password"]),
            market=str(pending["market"]),
            language=str(pending["language"]),
        )
        group_id = str(pending["group_id"])
        self.storage.upsert_fatsecret_account(
            telegram_id=user.id,
            label=account.label,
            username=account.username,
            password=account.password,
            market=account.market,
            language=account.language,
        )
        context.user_data.clear()
        status = await update.effective_message.reply_text("FatSecret аккаунт подключен. Загружаю рецепты из этого аккаунта...")
        try:
            imported = await self.sync_engine.refresh_account_recipes(account, group_id)
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

    async def _handle_account_label(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        account_key = str(context.user_data.get("account_label_key") or "")
        label = text.strip()[:32]
        if user is None or not account_key:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст переименования потерян. Открой «Аккаунты» заново.")
            return
        group, account = self._active_group_account(user.id, account_key)
        if group is None or account is None:
            context.user_data.clear()
            await update.effective_message.reply_text(
                "Этот FatSecret аккаунт больше не найден в активной группе.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        if not label:
            await update.effective_message.reply_text("Ник не должен быть пустым. Пришли короткое имя.")
            return
        updated = self.storage.update_fatsecret_account_label(account_key, label)
        context.user_data.clear()
        await update.effective_message.reply_text(
            f"Ник обновлен: {html.escape(label)}." if updated else "Не удалось обновить ник.",
            reply_markup=self._accounts_keyboard(user.id, group),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_recipe_list_title(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        group = await self._require_active_group(update)
        if group is None:
            return
        title = text.strip()
        if not title:
            await update.effective_message.reply_text("Название не должно быть пустым.")
            return
        context.user_data["mode"] = "recipe_list_items"
        context.user_data["recipe_list_title"] = title
        context.user_data["group_id"] = group.id
        await update.effective_message.reply_text(
            "Пришли ингредиенты списком. Последнее число в строке считаю граммами.\n\n"
            "Например:\n"
            "Филе 100\n"
            "Теос греческий 200"
        )

    async def _handle_recipe_list_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        group_id = context.user_data.get("group_id")
        if user is None or not title or not group_id:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст создания рецепта потерян. Начни заново из списка рецептов.")
            return
        items, bad_lines = _parse_recipe_list_lines(text)
        if bad_lines:
            lines = "\n".join(f"- {html.escape(line)}" for line in bad_lines)
            await update.effective_message.reply_text(
                "Эти строки я совсем не понимаю:\n"
                f"{lines}\n\n"
                "Формат: название и последним токеном масса в граммах.",
                parse_mode=ParseMode.HTML,
            )
            return
        if not items:
            await update.effective_message.reply_text("Не вижу ингредиентов. Пришли строки вида: Филе 100")
            return
        status = await update.effective_message.reply_text("Подбираю ингредиенты по твоим прошлым рецептам и FatSecret...")
        try:
            draft = await self.sync_engine.resolve_recipe_list_items(str(group_id), items)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recipe list resolve failed")
            await status.edit_text(f"Не удалось подобрать ингредиенты: {exc}")
            return
        if draft.unresolved:
            lines = "\n".join(f"- {html.escape(line)}" for line in draft.unresolved)
            await status.edit_text(
                "Не нашел ингредиенты в FatSecret:\n"
                f"{lines}\n\n"
                "Попробуй уточнить названия и пришли список заново.",
                parse_mode=ParseMode.HTML,
            )
            return
        context.user_data["recipe_list_draft"] = draft.items
        context.user_data["mode"] = "recipe_list_confirm"
        await status.edit_text(
            _format_recipe_list_draft(title, draft.items),
            reply_markup=_recipe_list_draft_keyboard(draft.items),
            parse_mode=ParseMode.HTML,
        )

    async def _edit_recipe_list_draft(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        draft_items = context.user_data.get("recipe_list_draft")
        if not title or not isinstance(draft_items, list):
            await query.edit_message_text(
                "Черновик устарел. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        context.user_data["mode"] = "recipe_list_confirm"
        context.user_data.pop("recipe_list_replace_index", None)
        context.user_data.pop("recipe_list_candidates", None)
        await query.edit_message_text(
            _format_recipe_list_draft(title, draft_items),
            reply_markup=_recipe_list_draft_keyboard(draft_items),
            parse_mode=ParseMode.HTML,
        )

    async def _start_recipe_list_replace(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        draft_items = context.user_data.get("recipe_list_draft")
        if not isinstance(draft_items, list) or index < 0 or index >= len(draft_items):
            await query.edit_message_text(
                "Черновик устарел. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        item = draft_items[index]
        context.user_data["mode"] = "recipe_list_replace_query"
        context.user_data["recipe_list_replace_index"] = index
        context.user_data.pop("recipe_list_candidates", None)
        await query.edit_message_text(
            f"Что искать вместо «{html.escape(item.ingredient.title)}»?\n"
            f"Массу оставлю {_format_decimal(item.grams)}г.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_recipe_list_replace_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        user = update.effective_user
        group_id = context.user_data.get("group_id")
        draft_items = context.user_data.get("recipe_list_draft")
        index = context.user_data.get("recipe_list_replace_index")
        if user is None or not group_id or not isinstance(draft_items, list) or not isinstance(index, int):
            context.user_data.clear()
            await update.effective_message.reply_text(
                "Контекст замены потерян. Начни создание заново из списка рецептов."
            )
            return
        if index < 0 or index >= len(draft_items):
            context.user_data.clear()
            await update.effective_message.reply_text("Ингредиент в черновике больше не найден. Начни создание заново.")
            return
        search_query = text.strip()
        if not search_query:
            await update.effective_message.reply_text("Пришли название ингредиента для поиска.")
            return
        status = await update.effective_message.reply_text("Ищу варианты замены...")
        context.user_data["recipe_list_replace_query"] = search_query
        context.user_data["recipe_list_replace_page"] = 0
        await self._show_recipe_list_replacements(status, context, page=0)

    async def _edit_flow_message(self, target, text: str, **kwargs) -> None:
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, **kwargs)
            return
        await target.edit_text(text, **kwargs)

    async def _show_recipe_list_replacements(
        self,
        message,
        context: ContextTypes.DEFAULT_TYPE,
        page: int,
    ) -> None:
        group_id = context.user_data.get("group_id")
        draft_items = context.user_data.get("recipe_list_draft")
        index = context.user_data.get("recipe_list_replace_index")
        search_query = str(context.user_data.get("recipe_list_replace_query") or "").strip()
        if not group_id or not isinstance(draft_items, list) or not isinstance(index, int) or not search_query:
            context.user_data.clear()
            await self._edit_flow_message(
                message,
                "Контекст замены потерян. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        if index < 0 or index >= len(draft_items):
            context.user_data.clear()
            await self._edit_flow_message(
                message,
                "Ингредиент в черновике больше не найден. Начни создание заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        page = max(0, page)
        grams = draft_items[index].grams
        offset = page * RECIPE_LIST_CANDIDATES_PAGE_SIZE
        try:
            candidates = await self.sync_engine.recipe_list_candidates(
                str(group_id),
                search_query,
                grams,
                limit=RECIPE_LIST_CANDIDATES_PAGE_SIZE + 1,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("recipe list replacement search failed")
            await self._edit_flow_message(message, f"Не удалось найти замену: {exc}")
            return
        if not candidates:
            await self._edit_flow_message(
                message,
                f"Не нашел вариантов для «{html.escape(search_query)}». Пришли другой запрос.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        visible_candidates = candidates[:RECIPE_LIST_CANDIDATES_PAGE_SIZE]
        has_next = len(candidates) > RECIPE_LIST_CANDIDATES_PAGE_SIZE
        context.user_data["mode"] = "recipe_list_replace_query"
        context.user_data["recipe_list_replace_page"] = page
        context.user_data["recipe_list_candidates"] = visible_candidates
        await self._edit_flow_message(
            message,
            _format_recipe_list_candidates(search_query, grams, visible_candidates, page),
            reply_markup=_recipe_list_candidate_keyboard(visible_candidates, page, has_next),
            parse_mode=ParseMode.HTML,
        )

    async def _pick_recipe_list_candidate(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        candidate_index: int,
    ) -> None:
        draft_items = context.user_data.get("recipe_list_draft")
        candidates = context.user_data.get("recipe_list_candidates")
        replace_index = context.user_data.get("recipe_list_replace_index")
        if (
            not isinstance(draft_items, list)
            or not isinstance(candidates, list)
            or not isinstance(replace_index, int)
            or candidate_index < 0
            or candidate_index >= len(candidates)
            or replace_index < 0
            or replace_index >= len(draft_items)
        ):
            await query.edit_message_text(
                "Выбор замены устарел. Вернись к проверке и попробуй еще раз.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]
                ),
            )
            return
        draft_items[replace_index] = candidates[candidate_index]
        context.user_data["recipe_list_draft"] = draft_items
        await self._edit_recipe_list_draft(query, context)

    async def _create_recipe_list_from_draft(self, query, context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        group_id = context.user_data.get("group_id")
        draft_items = context.user_data.get("recipe_list_draft")
        if not title or not group_id or not isinstance(draft_items, list):
            await query.edit_message_text("Черновик устарел. Начни создание заново из списка рецептов.")
            return
        await query.edit_message_text("Создаю рецепт в FatSecret аккаунтах группы...")
        try:
            created = await self.sync_engine.create_recipe_from_list(str(group_id), title, draft_items, telegram_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recipe list create failed")
            await query.edit_message_text(f"Ошибка создания рецепта: {exc}")
            return
        context.user_data.clear()
        account_labels = self._account_labels_for_group(str(group_id))
        lines = [
            f"{account_labels.get(result.account_key, result.account_key)}: "
            f"{'OK' if result.ok else 'ERROR'} {result.remote_recipe_id or ''} {result.message}"
            for result in created.results
        ]
        await query.edit_message_text(
            "Создание завершено:\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Открыть рецепт", callback_data=f"open:{created.recipe_id}")],
                    [InlineKeyboardButton("К списку", callback_data="list:0")],
                ]
            ),
        )

    async def _handle_recipe_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        group_id = context.user_data.get("group_id")
        if not group_id:
            group = await self._require_active_group(update)
            if group is None:
                return
            group_id = group.id
        recipes = self._filter_recipes(text, group_id)
        context.user_data["recipe_search_query"] = text
        context.user_data["group_id"] = group_id
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
            reply_markup=self._recipe_list_keyboard(recipes, 0, "searchpage", self._account_labels_for_group(group_id)),
        )
