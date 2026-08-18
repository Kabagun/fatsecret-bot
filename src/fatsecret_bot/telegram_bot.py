from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import io
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .barcodes import BarcodeDecodeError, DecodedBarcode, decode_barcode_image, normalize_barcode
from .fatsecret_client import user_safe_error_message
from .models import (
    MAX_RECIPE_STEPS,
    CustomFoodDefinition,
    DiaryCopyPreview,
    DiaryCopyResult,
    FatSecretAccountConfig,
    Ingredient,
    Recipe,
    RecipeGroup,
    RemoteRecipeVariant,
)
from .nutrition import custom_food_macro_error
from .portions import grams_from_portion
from .storage import GroupMemberLimitError, Storage, normalize_title
from .sync import RecipeListItem, RecipeSyncEngine, ResolvedRecipeListItem

logger = logging.getLogger(__name__)
RECIPES_PAGE_SIZE = 8
RECIPE_LIST_CANDIDATES_PAGE_SIZE = 10
RECIPE_LIST_CANDIDATES_PREFETCH_PAGES = 2
RECIPE_LIST_CANDIDATES_PREFETCH_SIZE = RECIPE_LIST_CANDIDATES_PAGE_SIZE * RECIPE_LIST_CANDIDATES_PREFETCH_PAGES
DISPLAY_RECIPE_STEPS_LIMIT = 20
FOOD_USAGE_REFRESH_HOUR = 12
RECIPE_CACHE_KEY = "recipe_cache"
RECIPE_CACHE_GROUP_KEY = "recipe_cache_group_id"
RECIPE_CACHE_LOADED_KEY = "recipe_cache_loaded_at"
RECIPE_RENDER_KEY = "recipe_render_key"
RECIPE_SEARCH_IDS_KEY = "recipe_search_ids"
RECIPE_PRODUCT_DIFFERENCE_CACHE_KEY = "recipe_product_difference_cache"
RECIPE_PRODUCT_DIFFERENCE_FOOTER = "⚠️ — в рецепте есть различия между аккаунтами."
RECIPE_WARNING_CACHE_TTL_SECONDS = 10 * 60.0
RECIPE_WARNING_RENDER_TOKEN_KEY = "recipe_warning_render_token"
RECIPE_WARNING_RENDER_TASK_KEY = "recipe_warning_render_task"
TELEGRAM_SAFE_TEXT_LIMIT = 4000
MAIN_BUTTONS = {
    "Поиск рецептов",
    "Рецепты",
    "Создать из списка",
    "Создать продукт",
    "Меню / Дневник",
    "Группы",
    "Аккаунты",
}
LIST_WIDTH_LINE = "--------------------------------"
PORTION_DESCRIPTION_RE = re.compile(
    r"^\s*(?P<size>\d+(?:[\.,]\d+)?)\s*(?P<unit>г|гр|g|gram|грам|мл|ml)\b",
    re.IGNORECASE,
)
RECIPE_KEYBOARD_BUTTONS = {
    "Поиск",
    "Создать из списка",
    "Удалить несколько",
    "Синхронизировать",
    "Удалить",
    "В меню",
}
RECIPE_LIST_LINE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<grams>\d+(?:[,.]\d+)?)$")
RECIPE_LIST_PORTIONS_RE = re.compile(
    r"^\s*(?:порц(?:ий|ии|ия)?|servings?)\s*:?\s*(?P<portions>\d+(?:[,.]\d+)?)\s*$",
    re.IGNORECASE,
)
RECIPE_STEPS_HEADER_RE = re.compile(r"^\s*(?:шаги|приготовление|способ приготовления)\s*:?\s*(.*)$", re.IGNORECASE)
RECIPE_STEP_PREFIX_RE = re.compile(r"^\s*(?:\d+[\).]\s*|[-*]\s*)?(?P<step>.+?)\s*$")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Поиск рецептов", "Создать из списка"],
        ["Создать продукт", "Меню / Дневник"],
        ["Группы", "Аккаунты"],
    ],
    resize_keyboard=True,
)

DIARY_DATE_TOKEN = r"(?:\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}|сегодня|завтра|вчера)"
DIARY_RANGE_RE = re.compile(
    rf"^\s*(?P<start>{DIARY_DATE_TOKEN})(?:\s*(?:\.\.|—|–|\s-\s)\s*(?P<end>{DIARY_DATE_TOKEN}))?\s*$",
    re.IGNORECASE,
)
CUSTOM_FOOD_NUMBER_RE = re.compile(r"(?<!\w)[+-]?\d+(?:[,.]\d+)?(?!\w)")


def _parse_diary_date(value: str, today: dt.date | None = None) -> dt.date:
    current = today or dt.date.today()
    normalized = value.strip().casefold()
    if normalized == "сегодня":
        return current
    if normalized == "завтра":
        return current + dt.timedelta(days=1)
    if normalized == "вчера":
        return current - dt.timedelta(days=1)
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(normalized, date_format).date()
        except ValueError:
            continue
    raise ValueError("Дата должна быть в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")


def _parse_diary_range(value: str, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    match = DIARY_RANGE_RE.fullmatch(value)
    if match is None:
        raise ValueError("Диапазон: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ; для одного дня укажи одну дату.")
    start = _parse_diary_date(match.group("start"), today=today)
    end = _parse_diary_date(match.group("end") or match.group("start"), today=today)
    return start, end


def _format_steps_lines(steps: list[str], limit: int = DISPLAY_RECIPE_STEPS_LIMIT) -> list[str]:
    clean_steps = [step.strip() for step in steps if step.strip()]
    lines = [
        f"{index}. {html.escape(step)}"
        for index, step in enumerate(clean_steps[:limit], start=1)
    ]
    if len(clean_steps) > limit:
        lines.append(f"...и еще {len(clean_steps) - limit} шагов")
    return lines


def _format_recipe(recipe: Recipe) -> str:
    ingredients = "\n".join(
        f"- {html.escape(item.title)}: {html.escape(_format_ingredient_amount(item))}"
        for item in recipe.ingredients
    )
    if not ingredients:
        ingredients = "Ингредиентов пока нет."
    description = f"\n\n{html.escape(recipe.description)}" if recipe.description else ""
    steps = "\n".join(_format_steps_lines(recipe.steps))
    steps_text = f"\n\n<b>Шаги</b>\n{steps}" if steps else ""
    return (
        f"<b>{html.escape(recipe.title)}</b>\n"
        f"Порций: {_format_decimal_plain(recipe.portions)}; "
        f"подготовка: {recipe.prep_time} мин; готовка: {recipe.cook_time} мин"
        f"{description}\n\n"
        f"<b>Ингредиенты</b>\n{ingredients}"
        f"{steps_text}"
    )


def _recipe_export_payload(recipe: Recipe) -> str:
    """Render the part of a recipe accepted by the existing list importer."""
    ingredient_lines: list[str] = []
    missing_weights: list[str] = []
    for ingredient in recipe.ingredients:
        grams = grams_from_portion(
            ingredient.amount,
            ingredient.portion_description,
            explicit_grams=ingredient.grams,
        )
        if grams is None or grams <= 0:
            missing_weights.append(ingredient.title.strip() or "Без названия")
            continue
        ingredient_lines.append(
            f"{ingredient.title.strip() or 'Без названия'} {_format_decimal_plain(grams)}"
        )
    if missing_weights:
        names = ", ".join(missing_weights)
        raise ValueError(f"Не удалось определить массу в граммах: {names}.")
    if not ingredient_lines:
        raise ValueError("В рецепте нет ингредиентов для экспорта.")
    lines = [f"Порций: {_format_decimal_plain(recipe.portions)}", *ingredient_lines]
    steps = [step.strip() for step in recipe.steps if step.strip()]
    if steps:
        lines.extend(["", "Шаги:"])
        lines.extend(f"{index}. {step}" for index, step in enumerate(steps, start=1))
    return "\n".join(lines)


def _format_recipe_conflict(
    variants: list[RemoteRecipeVariant],
    account_labels: dict[str, str],
) -> str:
    """Render only differing fields instead of repeating every complete recipe."""
    if not variants:
        return ""

    account_counts = Counter(variant.account_key for variant in variants)

    def label(variant: RemoteRecipeVariant) -> str:
        value = account_labels.get(variant.account_key, variant.account_key)
        if account_counts[variant.account_key] > 1:
            value = f"{value} (ID {variant.remote_recipe_id})"
        return value

    def add_field(lines: list[str], heading: str, values: list[str]) -> bool:
        if len(set(values)) <= 1:
            return False
        lines.extend(["", f"<b>{html.escape(heading)}</b>"])
        lines.extend(
            f"• {html.escape(label(variant))} — {html.escape(value) if value else 'не указано'}"
            for variant, value in zip(variants, values, strict=True)
        )
        return True

    title = variants[0].recipe.title.strip() or "Рецепт"
    lines = ["<b>Версии рецепта различаются</b>", "", f"<b>{html.escape(title)}</b>", "", "<b>Отличия</b>"]
    differences_found = False
    differences_found |= add_field(lines, "Название", [variant.recipe.title.strip() for variant in variants])
    differences_found |= add_field(
        lines,
        "Порций",
        [_format_decimal_plain(variant.recipe.portions) for variant in variants],
    )
    differences_found |= add_field(
        lines,
        "Подготовка",
        [f"{variant.recipe.prep_time} мин" for variant in variants],
    )
    differences_found |= add_field(
        lines,
        "Готовка",
        [f"{variant.recipe.cook_time} мин" for variant in variants],
    )
    differences_found |= add_field(
        lines,
        "Описание",
        [variant.recipe.description.strip() for variant in variants],
    )

    steps_by_variant = [
        [step.strip() for step in variant.recipe.steps if step.strip()]
        for variant in variants
    ]
    max_steps = max((len(steps) for steps in steps_by_variant), default=0)
    for index in range(max_steps):
        values = [steps[index] if index < len(steps) else "отсутствует" for steps in steps_by_variant]
        differences_found |= add_field(lines, f"Шаг {index + 1}", values)

    if not differences_found:
        lines.extend(["", "Различаются внутренние данные FatSecret."])
    lines.extend(["", "Ингредиенты и остальные поля совпадают."])
    return _truncate_html_lines(lines)


def _format_decimal_plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _parse_custom_food_macros(text: str) -> dict[str, Decimal]:
    """Parse the compact ``kcal protein fat carbs`` input used by the product wizard."""
    values = [Decimal(item.replace(",", ".")) for item in CUSTOM_FOOD_NUMBER_RE.findall(text)]
    if len(values) != 4:
        raise ValueError("Пришли ровно 4 числа: ккал, белки, жиры, углеводы.")
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("Значения должны быть неотрицательными числами.")
    calories, protein, fat, carbohydrate = values
    if calories > Decimal("10000") or any(
        value > Decimal("1000") for value in (protein, fat, carbohydrate)
    ):
        raise ValueError("Проверь значения на 100 г: одно из них слишком большое.")
    if error := custom_food_macro_error(calories, protein, fat, carbohydrate):
        raise ValueError(error)
    return {
        "calories": calories,
        "protein": protein,
        "totalFat": fat,
        "carbohydrate": carbohydrate,
    }


def _format_custom_food_draft(definition: CustomFoodDefinition) -> str:
    brand = (
        f"\nБренд: {html.escape(definition.manufacturer_name)}"
        if definition.manufacturer_name
        else "\nБренд: нет"
    )
    barcode = (
        f"\nШтрих-код: <code>{html.escape(definition.barcode)}</code>"
        if definition.barcode
        else "\nШтрих-код: нет"
    )
    return (
        f"<b>Новый продукт: {html.escape(definition.title)}</b>\n"
        "Значения на 100 г\n"
        f"Ккал: {_format_decimal_plain(definition.nutrients['calories'])}\n"
        f"Белки: {_format_decimal_plain(definition.nutrients['protein'])} г\n"
        f"Жиры: {_format_decimal_plain(definition.nutrients['totalFat'])} г\n"
        f"Углеводы: {_format_decimal_plain(definition.nutrients['carbohydrate'])} г"
        f"{brand}{barcode}\n\n"
        "Продукт будет создан во всех FatSecret аккаунтах активной группы."
    )


def _format_custom_food_created(
    title: str,
    food_ids: dict[str, str],
    account_labels: dict[str, str],
) -> str:
    lines = [
        f"{html.escape(account_labels.get(account_key, account_key))}: "
        f"<code>{html.escape(food_id)}</code>"
        for account_key, food_id in sorted(food_ids.items())
    ]
    return (
        f"Продукт <b>{html.escape(title)}</b> создан и проверен:\n"
        + "\n".join(lines)
        + "\n\nFatSecret может добавить новый продукт в поиск с задержкой в несколько минут. "
        "Если его пока нет, подожди и повтори поиск в нужном аккаунте по точному названию."
    )


def _custom_food_barcode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Без штрих-кода", callback_data="food_skip_barcode:0")],
            [InlineKeyboardButton("Отмена", callback_data="food_cancel:0")],
        ]
    )


def _custom_food_brand_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Без бренда", callback_data="food_skip_brand:0")],
            [InlineKeyboardButton("Отмена", callback_data="food_cancel:0")],
        ]
    )


def _custom_food_brand_suggestions_keyboard(
    suggestions: list[str],
    entered_brand: str,
    choice_token: str,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(brand[:60], callback_data=f"food_brand_pick:{choice_token}:{index}")]
        for index, brand in enumerate(suggestions)
    ]
    entered_label = entered_brand if len(entered_brand) <= 42 else f"{entered_brand[:39]}..."
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    f"Использовать введённое: {entered_label}",
                    callback_data=f"food_brand_custom:{choice_token}",
                )
            ],
            [InlineKeyboardButton("Без бренда", callback_data="food_skip_brand:0")],
            [InlineKeyboardButton("Отмена", callback_data="food_cancel:0")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def _custom_food_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Создать продукт", callback_data="food_create:0")],
            [InlineKeyboardButton("Изменить название", callback_data="food_change_title:0")],
            [InlineKeyboardButton("Отмена", callback_data="food_cancel:0")],
        ]
    )


def _portion_description_unit(portion_description: str) -> tuple[Decimal, str] | None:
    match = PORTION_DESCRIPTION_RE.search(portion_description.replace("\xa0", " "))
    if match is None:
        return None
    try:
        size = Decimal(match.group("size").replace(",", "."))
    except InvalidOperation:
        return None
    unit = "мл" if match.group("unit").casefold() in {"мл", "ml"} else "г"
    return size, unit


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


def _format_ingredient_amount(ingredient: Ingredient) -> str:
    amount = ingredient.amount
    grams = ingredient.grams
    portion_description = ingredient.portion_description
    if grams is not None:
        return f"{_format_decimal_plain(grams)}г"
    portion_unit = _portion_description_unit(portion_description)
    if portion_unit is not None:
        unit_size, unit = portion_unit
        return f"{_format_decimal_plain(amount * unit_size)}{unit}"
    number = _format_decimal_plain(amount)
    unit = _format_ingredient_unit(amount, portion_description)
    if not unit:
        return number
    if unit in {"г", "мл"}:
        return f"{number}{unit}"
    return f"{number} {unit}"


@dataclass(frozen=True)
class _RecipeProductComparison:
    same_products: tuple[Ingredient, ...]
    different_products: tuple[tuple[RemoteRecipeVariant, tuple[Ingredient, ...]], ...]

    @property
    def has_differences(self) -> bool:
        return any(products for _, products in self.different_products)


@dataclass(frozen=True)
class _RecipeWarningCacheEntry:
    signature: tuple[tuple[str, tuple[str, ...]], ...]
    has_differences: bool
    checked_at: float


@dataclass(frozen=True)
class _RecipeWarningScanResult:
    differences: dict[str, bool]
    failed: bool = False


def _recipe_remote_signature(
    recipe: Recipe,
    connected_account_keys: set[str],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (
            account_key,
            tuple(
                dict.fromkeys(
                    [
                        *(recipe.remote_ids_by_account.get(account_key) or []),
                        *([recipe.remote_ids[account_key]] if account_key in recipe.remote_ids else []),
                    ]
                )
            ),
        )
        for account_key in sorted(connected_account_keys)
    )


def _visible_ingredient_key(ingredient: Ingredient) -> tuple[str, str]:
    return normalize_title(ingredient.title), normalize_title(_format_ingredient_amount(ingredient))


def _compare_recipe_products(variants: list[RemoteRecipeVariant]) -> _RecipeProductComparison:
    """Compare visible ingredient names and amounts across account-specific recipe versions."""
    if not variants:
        return _RecipeProductComparison((), ())
    if len({variant.account_key for variant in variants}) < 2:
        return _RecipeProductComparison(tuple(variants[0].recipe.ingredients), ())

    counters = [Counter(_visible_ingredient_key(item) for item in variant.recipe.ingredients) for variant in variants]
    same_counts = counters[0].copy()
    for counter in counters[1:]:
        same_counts &= counter

    same_remaining = same_counts.copy()
    same_products: list[Ingredient] = []
    for ingredient in variants[0].recipe.ingredients:
        key = _visible_ingredient_key(ingredient)
        if same_remaining[key] <= 0:
            continue
        same_products.append(ingredient)
        same_remaining[key] -= 1

    different_products: list[tuple[RemoteRecipeVariant, tuple[Ingredient, ...]]] = []
    for variant in variants:
        remaining = same_counts.copy()
        differences: list[Ingredient] = []
        for ingredient in variant.recipe.ingredients:
            key = _visible_ingredient_key(ingredient)
            if remaining[key] > 0:
                remaining[key] -= 1
            else:
                differences.append(ingredient)
        different_products.append((variant, tuple(differences)))
    return _RecipeProductComparison(tuple(same_products), tuple(different_products))


def _recipe_versions_differ(
    variants: list[RemoteRecipeVariant],
    _connected_account_keys: set[str],
) -> bool:
    """Compare versions that exist; a missing account copy alone is not a difference."""
    if not variants:
        return True
    counts = Counter(variant.account_key for variant in variants)
    if any(count > 1 for count in counts.values()):
        return True
    return len({variant.fingerprint.digest for variant in variants}) > 1


def _format_product_difference_amount(ingredient: Ingredient) -> str:
    return re.sub(r"(?<=\d)(г|мл)$", r" \1", _format_ingredient_amount(ingredient))


def _format_product_difference_line(index: int, ingredient: Ingredient) -> str:
    title = ingredient.title.strip() or "Без названия"
    if len(title) > 240:
        title = title[:237].rstrip() + "…"
    return (
        f"{index}. {html.escape(title)} — "
        f"{html.escape(_format_product_difference_amount(ingredient))}"
    )


def _truncate_html_lines(lines: list[str], limit: int = 4000) -> str:
    rendered: list[str] = []
    current_length = 0
    for line in lines:
        added_length = len(line) + (1 if rendered else 0)
        if current_length + added_length > limit - 2:
            rendered.append("…")
            break
        rendered.append(line)
        current_length += added_length
    return "\n".join(rendered)


def _format_recipe_product_differences(
    comparison: _RecipeProductComparison,
    account_labels: dict[str, str],
) -> str:
    """Render visible ingredient differences grouped by FatSecret account."""
    if not comparison.different_products:
        return ""
    variants = [variant for variant, _ in comparison.different_products]
    title = variants[0].recipe.title.strip() or "Рецепт"
    account_counts = Counter(variant.account_key for variant in variants)
    difference_slots = max(len(products) for _, products in comparison.different_products)
    lines = [f"<b>{html.escape(title)}</b>"]
    for variant, products in comparison.different_products:
        label = account_labels.get(variant.account_key, variant.account_key)
        if account_counts[variant.account_key] > 1:
            label = f"{label} (ID {variant.remote_recipe_id})"
        lines.extend(["", f"<b>Продукты отличаются у {html.escape(label)}:</b>", ""])
        if products:
            lines.extend(
                _format_product_difference_line(index, ingredient)
                for index, ingredient in enumerate(products, start=1)
            )
        lines.extend(
            f"{index}. Отсутствует"
            for index in range(len(products) + 1, difference_slots + 1)
        )
    lines.extend(["", "<b>Совпадающие продукты:</b>", ""])
    if comparison.same_products:
        lines.extend(
            _format_product_difference_line(index, ingredient)
            for index, ingredient in enumerate(comparison.same_products, start=1)
        )
    else:
        lines.append("Отсутствуют.")
    return _truncate_html_lines(lines)


def _parse_open_recipe_value(value: str) -> tuple[str, int, str]:
    recipe_id, _, rest = value.partition(":")
    raw_page, _, raw_page_action = rest.partition(":")
    try:
        page = max(0, int(raw_page or "0"))
    except ValueError:
        page = 0
    page_action = raw_page_action if raw_page_action in {"list", "searchpage"} else "list"
    return recipe_id, page, page_action


def _recipe_actions_keyboard(
    recipe_id: str,
    page: int = 0,
    page_action: str = "list",
    total_pages: int = 1,
    *,
    can_sync: bool = False,
    export_variant_index: int = -1,
) -> InlineKeyboardMarkup:
    page_action = page_action if page_action in {"list", "searchpage"} else "list"
    page = max(0, page)
    buttons = [
        [
            InlineKeyboardButton(
                "Экспортировать",
                callback_data=f"recipe_export:{recipe_id}:{export_variant_index}",
            )
        ]
    ]
    if can_sync:
        buttons.append([InlineKeyboardButton("Синхронизировать", callback_data=f"sync:{recipe_id}")])
    buttons.extend(
        [
            [InlineKeyboardButton("Переименовать", callback_data=f"recipe_rename:{recipe_id}")],
            [InlineKeyboardButton("Удалить в FatSecret", callback_data=f"delete:{recipe_id}")],
            [InlineKeyboardButton("К списку", callback_data=f"{page_action}:{page}")],
        ]
    )
    return InlineKeyboardMarkup(
        buttons
    )


def _recipe_owner_text(recipe: Recipe, account_labels: dict[str, str]) -> str:
    owners = [account_labels.get(key, key) for key in recipe.remote_ids if key in account_labels]
    if not owners and recipe.remote_ids:
        owners = list(recipe.remote_ids)
    if not owners:
        return "без аккаунта"
    return ", ".join(owners)


def _recipe_list_button_text(
    recipe: Recipe,
    account_labels: dict[str, str],
    prefix: str = "",
    *,
    has_product_differences: bool = False,
) -> str:
    text = f"{prefix}{recipe.title} · {_recipe_owner_text(recipe, account_labels)}"
    marker = " ⚠️" if has_product_differences else ""
    return f"{text[: 90 - len(marker)].rstrip()}{marker}"


def _recipe_list_message(
    title: str,
    *,
    has_product_differences: bool = False,
    checking_versions: bool = False,
    needs_reload: bool = False,
) -> str:
    lines = [title, "Пришли текст в чат, чтобы искать по рецептам.", LIST_WIDTH_LINE]
    if has_product_differences:
        lines.append(RECIPE_PRODUCT_DIFFERENCE_FOOTER)
    if checking_versions:
        lines.append("⏳ Проверяю версии в фоне…")
    if needs_reload:
        lines.append("🔄 Данные о версиях старше 10 минут. Нажми «Обновить список».")
    return "\n".join(lines)


def _recipe_remote_identities(recipe: Recipe) -> set[tuple[str, str]]:
    identities = {(account_key, remote_id) for account_key, remote_id in recipe.remote_ids.items()}
    identities.update(
        (account_key, remote_id)
        for account_key, remote_ids in recipe.remote_ids_by_account.items()
        for remote_id in remote_ids
    )
    return identities


def _recipe_reference_for_identities(
    recipe_id: str,
    title: str,
    group_id: str | None,
    identities: set[tuple[str, str]],
) -> Recipe:
    """Build a live recipe reference containing only the supplied remote identities."""
    remote_ids_by_account: dict[str, list[str]] = {}
    for account_key, remote_id in sorted(identities):
        remote_ids_by_account.setdefault(account_key, []).append(remote_id)
    return Recipe(
        id=recipe_id,
        title=title,
        group_id=group_id,
        remote_ids={account_key: remote_ids[0] for account_key, remote_ids in remote_ids_by_account.items()},
        remote_ids_by_account=remote_ids_by_account,
    )


def _fresh_recipe_reference(reference: Recipe, live_recipes: list[Recipe]) -> Recipe | None:
    """Recover one logical selection by remote identity even after a partial title change."""
    wanted = _recipe_remote_identities(reference)
    live = set().union(*(_recipe_remote_identities(recipe) for recipe in live_recipes)) if live_recipes else set()
    existing = wanted & live
    if not existing:
        return None
    return _recipe_reference_for_identities(
        reference.id,
        reference.title,
        reference.group_id,
        existing,
    )


def _duplicate_recipe_reference(
    live_recipes: list[Recipe],
    title: str,
    *,
    exclude: set[tuple[str, str]],
) -> Recipe | None:
    """Return every live identity with a colliding title except the selected recipe itself."""
    normalized = normalize_title(title)
    identities = set().union(
        *(
            _recipe_remote_identities(recipe)
            for recipe in live_recipes
            if normalize_title(recipe.title) == normalized
        )
    ) if live_recipes else set()
    identities -= exclude
    if not identities:
        return None
    group_id = next((recipe.group_id for recipe in live_recipes if normalize_title(recipe.title) == normalized), None)
    return _recipe_reference_for_identities(
        f"rename-conflict:{normalized}",
        title,
        group_id,
        identities,
    )


def _next_live_recipe_title(title: str, recipes: list[Recipe]) -> str:
    """Return the first numeric copy title absent from a fresh cookbook snapshot."""
    base = title.strip() or "Рецепт"
    live_titles = {normalize_title(recipe.title) for recipe in recipes}
    suffix = 2
    while normalize_title(f"{base} {suffix}") in live_titles:
        suffix += 1
    return f"{base} {suffix}"


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


def _clean_recipe_step(line: str) -> str:
    match = RECIPE_STEP_PREFIX_RE.match(line)
    return match.group("step").strip() if match else line.strip()


def _parse_recipe_list_payload(text: str) -> tuple[Decimal | None, list[RecipeListItem], list[str], list[str]]:
    portions: Decimal | None = None
    ingredient_lines: list[str] = []
    step_lines: list[str] = []
    in_steps = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        portions_match = RECIPE_LIST_PORTIONS_RE.match(line)
        if portions_match is not None and not in_steps:
            try:
                parsed_portions = Decimal(portions_match.group("portions").replace(",", "."))
            except InvalidOperation:
                ingredient_lines.append(line)
                continue
            if parsed_portions > 0:
                portions = parsed_portions
                continue
            ingredient_lines.append(line)
            continue
        header = RECIPE_STEPS_HEADER_RE.match(line)
        if header is not None:
            in_steps = True
            first_step = _clean_recipe_step(header.group(1))
            if first_step:
                step_lines.append(first_step)
            continue
        if in_steps:
            step = _clean_recipe_step(line)
            if step:
                step_lines.append(step)
        else:
            ingredient_lines.append(line)
    items, bad_lines = _parse_recipe_list_lines("\n".join(ingredient_lines))
    return portions, items, bad_lines, step_lines[:MAX_RECIPE_STEPS]


def _parse_recipe_steps(text: str) -> list[str]:
    value = text.strip()
    if value == "-":
        return []
    return [line.strip() for line in text.splitlines() if line.strip()][:MAX_RECIPE_STEPS]


def _format_decimal(value: Decimal | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    quantum = Decimal("1") if digits == 0 else Decimal("0." + ("0" * (digits - 1)) + "1")
    text = str(value.quantize(quantum))
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _scaled_macro(value: Decimal | None, grams: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value * grams / Decimal("100")


def _plain_item_title(item: ResolvedRecipeListItem) -> str:
    title = item.ingredient.title.strip()
    brand = item.brand.strip()
    if brand and brand.casefold() not in title.casefold():
        title = f"{title} ({brand[:60]})"
    return title


def _format_item_title(item: ResolvedRecipeListItem) -> str:
    return html.escape(_plain_item_title(item))


def _format_macros_per_100g(item: ResolvedRecipeListItem) -> str:
    return (
        f"{_format_decimal(item.energy_per_100g, 0)}/"
        f"{_format_decimal(item.protein_per_100g)}/"
        f"{_format_decimal(item.fat_per_100g)}/"
        f"{_format_decimal(item.carbohydrate_per_100g)}"
    )


def _format_resolved_item(item: ResolvedRecipeListItem) -> str:
    return f"- {_format_item_title(item)} | 100г: {_format_macros_per_100g(item)} | масса: {_format_decimal(item.grams)}г"


def _format_unresolved_item(item: RecipeListItem) -> str:
    return f"- ? {html.escape(item.query)} | масса: {_format_decimal(item.grams)}г"


def _sum_known_macros(values: list[Decimal | None]) -> Decimal | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known, Decimal("0"))


def _format_recipe_list_draft(
    title: str,
    items: list[ResolvedRecipeListItem],
    steps: list[str] | None = None,
    unresolved: list[RecipeListItem] | None = None,
    portions: Decimal = Decimal("1"),
) -> str:
    energy = _sum_known_macros([_scaled_macro(item.energy_per_100g, item.grams) for item in items])
    protein = _sum_known_macros([_scaled_macro(item.protein_per_100g, item.grams) for item in items])
    fat = _sum_known_macros([_scaled_macro(item.fat_per_100g, item.grams) for item in items])
    carbs = _sum_known_macros([_scaled_macro(item.carbohydrate_per_100g, item.grams) for item in items])
    steps = steps or []
    unresolved = unresolved or []
    lines = [
        f"<b>Рецепт: {html.escape(title)}</b>",
        f"Порций: {_format_decimal(portions)}",
        f"Итого ккал/Б/Ж/У: {_format_decimal(energy, 0)}/{_format_decimal(protein)}/{_format_decimal(fat)}/{_format_decimal(carbs)}",
        "",
        "<b>Ингредиенты</b>",
    ]
    lines.extend(_format_resolved_item(item) for item in items)
    if not items:
        lines.append("Пока нет подобранных ингредиентов.")
    if unresolved:
        lines.extend(
            [
                "",
                "<b>Нужно заполнить или удалить</b>",
                *(_format_unresolved_item(item) for item in unresolved),
                "",
                "Создать рецепт можно после заполнения или удаления этих позиций.",
            ]
        )
    if steps:
        lines.extend(["", "<b>Шаги</b>", *_format_steps_lines(steps)])
    return "\n".join(lines)


def _recipe_list_draft_keyboard(
    items: list[ResolvedRecipeListItem],
    steps: list[str] | None = None,
    unresolved: list[RecipeListItem] | None = None,
) -> InlineKeyboardMarkup:
    unresolved = unresolved or []
    buttons = [
        [
            InlineKeyboardButton(
                f"Заменить: {item.ingredient.title[:42]}",
                callback_data=f"recipe_list_replace:{index}",
            )
        ]
        for index, item in enumerate(items)
    ]
    for index, item in enumerate(unresolved):
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Найти: {item.query[:24]}",
                    callback_data=f"recipe_list_resolve:{index}",
                ),
                InlineKeyboardButton("Создать", callback_data=f"recipe_list_create_food:{index}"),
                InlineKeyboardButton("Удалить", callback_data=f"recipe_list_drop:{index}"),
            ]
        )
    buttons.append([InlineKeyboardButton("Изменить имя", callback_data="recipe_list_rename:0")])
    buttons.append(
        [InlineKeyboardButton("Изменить шаги" if steps else "Шаги", callback_data="recipe_list_steps:0")]
    )
    if items and not unresolved:
        buttons.append([InlineKeyboardButton("Создать рецепт", callback_data="recipe_list_confirm:0")])
    buttons.append([InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")])
    return InlineKeyboardMarkup(buttons)


def _recipe_list_input_error_keyboard() -> InlineKeyboardMarkup:
    """Return a visible escape hatch while list input remains invalid."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")]]
    )


def _recipe_list_candidate_keyboard(
    candidates: list[ResolvedRecipeListItem],
    page: int,
    has_next: bool,
) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"{page * RECIPE_LIST_CANDIDATES_PAGE_SIZE + index + 1}. {_plain_item_title(item)[:46]}",
                callback_data=f"recipe_list_pick:{index}",
            )
        ]
        for index, item in enumerate(candidates)
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0 or has_next:
        nav.append(InlineKeyboardButton("Назад", callback_data=f"recipe_list_cpage:{page - 1}" if page > 0 else "noop:0"))
        nav.append(InlineKeyboardButton("Дальше", callback_data=f"recipe_list_cpage:{page + 1}" if has_next else "noop:0"))
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
        "Можно прислать новый текст - это запустит новый поиск.",
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
        self._food_usage_refresh_task: asyncio.Task[None] | None = None
        self._recipe_warning_scans: dict[tuple[object, ...], asyncio.Task[_RecipeWarningScanResult]] = {}

    def build(self) -> Application:
        app = (
            Application.builder()
            .token(self.token)
            .concurrent_updates(False)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("accounts", self.accounts))
        app.add_handler(CommandHandler("recipes", self.recipes))
        app.add_handler(CommandHandler("refresh", self.refresh))
        app.add_handler(CommandHandler("groups", self.groups))
        app.add_handler(CommandHandler("diary", self.diary))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app

    def _refresh_timezone(self) -> dt.tzinfo:
        try:
            return ZoneInfo(self.sync_engine.timezone)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown timezone %s; using system local timezone", self.sync_engine.timezone)
            return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc

    def _next_food_usage_refresh_at(self, now: dt.datetime | None = None) -> dt.datetime:
        timezone = self._refresh_timezone()
        current = now or dt.datetime.now(timezone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone)
        current = current.astimezone(timezone)
        target = current.replace(
            hour=FOOD_USAGE_REFRESH_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        if target <= current:
            target += dt.timedelta(days=1)
        return target

    async def _post_init(self, app: Application) -> None:
        if not self.allowed_user_ids:
            logger.warning(
                "TELEGRAM_ALLOWED_USER_IDS is empty; only users already registered in SQLite are authorized"
            )
        if self._food_usage_refresh_task is not None and not self._food_usage_refresh_task.done():
            return
        self._food_usage_refresh_task = asyncio.create_task(
            self._food_usage_refresh_loop(),
            name="fatsecret-food-usage-refresh",
        )
        logger.info("Scheduled daily FatSecret food usage refresh background task")

    async def _post_shutdown(self, app: Application) -> None:
        warning_scans = list(self._recipe_warning_scans.values())
        for task in warning_scans:
            task.cancel()
        if warning_scans:
            await asyncio.gather(*warning_scans, return_exceptions=True)
        self._recipe_warning_scans.clear()
        if self._food_usage_refresh_task is not None:
            self._food_usage_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._food_usage_refresh_task
            self._food_usage_refresh_task = None

    async def _food_usage_refresh_loop(self) -> None:
        while True:
            try:
                next_run = self._next_food_usage_refresh_at()
                delay = max(0.0, (next_run - dt.datetime.now(next_run.tzinfo)).total_seconds())
                logger.info("Next FatSecret food usage refresh scheduled for %s", next_run.isoformat())
                await asyncio.sleep(delay)
                started_at = time.monotonic()
                refreshed = await self.sync_engine.refresh_food_usage_cache_for_all_groups()
                logger.info(
                    "Finished FatSecret food usage refresh for %d groups, %d foods in %.1fs",
                    len(refreshed),
                    sum(refreshed.values()),
                    time.monotonic() - started_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FatSecret food usage background refresh failed")
                await asyncio.sleep(60)

    def _is_authorized(self, telegram_id: int) -> bool:
        if telegram_id in self.allowed_user_ids:
            return True
        if self.storage.is_registered_user(telegram_id):
            return True
        return False

    def _recipe_cache(self, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> list[Recipe] | None:
        if context.chat_data.get(RECIPE_CACHE_GROUP_KEY) != group_id:
            return None
        recipes = context.chat_data.get(RECIPE_CACHE_KEY)
        return recipes if isinstance(recipes, list) else None

    @staticmethod
    def _recipe_cache_needs_reload(context: ContextTypes.DEFAULT_TYPE, group_id: str) -> bool:
        if context.chat_data.get(RECIPE_CACHE_GROUP_KEY) != group_id:
            return True
        loaded_at = context.chat_data.get(RECIPE_CACHE_LOADED_KEY)
        if not isinstance(loaded_at, (int, float)):
            return True
        return time.time() - float(loaded_at) >= RECIPE_WARNING_CACHE_TTL_SECONDS

    def _set_recipe_cache(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        recipes: list[Recipe],
        *,
        invalidate_warnings: bool = True,
    ) -> None:
        context.chat_data[RECIPE_CACHE_GROUP_KEY] = group_id
        context.chat_data[RECIPE_CACHE_KEY] = recipes
        context.chat_data[RECIPE_CACHE_LOADED_KEY] = time.time()
        if invalidate_warnings:
            context.chat_data.pop(RECIPE_PRODUCT_DIFFERENCE_CACHE_KEY, None)

    def _clear_recipe_cache(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._cancel_recipe_warning_render(context)
        context.chat_data.pop(RECIPE_CACHE_GROUP_KEY, None)
        context.chat_data.pop(RECIPE_CACHE_KEY, None)
        context.chat_data.pop(RECIPE_CACHE_LOADED_KEY, None)
        context.chat_data.pop(RECIPE_PRODUCT_DIFFERENCE_CACHE_KEY, None)

    def _recipe_product_difference_cache(
        self,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> dict[str, _RecipeWarningCacheEntry]:
        cache = context.chat_data.get(RECIPE_PRODUCT_DIFFERENCE_CACHE_KEY)
        if not isinstance(cache, dict):
            cache = {}
            context.chat_data[RECIPE_PRODUCT_DIFFERENCE_CACHE_KEY] = cache
        return cache

    def _recipe_warning_state(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        recipes: list[Recipe],
        *,
        refresh_expired: bool = True,
    ) -> tuple[set[str], list[Recipe], set[str]]:
        connected_account_keys = {
            account.key for account in self.storage.list_fatsecret_accounts(group_id)
        }
        cache = self._recipe_product_difference_cache(context)
        now = time.monotonic()
        warning_ids: set[str] = set()
        pending: list[Recipe] = []
        for recipe in recipes:
            signature = _recipe_remote_signature(recipe, connected_account_keys)
            has_duplicate = any(len(remote_ids) > 1 for _, remote_ids in signature)
            if has_duplicate:
                cache[recipe.id] = _RecipeWarningCacheEntry(signature, True, now)
                warning_ids.add(recipe.id)
                continue
            version_count = sum(len(remote_ids) for _, remote_ids in signature)
            if version_count <= 1:
                cache[recipe.id] = _RecipeWarningCacheEntry(signature, False, now)
                continue
            entry = cache.get(recipe.id)
            if isinstance(entry, _RecipeWarningCacheEntry) and entry.signature == signature:
                if entry.has_differences:
                    warning_ids.add(recipe.id)
                if not refresh_expired or now - entry.checked_at <= RECIPE_WARNING_CACHE_TTL_SECONDS:
                    continue
            if refresh_expired:
                pending.append(recipe)
        return warning_ids, pending, connected_account_keys

    async def _run_recipe_warning_scan(
        self,
        group_id: str,
        recipes: list[Recipe],
        connected_account_keys: set[str],
    ) -> _RecipeWarningScanResult:
        started_at = time.monotonic()
        try:
            variants_by_recipe = await self.sync_engine.hydrate_live_recipe_variants_batch(recipes)
            differences = {
                recipe.id: _recipe_versions_differ(
                    variants_by_recipe.get(recipe.id, []),
                    connected_account_keys,
                )
                for recipe in recipes
            }
        except Exception:  # noqa: BLE001 - list rendering already succeeded and must remain usable.
            logger.exception(
                "Background recipe version scan failed group_id=%s recipes=%d",
                group_id,
                len(recipes),
            )
            return _RecipeWarningScanResult({}, failed=True)
        logger.info(
            "Background recipe version scan completed group_id=%s recipes=%d differences=%d duration=%.3fs",
            group_id,
            len(recipes),
            sum(differences.values()),
            time.monotonic() - started_at,
        )
        return _RecipeWarningScanResult(differences)

    def _store_recipe_warning_scan_result(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        recipes: list[Recipe],
        connected_account_keys: set[str],
        result: _RecipeWarningScanResult,
    ) -> None:
        if result.failed or context.chat_data.get(RECIPE_CACHE_GROUP_KEY) != group_id:
            return
        current_recipes = {
            recipe.id: recipe for recipe in self._recipe_cache(context, group_id) or []
        }
        cache = self._recipe_product_difference_cache(context)
        checked_at = time.monotonic()
        for recipe in recipes:
            current = current_recipes.get(recipe.id)
            signature = _recipe_remote_signature(recipe, connected_account_keys)
            if current is None or _recipe_remote_signature(current, connected_account_keys) != signature:
                continue
            cache[recipe.id] = _RecipeWarningCacheEntry(
                signature,
                bool(result.differences.get(recipe.id)),
                checked_at,
            )

    def _shared_recipe_warning_scan(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        recipes: list[Recipe],
        connected_account_keys: set[str],
    ) -> asyncio.Task[_RecipeWarningScanResult]:
        scans = getattr(self, "_recipe_warning_scans", None)
        if not isinstance(scans, dict):
            scans = {}
            self._recipe_warning_scans = scans
        scan_key: tuple[object, ...] = (
            group_id,
            tuple(
                (recipe.id, _recipe_remote_signature(recipe, connected_account_keys))
                for recipe in recipes
            ),
        )
        task = scans.get(scan_key)
        if task is not None and not task.done():
            return task
        task = context.application.create_task(
            self._run_recipe_warning_scan(group_id, recipes, connected_account_keys),
            name=f"recipe-version-scan-{group_id}",
        )
        scans[scan_key] = task

        def forget(completed: asyncio.Task[_RecipeWarningScanResult]) -> None:
            if scans.get(scan_key) is completed:
                scans.pop(scan_key, None)

        task.add_done_callback(forget)
        return task

    def _schedule_recipe_warning_update(
        self,
        target,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        group_id: str,
        recipes: list[Recipe],
        pending: list[Recipe],
        connected_account_keys: set[str],
        page: int,
        page_action: str,
        total_count: int,
        title: str,
        account_labels: dict[str, str],
    ) -> None:
        application = getattr(context, "application", None)
        if not pending or application is None:
            return
        previous = context.chat_data.get(RECIPE_WARNING_RENDER_TASK_KEY)
        if isinstance(previous, asyncio.Task) and not previous.done():
            previous.cancel()
        render_token = time.monotonic_ns()
        context.chat_data[RECIPE_WARNING_RENDER_TOKEN_KEY] = render_token
        shared_scan = self._shared_recipe_warning_scan(
            context,
            group_id,
            pending,
            connected_account_keys,
        )

        def cache_completed_scan(completed: asyncio.Task[_RecipeWarningScanResult]) -> None:
            try:
                result = completed.result()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - the render path reports scan failures separately.
                return
            self._store_recipe_warning_scan_result(
                context,
                group_id,
                pending,
                connected_account_keys,
                result,
            )

        shared_scan.add_done_callback(cache_completed_scan)
        task = application.create_task(
            self._apply_recipe_warning_update(
                target,
                context,
                shared_scan,
                render_token=render_token,
                group_id=group_id,
                recipes=recipes,
                pending=pending,
                connected_account_keys=connected_account_keys,
                page=page,
                page_action=page_action,
                total_count=total_count,
                title=title,
                account_labels=account_labels,
            ),
            name=f"recipe-version-render-{group_id}",
        )
        context.chat_data[RECIPE_WARNING_RENDER_TASK_KEY] = task

    @staticmethod
    def _cancel_recipe_warning_render(context: ContextTypes.DEFAULT_TYPE) -> None:
        task = context.chat_data.pop(RECIPE_WARNING_RENDER_TASK_KEY, None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        context.chat_data[RECIPE_WARNING_RENDER_TOKEN_KEY] = time.monotonic_ns()

    async def _apply_recipe_warning_update(
        self,
        target,
        context: ContextTypes.DEFAULT_TYPE,
        shared_scan: asyncio.Task[_RecipeWarningScanResult],
        *,
        render_token: int,
        group_id: str,
        recipes: list[Recipe],
        pending: list[Recipe],
        connected_account_keys: set[str],
        page: int,
        page_action: str,
        total_count: int,
        title: str,
        account_labels: dict[str, str],
    ) -> None:
        try:
            result = await asyncio.shield(shared_scan)
        except asyncio.CancelledError:
            raise
        if context.chat_data.get(RECIPE_WARNING_RENDER_TOKEN_KEY) != render_token:
            return
        if result.failed:
            context.chat_data[RECIPE_CACHE_LOADED_KEY] = (
                time.time() - RECIPE_WARNING_CACHE_TTL_SECONDS
            )
        self._store_recipe_warning_scan_result(
            context,
            group_id,
            pending,
            connected_account_keys,
            result,
        )
        needs_reload = self._recipe_cache_needs_reload(context, group_id)
        warning_ids, _, _ = self._recipe_warning_state(
            context,
            group_id,
            recipes,
            refresh_expired=not needs_reload,
        )
        text = _recipe_list_message(
            title,
            has_product_differences=bool(warning_ids),
            needs_reload=needs_reload,
        )
        reply_markup = self._recipe_list_keyboard(
            recipes,
            page,
            page_action,
            account_labels,
            total_count=total_count,
            product_difference_ids=warning_ids,
            needs_reload=needs_reload,
        )
        edit = getattr(target, "edit_message_text", None) or getattr(target, "edit_text")
        try:
            await edit(text, reply_markup=reply_markup)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                raise

    async def _refresh_recipe_cache_after_sync(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
    ) -> bool:
        """Replace the recipe cache with a fresh authoritative cookbook after synchronization."""
        try:
            recipes = await self.sync_engine.load_remote_recipe_index(group_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh recipe cache after synchronization group_id=%s", group_id)
            return False
        self._set_recipe_cache(context, group_id, recipes)
        return True

    async def _refresh_group_after_account_transfer(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str | None,
    ) -> bool:
        """Refresh the destination cookbook after owned credentials move between groups."""
        if group_id is None or self.storage.fatsecret_account_count(group_id) == 0:
            return True
        try:
            recipes = await self.sync_engine.load_remote_recipe_index(group_id)
        except Exception:  # noqa: BLE001 - the account move is already committed and remains valid.
            logger.exception("Failed to refresh destination group after account transfer group_id=%s", group_id)
            return False
        self._set_recipe_cache(context, group_id, recipes)
        return True

    def _cached_recipe(self, context: ContextTypes.DEFAULT_TYPE, group_id: str, recipe_id: str) -> Recipe | None:
        recipes = self._recipe_cache(context, group_id) or []
        return next((recipe for recipe in recipes if recipe.id == recipe_id), None)

    def _duplicate_recipe_for_title(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        title: str,
    ) -> Recipe | None:
        local = self.storage.find_recipe_by_title(group_id, title)
        if local is not None:
            return local
        normalized = normalize_title(title)
        if not normalized:
            return None
        return next(
            (recipe for recipe in self._recipe_cache(context, group_id) or [] if normalize_title(recipe.title) == normalized),
            None,
        )

    def _replace_cached_recipe(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        recipe: Recipe,
    ) -> None:
        self._recipe_product_difference_cache(context).pop(recipe.id, None)
        recipes = list(self._recipe_cache(context, group_id) or [])
        for index, item in enumerate(recipes):
            if item.id == recipe.id:
                recipes[index] = recipe
                self._set_recipe_cache(context, group_id, recipes, invalidate_warnings=False)
                return
        recipes.append(recipe)
        recipes.sort(key=lambda item: normalize_title(item.title))
        self._set_recipe_cache(context, group_id, recipes, invalidate_warnings=False)

    def _remove_cached_recipe(self, context: ContextTypes.DEFAULT_TYPE, group_id: str, recipe_id: str) -> None:
        self._recipe_product_difference_cache(context).pop(recipe_id, None)
        recipes = [recipe for recipe in self._recipe_cache(context, group_id) or [] if recipe.id != recipe_id]
        self._set_recipe_cache(context, group_id, recipes, invalidate_warnings=False)

    def _cached_or_stored_recipe(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        recipe_id: str,
    ) -> Recipe | None:
        return self._cached_recipe(context, group_id, recipe_id) or self.storage.get_recipe(recipe_id)

    def _cached_recipe_list(self, context: ContextTypes.DEFAULT_TYPE, group_id: str) -> list[Recipe] | None:
        recipes = self._recipe_cache(context, group_id)
        return list(recipes) if recipes is not None else None

    def _recipes_by_ids(self, context: ContextTypes.DEFAULT_TYPE, group_id: str, recipe_ids: list[str]) -> list[Recipe] | None:
        cached = self._recipe_cache(context, group_id)
        if cached is None:
            return None
        by_id = {recipe.id: recipe for recipe in cached}
        return [by_id[recipe_id] for recipe_id in recipe_ids if recipe_id in by_id]

    def _render_key(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        view: str,
        page: int,
        extra: str = "",
    ) -> str:
        message_id = query.message.message_id if query.message is not None else 0
        cache_loaded = context.chat_data.get(RECIPE_CACHE_LOADED_KEY, 0)
        return f"{message_id}:{view}:{page}:{cache_loaded}:{extra}"

    def _is_duplicate_render(self, context: ContextTypes.DEFAULT_TYPE, key: str) -> bool:
        return context.chat_data.get(RECIPE_RENDER_KEY) == key

    def _mark_rendered(self, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
        context.chat_data[RECIPE_RENDER_KEY] = key

    async def _safe_edit_message_text(self, query, text: str, **kwargs) -> None:
        try:
            await query.edit_message_text(text, **kwargs)
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return
            raise

    async def _require_user(self, update: Update) -> bool:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return False
        if not self._is_authorized(user.id):
            logger.warning(
                "Telegram access denied telegram_id=%s username=%r full_name=%r",
                user.id,
                user.username or "",
                user.full_name or "",
            )
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

    async def _ensure_main_keyboard(self, message, context: ContextTypes.DEFAULT_TYPE) -> None:
        if message is None:
            return
        context.chat_data["reply_keyboard"] = "main"

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
            "Готов. Здесь можно синхронизировать рецепты и через «Меню / Дневник» скопировать заполненный день в выбранные аккаунты и диапазон до 7 дней.",
            reply_markup=MAIN_KEYBOARD,
        )
        context.chat_data["reply_keyboard"] = "main"

    def _groups_text(self, telegram_id: int) -> str:
        active = self.storage.active_group_for_user(telegram_id)
        if active is None:
            return "Ты пока не в группе. Создай группу или подключись по коду."
        lines = [
            "<b>Активная группа</b>",
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
        groups = self.storage.list_groups_for_user(telegram_id)
        if len(groups) > 1:
            lines.extend(["", "<b>Доступные группы</b>"])
            lines.extend(
                f"- {'✓ ' if group.id == active.id else ''}{html.escape(group.name)}"
                for group in groups
            )
        return "\n".join(lines)

    def _groups_keyboard(self, telegram_id: int) -> InlineKeyboardMarkup:
        active = self.storage.active_group_for_user(telegram_id)
        if active is not None:
            buttons: list[list[InlineKeyboardButton]] = []
            for group in self.storage.list_groups_for_user(telegram_id):
                if group.id != active.id:
                    buttons.append(
                        [InlineKeyboardButton(f"Переключиться: {group.name}"[:60], callback_data=f"group_switch:{group.id}")]
                    )
            if self.storage.active_group_created_by(telegram_id):
                buttons.append([InlineKeyboardButton("Переименовать группу", callback_data="group_rename:0")])
            buttons.append(
                [
                    InlineKeyboardButton("Создать группу", callback_data="group_create:0"),
                    InlineKeyboardButton("Подключиться", callback_data="group_join:0"),
                ]
            )
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

    def _accounts_text(self, telegram_id: int, group: RecipeGroup) -> str:
        accounts = self.storage.list_fatsecret_accounts(group.id)
        lines = [f"<b>{html.escape(group.name)}</b>\nПодключено FatSecret аккаунтов: {len(accounts)}"]
        for account in accounts:
            owner = " (мой)" if self.storage.fatsecret_account_owner(account.key) == telegram_id else ""
            lines.append(f"- {html.escape(account.label)}: {html.escape(account.username)}{owner}")
        if not accounts:
            lines.append("FatSecret аккаунты в этой группе еще не подключены.")
        attached = {account.key for account in accounts}
        detached = [
            account
            for account in self.storage.list_fatsecret_accounts_for_owner(telegram_id)
            if account.key not in attached and self.storage.fatsecret_account_group_id(account.key) is None
        ]
        if detached:
            lines.extend(["", "<b>Мои аккаунты без группы</b>"])
            lines.extend(f"- {html.escape(account.label)}: {html.escape(account.username)}" for account in detached)
        return "\n".join(lines)

    def _accounts_keyboard(self, telegram_id: int, group: RecipeGroup) -> InlineKeyboardMarkup:
        accounts = self.storage.list_fatsecret_accounts(group.id)
        owned = {account.key: account for account in self.storage.list_fatsecret_accounts_for_owner(telegram_id)}
        buttons: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("Добавить FatSecret аккаунт", callback_data="account_add:0")]
        ]
        for account in accounts:
            if account.key in owned:
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
                            f"Отсоединить: {account.label[:38]}",
                            callback_data=f"account_detach:{account.key}",
                        )
                    ]
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"Удалить подключение: {account.label[:32]}",
                            callback_data=f"account_delete:{account.key}",
                        )
                    ]
                )
            elif self.storage.active_group_created_by(telegram_id):
                buttons.append(
                    [InlineKeyboardButton(f"Отсоединить: {account.label[:38]}", callback_data=f"account_detach:{account.key}")]
                )
        for account in owned.values():
            if self.storage.fatsecret_account_group_id(account.key) is None:
                buttons.append(
                    [InlineKeyboardButton(f"Подключить к группе: {account.label}"[:60], callback_data=f"account_attach:{account.key}")]
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
        if self.storage.fatsecret_account_owner(account_key) != telegram_id:
            return group, None
        group_accounts = {account.key: account for account in self.storage.list_fatsecret_accounts(group.id)}
        return group, group_accounts.get(account_key)

    async def accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        group = await self._require_active_group(update)
        if group is None:
            return
        await update.effective_message.reply_text(
            self._accounts_text(update.effective_user.id, group),
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
            self._accounts_text(telegram_id, group),
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
        await self._send_recipe_list(update, context, page=0)

    async def recipes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        context.user_data.clear()
        if await self._require_active_group(update) is None:
            return
        await self._send_recipe_list(update, context, page=0)

    async def diary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start copying one account's food diary to selected group accounts."""
        if not await self._require_user(update):
            return
        group = await self._require_active_group(update)
        if group is None:
            return
        accounts = self.storage.list_fatsecret_accounts(group.id)
        context.user_data.clear()
        if len(accounts) < 2:
            await update.effective_message.reply_text(
                "Для копирования дневника подключи минимум два FatSecret аккаунта группы.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        buttons = [
            [InlineKeyboardButton(account.label[:50], callback_data=f"diarysrc:{account.key}")]
            for account in accounts
        ]
        buttons.append([InlineKeyboardButton("Отмена", callback_data="diarycancel:0")])
        await update.effective_message.reply_text(
            "<b>Копирование меню / дневника</b>\n\nВыбери FatSecret аккаунт, где уже заполнен исходный день.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    async def _select_diary_source(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_id: int,
        source_account_key: str,
    ) -> None:
        group = self.storage.active_group_for_user(telegram_id)
        accounts = self.storage.list_fatsecret_accounts(group.id) if group is not None else []
        account = next((item for item in accounts if item.key == source_account_key), None)
        if group is None or account is None or len(accounts) < 2:
            context.user_data.clear()
            await query.edit_message_text("Аккаунты или активная группа изменились. Начни копирование заново.")
            return
        context.user_data.clear()
        context.user_data.update(
            {
                "group_id": group.id,
                "diary_source_account_key": account.key,
                "diary_target_account_keys": {item.key for item in accounts},
            }
        )
        await self._show_diary_targets(query, context)

    async def _show_diary_targets(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        group_id = str(context.user_data.get("group_id") or "")
        source_key = str(context.user_data.get("diary_source_account_key") or "")
        selected = context.user_data.get("diary_target_account_keys")
        selected_keys = selected if isinstance(selected, set) else set()
        accounts = self.storage.list_fatsecret_accounts(group_id)
        if not source_key or len(accounts) < 2:
            context.user_data.clear()
            await query.edit_message_text("Аккаунты изменились. Начни копирование заново.")
            return
        buttons = [
            [
                InlineKeyboardButton(
                    f"{'✓' if account.key in selected_keys else '○'} {account.label}"[:60],
                    callback_data=f"diarytarget:{account.key}",
                )
            ]
            for account in accounts
        ]
        buttons.extend(
            [
                [InlineKeyboardButton("Дальше", callback_data="diarytargets_done:0")],
                [InlineKeyboardButton("Отмена", callback_data="diarycancel:0")],
            ]
        )
        error = "Нужно выбрать хотя бы один целевой аккаунт.\n\n" if context.user_data.pop("diary_targets_error", False) else ""
        await query.edit_message_text(
            error + "Выбери один или несколько аккаунтов, куда копировать выбранный день. "
            "Источник тоже можно оставить выбранным для других дат.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    async def _finish_diary_targets(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        selected = context.user_data.get("diary_target_account_keys")
        if not isinstance(selected, set) or not selected:
            context.user_data["diary_targets_error"] = True
            await self._show_diary_targets(query, context)
            return
        source_key = str(context.user_data.get("diary_source_account_key") or "")
        group_id = str(context.user_data.get("group_id") or "")
        label = next(
            (account.label for account in self.storage.list_fatsecret_accounts(group_id) if account.key == source_key),
            source_key,
        )
        context.user_data["mode"] = "diary_source_date"
        await query.edit_message_text(
            f"Источник: <b>{html.escape(label)}</b>.\n\n"
            "Пришли дату заполненного дня: <code>ДД.ММ.ГГГГ</code>, <code>сегодня</code> или <code>вчера</code>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="diarycancel:0")]]),
            parse_mode=ParseMode.HTML,
        )

    async def _prepare_diary_preview(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        try:
            target_start, target_end = _parse_diary_range(
                text,
                today=dt.datetime.now(self._refresh_timezone()).date(),
            )
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        group_id = str(context.user_data.get("group_id") or "")
        source_account_key = str(context.user_data.get("diary_source_account_key") or "")
        source_date_text = str(context.user_data.get("diary_source_date") or "")
        if not group_id or not source_account_key or not source_date_text:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст копирования потерян. Нажми «Меню / Дневник» заново.")
            return
        status = await update.effective_message.reply_text("Загружаю исходный дневник и готовлю проверку...")
        try:
            preview = await self.sync_engine.prepare_diary_copy(
                group_id=group_id,
                initiated_by=update.effective_user.id,
                source_account_key=source_account_key,
                source_date=dt.date.fromisoformat(source_date_text),
                target_start=target_start,
                target_end=target_end,
                target_account_keys=sorted(context.user_data.get("diary_target_account_keys") or []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("diary copy preview failed")
            await status.edit_text(f"Не удалось подготовить копирование: {user_safe_error_message(exc)}")
            return
        context.user_data["mode"] = "diary_confirm"
        context.user_data["diary_run_id"] = preview.run_id
        await status.edit_text(
            self._format_diary_preview(preview, group_id),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Скопировать", callback_data=f"diaryrun:{preview.run_id}")],
                    [InlineKeyboardButton("Отмена", callback_data="diarycancel:0")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    def _format_diary_preview(self, preview: DiaryCopyPreview, group_id: str) -> str:
        labels = self._account_labels_for_group(group_id)
        meal_counts: dict[int, int] = {}
        for entry in preview.source_entries:
            meal_counts[entry.meal] = meal_counts.get(entry.meal, 0) + 1
        meal_names = {1: "завтрак", 2: "обед", 3: "ужин", 4: "перекусы"}
        meals = ", ".join(
            f"{meal_names.get(meal, f'приём {meal}')}: {count}"
            for meal, count in sorted(meal_counts.items())
        )
        skipped = (
            "\nИсходный аккаунт в исходную дату пропущен, чтобы не создать самодубликат."
            if preview.skipped_source_day
            else ""
        )
        total_rows = len(preview.source_entries) * preview.target_operations
        return (
            "<b>Проверка копирования дневника</b>\n\n"
            f"Источник: {html.escape(labels.get(preview.source_account_key, preview.source_account_key))}, "
            f"{preview.source_date:%d.%m.%Y}\n"
            f"Целевые даты: {preview.target_start:%d.%m.%Y} — {preview.target_end:%d.%m.%Y}\n"
            f"Записей еды в источнике: {len(preview.source_entries)} ({html.escape(meals)})\n"
            f"Операций аккаунт/дата: {preview.target_operations}; будет добавлено строк: {total_rows}.\n\n"
            "Уже существующие записи сохранятся; новые строки добавятся к ним. "
            "Вода, упражнения, фото и заметки не копируются."
            f"{skipped}"
        )

    async def _execute_diary_copy(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_id: int,
        run_id: str,
    ) -> None:
        run = self.storage.diary_copy_run(run_id)
        active_group = self.storage.active_group_for_user(telegram_id)
        if run is None or active_group is None or run["group_id"] != active_group.id:
            context.user_data.clear()
            await query.edit_message_text("Операция устарела или относится к другой группе.")
            return
        await query.edit_message_text("Копирую дневник. Личные рецепты и продукты при необходимости синхронизируются...")
        try:
            result = await self.sync_engine.execute_diary_copy(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("diary copy execution failed")
            await query.edit_message_text(f"Копирование не выполнено: {user_safe_error_message(exc)}")
            return
        context.user_data.clear()
        await query.edit_message_text(self._format_diary_result(result, active_group.id))

    def _format_diary_result(self, result: DiaryCopyResult, group_id: str) -> str:
        labels = self._account_labels_for_group(group_id)
        inserted = sum(item.inserted for item in result.dates)
        failed = sum(item.failed for item in result.dates)
        heading = "Копирование завершено" if failed == 0 else "Копирование завершено частично"
        lines = [f"{heading}. Добавлено: {inserted}; ошибок: {failed}.", ""]
        for item in result.dates:
            label = labels.get(item.account_key, item.account_key)
            lines.append(
                f"- {label}, {item.date:%d.%m.%Y}: +{item.inserted}, ошибок {item.failed}"
                + (f" — {item.message}" if item.message and item.failed else "")
            )
        return "\n".join(lines)

    def _recipe_page(self, recipes: list[Recipe], page: int) -> tuple[list[Recipe], int, int]:
        total_count = len(recipes)
        total_pages = max(1, (total_count + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(max(0, page), total_pages - 1)
        start = page * RECIPES_PAGE_SIZE
        return recipes[start : start + RECIPES_PAGE_SIZE], page, total_count

    async def _send_recipe_list(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        page: int,
    ) -> None:
        self._cancel_recipe_warning_render(context)
        group = self.storage.active_group_for_user(update.effective_user.id)
        if group is None:
            await update.effective_message.reply_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(update.effective_user.id),
            )
            return
        await self._ensure_main_keyboard(update.effective_message, context)
        status = await update.effective_message.reply_text(f"Загружаю рецепты группы «{group.name}» из FatSecret...")
        try:
            all_recipes = await self.sync_engine.load_remote_recipe_index(group.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("live recipe list load failed")
            await status.edit_text(f"Ошибка загрузки рецептов из FatSecret: {user_safe_error_message(exc)}")
            return
        self._set_recipe_cache(context, group.id, all_recipes)
        recipes, page, total_count = self._recipe_page(all_recipes, page)
        context.user_data["mode"] = "recipe_search"
        context.user_data["recipe_list_page"] = page
        context.user_data["group_id"] = group.id
        if total_count == 0:
            await status.edit_text("Рецептов пока нет. Создай рецепт в FatSecret и снова нажми «Поиск рецептов».")
            return
        needs_reload = self._recipe_cache_needs_reload(context, group.id)
        product_difference_ids, pending, connected_account_keys = self._recipe_warning_state(
            context,
            group.id,
            all_recipes,
            refresh_expired=not needs_reload,
        )
        visible_difference_ids = product_difference_ids & {recipe.id for recipe in recipes}
        account_labels = self._account_labels_for_group(group.id)
        await status.edit_text(
            _recipe_list_message(
                "Общий список рецептов:",
                has_product_differences=bool(visible_difference_ids),
                checking_versions=bool(pending and getattr(context, "application", None)),
                needs_reload=needs_reload,
            ),
            reply_markup=self._recipe_list_keyboard(
                recipes,
                page,
                "list",
                account_labels,
                total_count=total_count,
                product_difference_ids=visible_difference_ids,
                needs_reload=needs_reload,
            ),
        )
        self._schedule_recipe_warning_update(
            status,
            context,
            group_id=group.id,
            recipes=recipes,
            pending=pending,
            connected_account_keys=connected_account_keys,
            page=page,
            page_action="list",
            total_count=total_count,
            title="Общий список рецептов:",
            account_labels=account_labels,
        )

    def _account_labels_for_group(self, group_id: str | None) -> dict[str, str]:
        return {account.key: account.label for account in self.storage.list_fatsecret_accounts(group_id)}

    def _recipe_list_keyboard(
        self,
        recipes: list[Recipe],
        page: int,
        page_action: str,
        account_labels: dict[str, str] | None = None,
        total_count: int | None = None,
        product_difference_ids: set[str] | None = None,
        needs_reload: bool = False,
    ) -> InlineKeyboardMarkup:
        account_labels = account_labels or {}
        product_difference_ids = product_difference_ids or set()
        page = max(0, page)
        total_items = len(recipes) if total_count is None else total_count
        total_pages = max(1, (total_items + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE)
        page = min(page, total_pages - 1)
        current = recipes if total_count is not None else recipes[page * RECIPES_PAGE_SIZE : (page + 1) * RECIPES_PAGE_SIZE]
        buttons = [
            [
                InlineKeyboardButton(
                    _recipe_list_button_text(
                        recipe,
                        account_labels,
                        has_product_differences=recipe.id in product_difference_ids,
                    ),
                    callback_data=f"open:{recipe.id}:{page}:{page_action}",
                )
            ]
            for recipe in current
        ]
        nav: list[InlineKeyboardButton] = []
        if total_pages > 1:
            nav.append(
                InlineKeyboardButton("Назад", callback_data=f"{page_action}:{page - 1}" if page > 0 else "noop:0")
            )
            nav.append(
                InlineKeyboardButton(
                    "Дальше",
                    callback_data=f"{page_action}:{page + 1}" if page + 1 < total_pages else "noop:0",
                )
            )
            buttons.append(nav)
        if needs_reload:
            buttons.append([InlineKeyboardButton("Обновить список", callback_data="refresh:0")])
        buttons.append([InlineKeyboardButton("Удалить несколько", callback_data=f"batchdel:{page}")])
        return InlineKeyboardMarkup(buttons)

    def _filter_recipes(self, query: str, recipes: list[Recipe]) -> list[Recipe]:
        terms = normalize_title(query).split()
        if not terms:
            return []
        matches: list[Recipe] = []
        for recipe in recipes:
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
        self._cancel_recipe_warning_render(context)

        if action == "open":
            context.user_data.pop("mode", None)
            await self._open_recipe(query, context, value)
        elif action == "variant":
            await self._open_recipe_variant(query, context, int(value or "0"))
        elif action == "syncvariant":
            await self._sync_recipe_variant(query, context, int(value or "0"))
        elif action == "syncpreview":
            await self._show_sync_preview(query, context, int(value or "0"))
        elif action == "syncconfirm":
            await self._confirm_sync_preview(query, context)
        elif action == "recipe_export":
            await self._export_recipe(query, context, value)
        elif action == "recipe_rename":
            await self._start_recipe_rename(query, context, value)
        elif action == "recipe_rename_replace":
            await self._execute_recipe_rename(query, context, replace_existing=True)
        elif action == "recipe_rename_copy":
            await self._execute_recipe_rename(query, context, create_copy=True)
        elif action == "recipe_rename_retry":
            await self._execute_recipe_rename(
                query,
                context,
                replace_existing=bool(context.user_data.get("recipe_rename_replace_existing")),
            )
        elif action == "noop":
            return
        elif action == "menu":
            context.user_data.clear()
            await query.edit_message_text("Главное меню. Выбери действие на клавиатуре снизу.")
            await self._ensure_main_keyboard(query.message, context)
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
        elif action == "group_switch":
            context.user_data.clear()
            source_group = self.storage.active_group_for_user(update.effective_user.id)
            moved = (
                self.storage.owned_fatsecret_account_count(update.effective_user.id, source_group.id)
                if source_group is not None and source_group.id != value
                else 0
            )
            switched = self.storage.set_active_group_for_user(update.effective_user.id, value)
            if switched:
                self._clear_recipe_cache(context)
            refreshed = await self._refresh_group_after_account_transfer(context, value) if switched else False
            transfer_note = f"\n\nПеренесено твоих FatSecret аккаунтов: {moved}." if switched and moved else ""
            refresh_note = "\nНе удалось сразу обновить рецепты; открой список еще раз." if switched and not refreshed else ""
            await query.edit_message_text(
                (
                    f"{self._groups_text(update.effective_user.id)}{transfer_note}{refresh_note}"
                    if switched
                    else "Эта группа больше недоступна."
                ),
                reply_markup=self._groups_keyboard(update.effective_user.id),
                parse_mode=ParseMode.HTML,
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
            source_group = self.storage.active_group_for_user(update.effective_user.id)
            moved = (
                self.storage.owned_fatsecret_account_count(update.effective_user.id, source_group.id)
                if source_group is not None
                else 0
            )
            left = self.storage.leave_active_group(update.effective_user.id)
            destination = self.storage.active_group_for_user(update.effective_user.id)
            if left:
                self._clear_recipe_cache(context)
            refreshed = await self._refresh_group_after_account_transfer(
                context,
                destination.id if destination is not None else None,
            ) if left else False
            if left and moved:
                account_note = (
                    f" Твои FatSecret аккаунты ({moved}) перенесены в группу «{html.escape(destination.name)}»."
                    if destination is not None
                    else f" Твои FatSecret аккаунты ({moved}) сохранены без группы."
                )
            else:
                account_note = ""
            refresh_note = " Не удалось сразу обновить рецепты; открой список еще раз." if left and not refreshed else ""
            await query.edit_message_text(
                f"Отключился от группы.{account_note}{refresh_note}" if left else "Ты сейчас не в группе.",
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
        elif action == "account_attach":
            context.user_data.clear()
            group = self.storage.active_group_for_user(update.effective_user.id)
            attached = bool(
                group
                and self.storage.attach_fatsecret_account_to_group(
                    value,
                    group.id,
                    update.effective_user.id,
                )
            )
            if attached:
                self._clear_recipe_cache(context)
            await query.edit_message_text(
                "Аккаунт подключен к активной группе." if attached else "Не удалось подключить: аккаунт уже в другой группе или не принадлежит тебе.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
        elif action == "account_detach":
            context.user_data.clear()
            group = self.storage.active_group_for_user(update.effective_user.id)
            detached = bool(
                group
                and self.storage.detach_fatsecret_account_from_group(
                    value,
                    group.id,
                    update.effective_user.id,
                )
            )
            if detached:
                self._clear_recipe_cache(context)
            await query.edit_message_text(
                "Аккаунт отсоединен от группы. Credentials сохранены у владельца."
                if detached
                else "Не удалось отсоединить аккаунт.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
        elif action in {"account_delete", "account_logout", "account_remove"}:
            context.user_data.clear()
            account = self.storage.get_fatsecret_account(value)
            if account is None or self.storage.fatsecret_account_owner(value) != update.effective_user.id:
                await query.edit_message_text("Удалить подключение может только владелец аккаунта.")
                return
            await query.edit_message_text(
                f"Полностью удалить подключение «{html.escape(account.label)}» из бота?\n"
                "Сам FatSecret аккаунт и его рецепты не удалятся.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Да, удалить", callback_data=f"account_delete_confirm:{account.key}")],
                        [InlineKeyboardButton("Назад к аккаунтам", callback_data="accounts:0")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
        elif action in {"account_delete_confirm", "account_logout_confirm", "account_remove_confirm"}:
            context.user_data.clear()
            removed = self.storage.delete_fatsecret_account(
                value,
                owner_telegram_id=update.effective_user.id,
            )
            if removed:
                self._clear_recipe_cache(context)
            await query.edit_message_text(
                "Подключение FatSecret аккаунта удалено из бота."
                if removed
                else "Аккаунт уже удален или принадлежит другому пользователю.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
        elif action == "diarysrc":
            await self._select_diary_source(query, context, update.effective_user.id, value)
        elif action == "diarytarget":
            selected = context.user_data.get("diary_target_account_keys")
            if not isinstance(selected, set):
                await query.edit_message_text("Выбор целей устарел. Начни копирование заново.")
                return
            if value in selected:
                selected.remove(value)
            else:
                selected.add(value)
            await self._show_diary_targets(query, context)
        elif action == "diarytargets_done":
            await self._finish_diary_targets(query, context)
        elif action == "diaryrun":
            await self._execute_diary_copy(query, context, update.effective_user.id, value)
        elif action == "diarycancel":
            context.user_data.clear()
            await query.edit_message_text("Копирование дневника отменено.")
            await self._ensure_main_keyboard(query.message, context)
        elif action == "list":
            context.user_data.pop("current_recipe_id", None)
            context.user_data.pop("recipe_page_action", None)
            await self._edit_recipe_list(query, int(value or "0"), context)
        elif action == "search":
            context.user_data.clear()
            group = await self._require_active_group_query(query, update.effective_user.id)
            if group is None:
                return
            context.user_data["mode"] = "recipe_search"
            context.user_data["group_id"] = group.id
            await query.edit_message_text("Пришли часть названия или ингредиента для поиска по рецептам.")
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
        elif action == "recipe_list_replace_existing":
            await self._replace_existing_recipe_list_from_draft(query, context, update.effective_user.id)
        elif action == "recipe_list_copy":
            await self._copy_existing_recipe_list_from_draft(query, context, update.effective_user.id)
        elif action == "recipe_list_replace":
            await self._start_recipe_list_replace(query, context, int(value or "0"))
        elif action == "recipe_list_resolve":
            await self._start_recipe_list_resolve(query, context, int(value or "0"))
        elif action == "recipe_list_create_food":
            await self._start_recipe_list_food_create(query, context, int(value or "0"))
        elif action == "recipe_list_drop":
            await self._drop_recipe_list_unresolved(query, context, int(value or "0"))
        elif action == "recipe_list_pick":
            await self._pick_recipe_list_candidate(query, context, int(value or "0"))
        elif action == "recipe_list_cpage":
            await self._show_recipe_list_replacements(query, context, int(value or "0"))
        elif action == "recipe_list_rename":
            await self._start_recipe_list_rename(query, context)
        elif action == "recipe_list_steps":
            await self._start_recipe_list_steps(query, context)
        elif action == "recipe_list_back":
            await self._edit_recipe_list_draft(query, context)
        elif action == "recipe_list_cancel":
            context.user_data.clear()
            await self._edit_recipe_list(query, 0, context)
        elif action == "food_skip_barcode":
            await self._skip_custom_food_barcode(query, context)
        elif action == "food_skip_brand":
            await self._skip_custom_food_brand(query, context)
        elif action == "food_brand_pick":
            await self._pick_custom_food_brand(query, context, value)
        elif action == "food_brand_custom":
            await self._use_custom_food_brand_text(query, context, value)
        elif action == "food_ignore_known_barcode":
            await self._ignore_known_custom_food_barcode(query, context)
        elif action == "food_create":
            await self._create_custom_food(query, context, update.effective_user.id)
        elif action == "food_change_title":
            context.user_data["mode"] = "custom_food_title"
            await query.edit_message_text("Пришли новое название продукта.")
        elif action == "food_cancel":
            await self._cancel_custom_food(query, context)
        elif action == "refresh":
            context.user_data.clear()
            await self._refresh_from_callback(query, context)
        elif action == "sync":
            context.user_data.pop("mode", None)
            await self._open_sync_menu(query, context, value)
        elif action == "syncfrom":
            _source_key, _, recipe_id = value.partition(":")
            context.user_data.pop("mode", None)
            await self._open_sync_menu(query, context, recipe_id)
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
            await self._edit_recipe_list(query, 0, context)
        elif action == "delete":
            context.user_data.pop("mode", None)
            await self._confirm_delete_recipe(query, context, value)
        elif action == "delete_confirm":
            context.user_data.pop("mode", None)
            await self._delete_recipe(query, context, value)
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
        context.user_data.clear()
        context.user_data["mode"] = "fatsecret_login"
        context.user_data["group_id"] = group.id
        await query.edit_message_text("Пришли логин или email от FatSecret. Сообщение я постараюсь удалить после чтения.")

    async def _edit_recipe_list(self, query, page: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        user = query.from_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return
        all_recipes = self._recipe_cache(context, group.id) if context is not None else None
        if all_recipes is None:
            await query.edit_message_text(
                "Список рецептов устарел. Нажми «Поиск рецептов», чтобы загрузить актуальный список."
            )
            return
        recipes, page, total_count = self._recipe_page(all_recipes, page)
        render_key = self._render_key(query, context, "list", page) if context is not None else ""
        if context is not None and self._is_duplicate_render(context, render_key):
            return
        if context is not None:
            context.user_data["mode"] = "recipe_search"
            context.user_data["recipe_list_page"] = page
            context.user_data["group_id"] = group.id
            await self._ensure_main_keyboard(query.message, context)
        if total_count == 0:
            await query.edit_message_text("Рецептов пока нет.")
            return
        if context is not None:
            needs_reload = self._recipe_cache_needs_reload(context, group.id)
            product_difference_ids, pending, connected_account_keys = self._recipe_warning_state(
                context,
                group.id,
                all_recipes,
                refresh_expired=not needs_reload,
            )
        else:
            product_difference_ids, pending, connected_account_keys = set(), [], set()
            needs_reload = False
        visible_difference_ids = product_difference_ids & {recipe.id for recipe in recipes}
        account_labels = self._account_labels_for_group(group.id)
        await self._safe_edit_message_text(
            query,
            _recipe_list_message(
                "Общий список рецептов:",
                has_product_differences=bool(visible_difference_ids),
                checking_versions=bool(pending and getattr(context, "application", None)),
                needs_reload=needs_reload,
            ),
            reply_markup=self._recipe_list_keyboard(
                recipes,
                page,
                "list",
                account_labels,
                total_count=total_count,
                product_difference_ids=visible_difference_ids,
                needs_reload=needs_reload,
            ),
        )
        if context is not None:
            self._mark_rendered(context, render_key)
            self._schedule_recipe_warning_update(
                query,
                context,
                group_id=group.id,
                recipes=recipes,
                pending=pending,
                connected_account_keys=connected_account_keys,
                page=page,
                page_action="list",
                total_count=total_count,
                title="Общий список рецептов:",
                account_labels=account_labels,
            )

    async def _edit_search_results(self, query, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        search_query = context.user_data.get("recipe_search_query")
        group_id = context.user_data.get("group_id")
        if not search_query:
            await query.edit_message_text(
                "Поиск устарел. Пришли новый текст для поиска по рецептам.",
            )
            return
        if not group_id:
            await query.edit_message_text("Группа поиска устарела. Запусти поиск заново.")
            return
        cached = self._recipe_cache(context, str(group_id))
        if cached is None:
            await query.edit_message_text("Список рецептов устарел. Нажми «Поиск рецептов», чтобы загрузить актуальный список.")
            return
        search_ids = context.user_data.get(RECIPE_SEARCH_IDS_KEY)
        recipes = (
            self._recipes_by_ids(context, str(group_id), search_ids)
            if isinstance(search_ids, list)
            else None
        )
        if recipes is None:
            recipes = self._filter_recipes(search_query, cached)
            context.user_data[RECIPE_SEARCH_IDS_KEY] = [recipe.id for recipe in recipes]
        if not recipes:
            await query.edit_message_text(
                f"По запросу «{html.escape(search_query)}» ничего не найдено. Пришли другой текст.",
                parse_mode=ParseMode.HTML,
            )
            return
        page_recipes, page, total_count = self._recipe_page(recipes, page)
        extra = f"{search_query}:{len(recipes)}:{hash(tuple(recipe.id for recipe in recipes))}"
        render_key = self._render_key(query, context, "searchpage", page, extra)
        if self._is_duplicate_render(context, render_key):
            return
        await self._ensure_main_keyboard(query.message, context)
        context.user_data["mode"] = "recipe_search"
        needs_reload = self._recipe_cache_needs_reload(context, str(group_id))
        product_difference_ids, pending, connected_account_keys = self._recipe_warning_state(
            context,
            str(group_id),
            cached,
            refresh_expired=not needs_reload,
        )
        visible_difference_ids = product_difference_ids & {recipe.id for recipe in page_recipes}
        account_labels = self._account_labels_for_group(group_id)
        title = f"Найдено рецептов: {len(recipes)}"
        await self._safe_edit_message_text(
            query,
            _recipe_list_message(
                title,
                has_product_differences=bool(visible_difference_ids),
                checking_versions=bool(pending and getattr(context, "application", None)),
                needs_reload=needs_reload,
            ),
            reply_markup=self._recipe_list_keyboard(
                page_recipes,
                page,
                "searchpage",
                account_labels,
                total_count=total_count,
                product_difference_ids=visible_difference_ids,
                needs_reload=needs_reload,
            ),
        )
        self._mark_rendered(context, render_key)
        self._schedule_recipe_warning_update(
            query,
            context,
            group_id=str(group_id),
            recipes=page_recipes,
            pending=pending,
            connected_account_keys=connected_account_keys,
            page=page,
            page_action="searchpage",
            total_count=total_count,
            title=title,
            account_labels=account_labels,
        )

    async def _refresh_from_callback(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = query.from_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None:
            await query.edit_message_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return
        await query.edit_message_text(f"Загружаю рецепты группы «{group.name}» из FatSecret...")
        try:
            recipes = await self.sync_engine.load_remote_recipe_index(group.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh failed")
            await query.edit_message_text(f"Ошибка обновления: {user_safe_error_message(exc)}")
            return
        self._set_recipe_cache(context, group.id, recipes)
        await self._edit_recipe_list(query, 0, context)

    def _recipe_detail_page_count(
        self,
        telegram_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        page_action: str,
    ) -> tuple[int, str]:
        group = self.storage.active_group_for_user(telegram_id)
        if group is None:
            return 1, "list"
        if page_action == "searchpage":
            search_query = str(context.user_data.get("recipe_search_query") or "").strip()
            group_id = context.user_data.get("group_id")
            if search_query and group_id == group.id:
                search_ids = context.user_data.get(RECIPE_SEARCH_IDS_KEY)
                recipes = (
                    self._recipes_by_ids(context, group.id, search_ids)
                    if isinstance(search_ids, list)
                    else None
                )
                if recipes is None:
                    recipes = self._filter_recipes(search_query, self._recipe_cache(context, group.id) or [])
                    context.user_data[RECIPE_SEARCH_IDS_KEY] = [recipe.id for recipe in recipes]
                return max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE), "searchpage"
        recipes = self._recipe_cache(context, group.id) or []
        return max(1, (len(recipes) + RECIPES_PAGE_SIZE - 1) // RECIPES_PAGE_SIZE), "list"

    async def _edit_recipe_rename_status(self, target, text: str, **kwargs) -> None:
        """Edit either a callback query message or a status Message."""
        edit = target.edit_message_text if hasattr(target, "edit_message_text") else target.edit_text
        await edit(text, **kwargs)

    def _recipe_rename_back_callback(self, context: ContextTypes.DEFAULT_TYPE, recipe_id: str) -> str:
        page = max(0, int(context.user_data.get("recipe_list_page") or 0))
        page_action = str(context.user_data.get("recipe_page_action") or "list")
        page_action = page_action if page_action in {"list", "searchpage"} else "list"
        return f"open:{recipe_id}:{page}:{page_action}"

    async def _start_recipe_rename(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        recipe_id: str,
    ) -> None:
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe = self._cached_or_stored_recipe(context, group.id, recipe_id) if group is not None else None
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        context.user_data["mode"] = "recipe_rename"
        context.user_data["recipe_rename_group_id"] = recipe.group_id
        context.user_data["recipe_rename_updated_by"] = query.from_user.id
        context.user_data["recipe_rename_ref"] = recipe
        context.user_data.pop("recipe_rename_title", None)
        context.user_data.pop("recipe_rename_replace_existing", None)
        await query.edit_message_text(
            f"Текущее имя: <b>{html.escape(recipe.title)}</b>\nПришли новое имя рецепта.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data=self._recipe_rename_back_callback(context, recipe.id))]]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_recipe_rename(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        title = text.strip()
        reference = context.user_data.get("recipe_rename_ref")
        group = self.storage.active_group_for_user(update.effective_user.id)
        if not title:
            await update.effective_message.reply_text("Название не должно быть пустым.")
            return
        if not isinstance(reference, Recipe) or group is None or reference.group_id != group.id:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст переименования устарел. Открой рецепт заново.")
            return
        if title == reference.title.strip():
            for key in (
                "mode",
                "recipe_rename_group_id",
                "recipe_rename_updated_by",
                "recipe_rename_ref",
                "recipe_rename_title",
                "recipe_rename_conflict_ref",
                "recipe_rename_replace_existing",
            ):
                context.user_data.pop(key, None)
            await update.effective_message.reply_text(
                "Название не изменилось.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Открыть рецепт", callback_data=self._recipe_rename_back_callback(context, reference.id))]]
                ),
            )
            return
        context.user_data["recipe_rename_title"] = title
        status = await update.effective_message.reply_text("Проверяю актуальные названия рецептов в FatSecret...")
        await self._execute_recipe_rename(status, context)

    async def _show_recipe_rename_duplicate(
        self,
        target,
        context: ContextTypes.DEFAULT_TYPE,
        duplicate: Recipe,
        live_recipes: list[Recipe],
    ) -> None:
        title = str(context.user_data.get("recipe_rename_title") or "").strip()
        reference = context.user_data.get("recipe_rename_ref")
        if not title or not isinstance(reference, Recipe):
            await self._edit_recipe_rename_status(target, "Контекст переименования устарел. Открой рецепт заново.")
            return
        copy_title = _next_live_recipe_title(title, live_recipes)
        context.user_data["mode"] = "recipe_rename_conflict"
        context.user_data["recipe_rename_conflict_ref"] = duplicate
        await self._edit_recipe_rename_status(
            target,
            "Рецепт с таким названием уже есть.\n\n"
            "Можно обновить существующий: выбранный рецепт сохраню под новым именем, "
            "а прежний одноимённый рецепт удалю только после успешного переименования.\n\n"
            f"Для отдельной копии использую имя: <b>{html.escape(copy_title)}</b>",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Обновить существующий", callback_data="recipe_rename_replace:0")],
                    [InlineKeyboardButton(f"Создать «{copy_title}»"[:60], callback_data="recipe_rename_copy:0")],
                    [
                        InlineKeyboardButton(
                            "Отмена",
                            callback_data=self._recipe_rename_back_callback(context, reference.id),
                        )
                    ],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _execute_recipe_rename(
        self,
        target,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        replace_existing: bool = False,
        create_copy: bool = False,
    ) -> None:
        requested_title = str(context.user_data.get("recipe_rename_title") or "").strip()
        group_id = str(context.user_data.get("recipe_rename_group_id") or "")
        reference = context.user_data.get("recipe_rename_ref")
        if not requested_title or not group_id or not isinstance(reference, Recipe):
            await self._edit_recipe_rename_status(target, "Контекст переименования устарел. Открой рецепт заново.")
            return

        await self._edit_recipe_rename_status(target, "Проверяю актуальные названия рецептов в FatSecret...")
        try:
            live_recipes = await self.sync_engine.load_remote_recipe_index(group_id)
        except Exception as exc:  # Rename must never use stale collision data.
            logger.exception("live recipe rename check failed")
            await self._edit_recipe_rename_status(
                target,
                f"Не удалось проверить актуальные названия рецептов: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Повторить", callback_data="recipe_rename_retry:0")]]
                ),
            )
            return
        self._set_recipe_cache(context, group_id, live_recipes)
        selected = _fresh_recipe_reference(reference, live_recipes)
        if selected is None:
            await self._edit_recipe_rename_status(
                target,
                "Выбранный рецепт изменился или был удалён. Обнови список и выбери его заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return

        selected_identities = _recipe_remote_identities(reference)
        final_title = _next_live_recipe_title(requested_title, live_recipes) if create_copy else requested_title
        if create_copy:
            context.user_data["recipe_rename_title"] = final_title
        duplicate = _duplicate_recipe_reference(
            live_recipes,
            requested_title,
            exclude=selected_identities,
        )
        if duplicate is not None and not replace_existing and not create_copy:
            await self._show_recipe_rename_duplicate(target, context, duplicate, live_recipes)
            return
        context.user_data["recipe_rename_replace_existing"] = replace_existing

        await self._edit_recipe_rename_status(target, "Переименовываю рецепт во всех FatSecret аккаунтах...")
        try:
            rename_results = await self.sync_engine.rename_live_recipe_everywhere(selected, final_title)
        except Exception as exc:
            logger.exception("recipe rename failed")
            await self._edit_recipe_rename_status(
                target,
                f"Ошибка переименования: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Повторить", callback_data="recipe_rename_retry:0")]]
                ),
            )
            return
        if not rename_results or not all(result.ok for result in rename_results):
            account_labels = self._account_labels_for_group(group_id)
            lines = [
                f"{account_labels.get(result.account_key, result.account_key)}: "
                f"{'OK' if result.ok else 'ERROR'} — {result.message}"
                for result in rename_results
            ]
            await self._edit_recipe_rename_status(
                target,
                "Переименование выполнено не во всех аккаунтах. Другой рецепт не удалялся.\n\n"
                + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Повторить", callback_data="recipe_rename_retry:0")]]
                ),
            )
            return

        delete_results = []
        if replace_existing and duplicate is not None:
            await self._edit_recipe_rename_status(
                target,
                "Новое имя проверено во всех аккаунтах. Удаляю прежний одноимённый рецепт...",
            )
            try:
                delete_results = await self.sync_engine.delete_live_recipe_everywhere(duplicate)
            except Exception as exc:
                logger.exception("duplicate recipe cleanup after rename failed")
                await self._edit_recipe_rename_status(
                    target,
                    f"Рецепт переименован в «{html.escape(final_title)}», но прежний одноимённый рецепт "
                    f"пока не удалён: {html.escape(user_safe_error_message(exc))}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Повторить очистку", callback_data="recipe_rename_retry:0")]]
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return
            if delete_results and not all(result.ok for result in delete_results):
                account_labels = self._account_labels_for_group(group_id)
                lines = [
                    f"{html.escape(account_labels.get(result.account_key, result.account_key))}: "
                    f"{'OK' if result.ok else 'ERROR'} — {html.escape(result.message)}"
                    for result in delete_results
                ]
                await self._edit_recipe_rename_status(
                    target,
                    f"Рецепт переименован в «{html.escape(final_title)}», но старый одноимённый рецепт "
                    "удалён не во всех аккаунтах.\n\n" + "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Повторить очистку", callback_data="recipe_rename_retry:0")]]
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return

        try:
            refreshed = await self.sync_engine.load_remote_recipe_index(group_id)
            self._set_recipe_cache(context, group_id, refreshed)
        except Exception:  # The verified remote rename remains successful.
            logger.exception("recipe cache refresh after rename failed")
            refreshed = []
        selected_ids = _recipe_remote_identities(reference)
        local_warning = ""
        try:
            self.storage.rename_recipe_for_remote_identities(
                group_id,
                selected_ids,
                final_title,
                int(context.user_data.get("recipe_rename_updated_by") or 0) or None,
            )
        except Exception:  # Remote readback is authoritative; surface the repair need.
            logger.exception("local recipe title finalization after remote rename failed")
            local_warning = " Локальный индекс не обновился; обнови список перед следующим действием."
        renamed_card = next(
            (recipe for recipe in refreshed if selected_ids & _recipe_remote_identities(recipe)),
            None,
        )
        renamed_id = renamed_card.id if renamed_card is not None else reference.id
        context.user_data["current_recipe_id"] = renamed_id
        for key in (
            "mode",
            "recipe_rename_group_id",
            "recipe_rename_updated_by",
            "recipe_rename_ref",
            "recipe_rename_title",
            "recipe_rename_conflict_ref",
            "recipe_rename_replace_existing",
        ):
            context.user_data.pop(key, None)
        suffix = " Прежний одноимённый рецепт удалён." if replace_existing and delete_results else ""
        await self._edit_recipe_rename_status(
            target,
            f"Рецепт переименован: <b>{html.escape(final_title)}</b>.{suffix}{local_warning}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Открыть рецепт", callback_data=f"open:{renamed_id}")],
                    [InlineKeyboardButton("К списку", callback_data="list:0")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _open_recipe(self, query, context: ContextTypes.DEFAULT_TYPE, value: str) -> None:
        recipe_id, page, page_action = _parse_open_recipe_value(value)
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe_ref = self._cached_recipe(context, group.id, recipe_id) if group is not None else None
        local_recipe = recipe_ref or self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, local_recipe):
            return
        variants = []
        if recipe_ref is not None:
            variants = await self.sync_engine.hydrate_live_recipe_variants(recipe_ref)
            if not variants:
                await query.edit_message_text(
                    "Рецепт больше не найден в подключённых FatSecret аккаунтах. Обнови список."
                )
                return
            recipe = variants[0].recipe
        else:
            recipe = await self.sync_engine.hydrate_recipe_from_remote(recipe_id)
        if recipe is None:
            await query.edit_message_text("Рецепт не найден.")
            return
        total_pages, page_action = self._recipe_detail_page_count(query.from_user.id, context, page_action)
        context.user_data["current_recipe_id"] = recipe.id
        context.user_data["recipe_list_page"] = page
        context.user_data["recipe_page_action"] = page_action
        await self._ensure_main_keyboard(query.message, context)
        accounts = self.storage.list_fatsecret_accounts(recipe.group_id)
        connected_account_keys = {account.key for account in accounts}
        versions_differ = bool(variants) and _recipe_versions_differ(variants, connected_account_keys)
        context.user_data["recipe_variants"] = variants
        context.user_data["recipe_versions_differ"] = versions_differ
        context.user_data.pop("current_recipe_variant_index", None)
        if recipe_ref is not None and group is not None:
            recipe.remote_ids = dict(recipe_ref.remote_ids)
            recipe.remote_ids_by_account = {
                account_key: list(remote_ids)
                for account_key, remote_ids in recipe_ref.remote_ids_by_account.items()
            }
            self._replace_cached_recipe(context, group.id, recipe)
            self._recipe_product_difference_cache(context)[recipe.id] = _RecipeWarningCacheEntry(
                _recipe_remote_signature(recipe, connected_account_keys),
                versions_differ,
                time.monotonic(),
            )
        if versions_differ:
            await self._show_recipe_variant_picker(query, context, recipe, variants, accounts)
            return
        await query.edit_message_text(
            _format_recipe(recipe),
            reply_markup=_recipe_actions_keyboard(
                recipe.id,
                page,
                page_action,
                total_pages,
                export_variant_index=0 if variants else -1,
            ),
            parse_mode=ParseMode.HTML,
        )

    def _recipe_variant(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> RemoteRecipeVariant | None:
        variants = context.user_data.get("recipe_variants")
        if not isinstance(variants, list) or index < 0 or index >= len(variants):
            return None
        variant = variants[index]
        return variant if isinstance(variant, RemoteRecipeVariant) else None

    @staticmethod
    def _variant_button_label(
        variant: RemoteRecipeVariant,
        variants: list[RemoteRecipeVariant],
        account_labels: dict[str, str],
    ) -> str:
        label = account_labels.get(variant.account_key, variant.account_key)
        if sum(item.account_key == variant.account_key for item in variants) > 1:
            label = f"{label} (ID {variant.remote_recipe_id})"
        return label

    async def _show_recipe_variant_picker(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        recipe: Recipe,
        variants: list[RemoteRecipeVariant],
        accounts: list[FatSecretAccountConfig],
        *,
        heading: str = "Версии рецепта различаются",
    ) -> None:
        labels = {account.key: account.label for account in accounts}
        counts = Counter(variant.account_key for variant in variants)
        missing = [account.label for account in accounts if counts.get(account.key, 0) == 0]
        lines = [f"<b>{html.escape(heading)}</b>", "", f"<b>{html.escape(recipe.title)}</b>"]
        if missing:
            lines.extend(["", "Нет версии: " + ", ".join(html.escape(label) for label in missing) + "."])
        lines.extend(["", "Выбери аккаунт, версию которого нужно открыть."])
        buttons = [
            [
                InlineKeyboardButton(
                    self._variant_button_label(variant, variants, labels)[:60],
                    callback_data=f"variant:{index}",
                )
            ]
            for index, variant in enumerate(variants)
        ]
        page = max(0, int(context.user_data.get("recipe_list_page") or 0))
        page_action = str(context.user_data.get("recipe_page_action") or "list")
        buttons.append([InlineKeyboardButton("К списку", callback_data=f"{page_action}:{page}")])
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    async def _open_recipe_variant(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        variant = self._recipe_variant(context, index)
        if variant is None:
            await query.edit_message_text("Версия устарела. Открой рецепт из списка заново.")
            return
        variants = context.user_data.get("recipe_variants")
        variants = variants if isinstance(variants, list) else [variant]
        accounts = self.storage.list_fatsecret_accounts(variant.recipe.group_id)
        labels = {account.key: account.label for account in accounts}
        label = self._variant_button_label(variant, variants, labels)
        context.user_data["current_recipe_variant_index"] = index
        page = max(0, int(context.user_data.get("recipe_list_page") or 0))
        page_action = str(context.user_data.get("recipe_page_action") or "list")
        await query.edit_message_text(
            f"<b>Версия: {html.escape(label)}</b>\n\n{_format_recipe(variant.recipe)}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Экспортировать",
                            callback_data=f"recipe_export:{variant.recipe.id}:{index}",
                        )
                    ],
                    [InlineKeyboardButton("Синхронизировать", callback_data=f"sync:{variant.recipe.id}")],
                    [InlineKeyboardButton("Переименовать", callback_data=f"recipe_rename:{variant.recipe.id}")],
                    [InlineKeyboardButton("Удалить в FatSecret", callback_data=f"delete:{variant.recipe.id}")],
                    [InlineKeyboardButton("Выбрать другой аккаунт", callback_data=f"open:{variant.recipe.id}")],
                    [InlineKeyboardButton("К списку", callback_data=f"{page_action}:{page}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _export_recipe(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        value: str,
    ) -> None:
        recipe_id, separator, raw_index = value.rpartition(":")
        if not separator:
            recipe_id, raw_index = value, "-1"
        try:
            variant_index = int(raw_index)
        except ValueError:
            variant_index = -1
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe: Recipe | None = None
        if variant_index >= 0:
            variant = self._recipe_variant(context, variant_index)
            if variant is not None and variant.recipe.id == recipe_id:
                recipe = variant.recipe
        if recipe is None and group is not None:
            recipe = self._cached_or_stored_recipe(context, group.id, recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        try:
            payload = _recipe_export_payload(recipe)
        except ValueError as exc:
            await query.message.reply_text(
                f"Не удалось экспортировать рецепт: {html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
            return
        title = recipe.title.strip() or "Рецепт"
        rendered = (
            f"Название: <code>{html.escape(title)}</code>\n\n"
            "После ввода названия вставь этот блок:\n"
            f"<pre>{html.escape(payload)}</pre>"
        )
        if len(rendered) <= TELEGRAM_SAFE_TEXT_LIMIT:
            await query.message.reply_text(rendered, parse_mode=ParseMode.HTML)
            return
        await query.message.reply_document(
            document=InputFile(io.BytesIO(payload.encode("utf-8")), filename="recipe-import.txt"),
            caption=f"Название: <code>{html.escape(title)}</code>\nИмпортный блок находится в файле.",
            parse_mode=ParseMode.HTML,
        )

    async def _sync_recipe_variant(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        variant = self._recipe_variant(context, index)
        if variant is None:
            await query.edit_message_text("Версия устарела. Открой рецепт из списка заново.")
            return
        await self._show_sync_preview(query, context, index)

    async def _open_sync_menu(self, query, context: ContextTypes.DEFAULT_TYPE, recipe_id: str) -> None:
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe_ref = self._cached_recipe(context, group.id, recipe_id) if group is not None else None
        recipe = recipe_ref or self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        if recipe_ref is None:
            await query.edit_message_text("Карточка рецепта устарела. Нажми «Поиск рецептов» и открой её заново.")
            return
        variants = await self.sync_engine.hydrate_live_recipe_variants(recipe_ref)
        accounts = self.storage.list_fatsecret_accounts(recipe.group_id)
        connected_account_keys = {account.key for account in accounts}
        if not variants:
            await query.edit_message_text("Не удалось найти живую версию рецепта в подключённых аккаунтах.")
            return
        versions_differ = _recipe_versions_differ(variants, connected_account_keys)
        context.user_data["recipe_variants"] = variants
        context.user_data["recipe_versions_differ"] = versions_differ
        if not versions_differ:
            context.user_data.pop("recipe_sync_preview", None)
            display = variants[0].recipe
            display.remote_ids = dict(recipe_ref.remote_ids)
            display.remote_ids_by_account = {
                key: list(values) for key, values in recipe_ref.remote_ids_by_account.items()
            }
            self._replace_cached_recipe(context, recipe.group_id, display)
            await query.edit_message_text(
                "Версии уже совпадают — синхронизация не нужна.\n\n" + _format_recipe(display),
                reply_markup=_recipe_actions_keyboard(
                    display.id,
                    max(0, int(context.user_data.get("recipe_list_page") or 0)),
                    str(context.user_data.get("recipe_page_action") or "list"),
                    export_variant_index=0,
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        labels = {account.key: account.label for account in accounts}
        buttons = [
            [
                InlineKeyboardButton(
                    self._variant_button_label(variant, variants, labels)[:60],
                    callback_data=f"syncpreview:{index}",
                )
            ]
            for index, variant in enumerate(variants)
        ]
        buttons.append([InlineKeyboardButton("Назад к рецепту", callback_data=f"open:{recipe_id}")])
        await query.edit_message_text(
            "Из какого FatSecret аккаунта взять оригинал рецепта?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _show_sync_preview(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        variant = self._recipe_variant(context, index)
        if variant is None or not context.user_data.get("recipe_versions_differ"):
            await query.edit_message_text("Версии изменились. Открой рецепт и проверь их заново.")
            return
        variants = context.user_data.get("recipe_variants")
        variants = variants if isinstance(variants, list) else [variant]
        labels = {
            account.key: account.label
            for account in self.storage.list_fatsecret_accounts(variant.recipe.group_id)
        }
        label = self._variant_button_label(variant, variants, labels)
        context.user_data["recipe_sync_preview"] = {
            "recipe_id": variant.recipe.id,
            "group_id": variant.recipe.group_id,
            "account_key": variant.account_key,
            "remote_id": variant.remote_recipe_id,
            "content_digest": variant.fingerprint.digest,
            "variant_index": index,
        }
        await query.edit_message_text(
            f"<b>Оригинал из аккаунта: {html.escape(label)}</b>\n\n"
            f"{_format_recipe(variant.recipe)}\n\n"
            "После подтверждения эта версия заменит отличающиеся версии в остальных подключённых аккаунтах. "
            "Оригинал не изменится.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Подтвердить синхронизацию", callback_data="syncconfirm:0")],
                    [InlineKeyboardButton("Выбрать другой источник", callback_data=f"sync:{variant.recipe.id}")],
                    [InlineKeyboardButton("Назад к версии", callback_data=f"variant:{index}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _confirm_sync_preview(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        preview = context.user_data.get("recipe_sync_preview")
        if not isinstance(preview, dict):
            await query.edit_message_text("Подтверждение устарело. Открой рецепт заново.")
            return
        recipe_id = str(preview.get("recipe_id") or "")
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe_ref = self._cached_recipe(context, group.id, recipe_id) if group is not None else None
        if recipe_ref is None or group is None or recipe_ref.group_id != group.id:
            await query.edit_message_text("Карточка рецепта устарела. Нажми «Поиск рецептов».")
            return
        fresh_variants = await self.sync_engine.hydrate_live_recipe_variants(recipe_ref)
        accounts = self.storage.list_fatsecret_accounts(group.id)
        connected_account_keys = {account.key for account in accounts}
        context.user_data["recipe_variants"] = fresh_variants
        if not _recipe_versions_differ(fresh_variants, connected_account_keys):
            context.user_data["recipe_versions_differ"] = False
            context.user_data.pop("recipe_sync_preview", None)
            display = fresh_variants[0].recipe
            display.remote_ids = dict(recipe_ref.remote_ids)
            display.remote_ids_by_account = {
                key: list(values) for key, values in recipe_ref.remote_ids_by_account.items()
            }
            self._replace_cached_recipe(context, group.id, display)
            await query.edit_message_text(
                "Версии уже совпадают — синхронизация не нужна.\n\n" + _format_recipe(display),
                reply_markup=_recipe_actions_keyboard(
                    display.id,
                    max(0, int(context.user_data.get("recipe_list_page") or 0)),
                    str(context.user_data.get("recipe_page_action") or "list"),
                    export_variant_index=0,
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        selected = next(
            (
                variant
                for variant in fresh_variants
                if variant.account_key == preview.get("account_key")
                and variant.remote_recipe_id == preview.get("remote_id")
            ),
            None,
        )
        if selected is None or selected.fingerprint.digest != preview.get("content_digest"):
            context.user_data["recipe_versions_differ"] = True
            context.user_data.pop("recipe_sync_preview", None)
            await self._show_recipe_variant_picker(
                query,
                context,
                recipe_ref,
                fresh_variants,
                accounts,
                heading="Выбранная версия изменилась — проверь источник снова",
            )
            return
        recipe_ref.remote_ids[selected.account_key] = selected.remote_recipe_id
        remote_ids = recipe_ref.remote_ids_by_account.setdefault(selected.account_key, [])
        if selected.remote_recipe_id in remote_ids:
            remote_ids.remove(selected.remote_recipe_id)
        remote_ids.insert(0, selected.remote_recipe_id)
        await self._sync_recipe_message(
            query,
            context,
            recipe_ref.id,
            selected.account_key,
            expected_source_remote_id=selected.remote_recipe_id,
            expected_source_content_digest=selected.fingerprint.digest,
        )

    async def _sync_recipe_message(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        recipe_id: str,
        source_account_key: str,
        *,
        expected_source_remote_id: str | None = None,
        expected_source_content_digest: str | None = None,
    ) -> None:
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe_ref = self._cached_recipe(context, group.id, recipe_id) if group is not None else None
        recipe = recipe_ref or self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        account_labels = {
            account.key: account.label
            for account in self.storage.list_fatsecret_accounts(recipe.group_id)
        }
        source_label = account_labels.get(source_account_key, source_account_key)
        await query.edit_message_text(f"Синхронизирую рецепт из FatSecret аккаунта «{source_label}»...")
        try:
            if recipe_ref is not None:
                synced_recipe, results = await self.sync_engine.sync_live_recipe_from_source(
                    recipe_ref,
                    source_account_key,
                    expected_source_remote_id=expected_source_remote_id,
                    expected_source_content_digest=expected_source_content_digest,
                )
                if not await self._refresh_recipe_cache_after_sync(context, recipe.group_id):
                    self._replace_cached_recipe(context, recipe.group_id, synced_recipe)
            else:
                results = await self.sync_engine.sync_recipe_from_source(
                    recipe_id,
                    source_account_key,
                    expected_source_remote_id=expected_source_remote_id,
                    expected_source_content_digest=expected_source_content_digest,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync failed")
            await query.edit_message_text(
                f"Ошибка синхронизации: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Проверить версии заново", callback_data=f"open:{recipe_id}")]]
                ),
            )
            return
        context.user_data.pop("recipe_sync_preview", None)
        context.user_data["recipe_versions_differ"] = False
        self._recipe_product_difference_cache(context).pop(recipe_id, None)
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

    async def _confirm_delete_recipe(self, query, context: ContextTypes.DEFAULT_TYPE, recipe_id: str) -> None:
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe = self._cached_or_stored_recipe(context, group.id, recipe_id) if group is not None else None
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

    async def _delete_recipe(self, query, context: ContextTypes.DEFAULT_TYPE, recipe_id: str) -> None:
        group = self.storage.active_group_for_user(query.from_user.id)
        recipe_ref = self._cached_recipe(context, group.id, recipe_id) if group is not None else None
        recipe = recipe_ref or self.storage.get_recipe(recipe_id)
        if not await self._require_recipe_in_active_group(query, recipe):
            return
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts(recipe.group_id)}
        await query.edit_message_text("Удаляю рецепт в FatSecret...")
        try:
            results = (
                await self.sync_engine.delete_live_recipe_everywhere(recipe_ref)
                if recipe_ref is not None
                else await self.sync_engine.delete_recipe_everywhere(recipe_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("delete failed")
            await query.edit_message_text(f"Ошибка удаления: {user_safe_error_message(exc)}")
            return
        if results and all(result.ok for result in results):
            self._remove_cached_recipe(context, recipe.group_id, recipe_id)
            if context.user_data.get("current_recipe_id") == recipe_id:
                context.user_data.pop("current_recipe_id", None)
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

    async def _sync_current_recipe_from_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        recipe_id = str(context.user_data.get("current_recipe_id") or "")
        group = self.storage.active_group_for_user(update.effective_user.id)
        recipe = self._cached_or_stored_recipe(context, group.id, recipe_id) if group is not None and recipe_id else None
        if recipe is None or group is None or recipe.group_id != group.id:
            await update.effective_message.reply_text("Открой рецепт из списка и нажми «Синхронизировать».")
            return
        await update.effective_message.reply_text(
            "Проверь актуальные версии перед синхронизацией.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Проверить версии", callback_data=f"sync:{recipe_id}")]]
            ),
        )

    async def _confirm_current_recipe_delete_from_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        recipe_id = str(context.user_data.get("current_recipe_id") or "")
        group = self.storage.active_group_for_user(update.effective_user.id)
        recipe = self._cached_or_stored_recipe(context, group.id, recipe_id) if group is not None and recipe_id else None
        if recipe is None or group is None or recipe.group_id != group.id:
            await update.effective_message.reply_text("Открой рецепт из списка и нажми «Удалить».")
            return
        await update.effective_message.reply_text(
            f"Удалить «{html.escape(recipe.title)}» из FatSecret на всех привязанных аккаунтах?\n\n"
            "После успешного удаления бот уберет рецепт из своего списка.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Удалить в FatSecret", callback_data=f"delete_confirm:{recipe_id}")],
                    [InlineKeyboardButton("Отмена", callback_data=f"open:{recipe_id}")],
                ]
            ),
            parse_mode=ParseMode.HTML,
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
        recipes = self._cached_recipe_list(context, group.id)
        if recipes is None:
            await query.edit_message_text("Список рецептов устарел. Нажми «Поиск рецептов», чтобы загрузить актуальный список.")
            return
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

    async def _send_batch_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        user = update.effective_user
        group = self.storage.active_group_for_user(user.id) if user else None
        if group is None:
            await update.effective_message.reply_text(
                "Сначала создай группу или подключись к группе.",
                reply_markup=self._groups_keyboard(user.id) if user else None,
            )
            return
        recipes = self._cached_recipe_list(context, group.id)
        if recipes is None:
            await update.effective_message.reply_text(
                "Список рецептов еще не загружен. Нажми «Поиск рецептов».",
                reply_markup=MAIN_KEYBOARD,
            )
            context.chat_data["reply_keyboard"] = "main"
            return
        if not recipes:
            await update.effective_message.reply_text("Рецептов пока нет.", reply_markup=MAIN_KEYBOARD)
            context.chat_data["reply_keyboard"] = "main"
            return
        context.user_data.clear()
        context.user_data["mode"] = "batch_delete"
        context.user_data["group_id"] = group.id
        selected = self._batch_delete_ids(context)
        await update.effective_message.reply_text(
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
        if total_pages > 1:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"batchdel:{page - 1}" if page > 0 else "noop:0"))
            nav.append(
                InlineKeyboardButton("Дальше", callback_data=f"batchdel:{page + 1}" if page + 1 < total_pages else "noop:0")
            )
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
        recipes = self._cached_recipe_list(context, str(group_id))
        if recipes is None:
            await query.edit_message_text("Список рецептов устарел. Нажми «Поиск рецептов», чтобы загрузить актуальный список.")
            return
        selected_recipes = [recipe for recipe in recipes if recipe.id in selected]
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
        recipes = self._cached_recipe_list(context, str(group_id))
        if recipes is None:
            await query.edit_message_text("Список рецептов устарел. Нажми «Поиск рецептов», чтобы загрузить актуальный список.")
            return
        selected_recipes = [recipe for recipe in recipes if recipe.id in selected_set]
        selected = [recipe.id for recipe in selected_recipes]
        if not selected_recipes:
            await query.edit_message_text("Ничего не выбрано.")
            return
        title_by_id = {recipe.id: recipe.title for recipe in recipes}
        await query.edit_message_text(f"Удаляю рецепты в FatSecret: {len(selected)}...")
        try:
            results_by_recipe = await self.sync_engine.delete_live_recipes_everywhere(selected_recipes)
        except Exception as exc:  # noqa: BLE001
            logger.exception("batch delete failed")
            await query.edit_message_text(f"Ошибка batch удаления: {user_safe_error_message(exc)}")
            return
        account_labels = {account.key: account.label for account in self.storage.list_fatsecret_accounts(group_id)}
        ok_count = 0
        error_count = 0
        lines: list[str] = []
        deleted_ids: set[str] = set()
        for recipe_id in selected:
            results = results_by_recipe.get(recipe_id, [])
            ok = bool(results) and all(result.ok for result in results)
            ok_count += int(ok)
            error_count += int(not ok)
            if ok:
                deleted_ids.add(recipe_id)
            deleted_accounts = [
                account_labels.get(result.account_key, result.account_key)
                for result in results
                if result.ok
            ]
            errors = [
                f"{account_labels.get(result.account_key, result.account_key)}: {result.message}"
                for result in results
                if not result.ok
            ]
            parts: list[str] = []
            if deleted_accounts:
                parts.append("удален у " + ", ".join(deleted_accounts))
            if errors:
                parts.append("ошибка у " + "; ".join(errors))
            lines.append(f"- {title_by_id.get(recipe_id, recipe_id)}: {'; '.join(parts) if parts else 'нет ответа FatSecret'}")
        self._set_recipe_cache(context, str(group_id), [recipe for recipe in recipes if recipe.id not in deleted_ids])
        context.user_data.clear()
        text = (
            f"Массовое удаление завершено. Удалено: {ok_count}; ошибок: {error_count}.\n\n"
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
        if text == "В меню":
            context.user_data.clear()
            await update.effective_message.reply_text(
                "Главное меню. Выбери действие на клавиатуре снизу.",
                reply_markup=MAIN_KEYBOARD,
            )
            context.chat_data["reply_keyboard"] = "main"
            return
        if mode is not None and text.casefold() in {"отмена", "назад"}:
            await self._cancel_mode(update, context)
            return
        if text in RECIPE_KEYBOARD_BUTTONS:
            context.user_data.pop("mode", None)
            mode = None
        if text in MAIN_BUTTONS:
            context.user_data.clear()
            mode = None
        if mode is None and text in {"Поиск рецептов", "Рецепты"}:
            await self._send_recipe_list(update, context, page=0)
            return
        if mode is None and text == "Синхронизировать":
            await self._sync_current_recipe_from_message(update, context)
            return
        if mode is None and text == "Удалить":
            await self._confirm_current_recipe_delete_from_message(update, context)
            return
        if mode is None and text == "Поиск":
            group = await self._require_active_group(update)
            if group is None:
                return
            context.user_data.clear()
            context.user_data["mode"] = "recipe_search"
            context.user_data["group_id"] = group.id
            await update.effective_message.reply_text(
                "Пришли часть названия или ингредиента для поиска по рецептам.",
                reply_markup=MAIN_KEYBOARD,
            )
            context.chat_data["reply_keyboard"] = "main"
            return
        if mode is None and text == "Создать из списка":
            group = await self._require_active_group(update)
            if group is None:
                return
            context.user_data.clear()
            context.user_data["mode"] = "recipe_list_title"
            context.user_data["group_id"] = group.id
            await update.effective_message.reply_text("Пришли название рецепта.", reply_markup=MAIN_KEYBOARD)
            context.chat_data["reply_keyboard"] = "main"
            return
        if mode is None and text == "Создать продукт":
            group = await self._require_active_group(update)
            if group is None:
                return
            context.user_data.clear()
            context.user_data["mode"] = "custom_food_title"
            context.user_data["group_id"] = group.id
            context.user_data["custom_food_origin"] = "standalone"
            await update.effective_message.reply_text(
                "Пришли название продукта. Я создам его во всех FatSecret аккаунтах активной группы.",
                reply_markup=MAIN_KEYBOARD,
            )
            context.chat_data["reply_keyboard"] = "main"
            return
        if mode is None and text == "Удалить несколько":
            page = int(context.user_data.get("recipe_list_page") or 0)
            await self._send_batch_delete(update, context, page)
            return
        if mode is None and text == "Меню / Дневник":
            await self.diary(update, context)
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
        elif mode == "recipe_list_rename":
            await self._handle_recipe_list_rename(update, context, text)
        elif mode == "recipe_rename":
            await self._handle_recipe_rename(update, context, text)
        elif mode == "recipe_list_steps":
            await self._handle_recipe_list_steps(update, context, text)
        elif mode == "recipe_list_replace_query":
            await self._handle_recipe_list_replace_query(update, context, text)
        elif mode == "custom_food_title":
            await self._handle_custom_food_title(update, context, text)
        elif mode == "custom_food_barcode":
            await self._handle_custom_food_barcode_text(update, context, text)
        elif mode in {"custom_food_brand", "custom_food_brand_choice"}:
            await self._handle_custom_food_brand(update, context, text)
        elif mode == "custom_food_macros":
            await self._handle_custom_food_macros(update, context, text)
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
        elif mode == "diary_source_date":
            try:
                source_date = _parse_diary_date(
                    text,
                    today=dt.datetime.now(self._refresh_timezone()).date(),
                )
            except ValueError as exc:
                await update.effective_message.reply_text(str(exc))
                return
            context.user_data["diary_source_date"] = source_date.isoformat()
            context.user_data["mode"] = "diary_target_range"
            await update.effective_message.reply_text(
                "Теперь пришли целевой диапазон до 7 дней:\n"
                "<code>15.07.2026 - 17.07.2026</code>\n"
                "или одну дату для одного дня. Можно использовать <code>сегодня</code>/<code>завтра</code>.",
                parse_mode=ParseMode.HTML,
            )
        elif mode == "diary_target_range":
            await self._prepare_diary_preview(update, context, text)
        else:
            await update.effective_message.reply_text(
                "Выбери действие кнопками ниже.",
                reply_markup=MAIN_KEYBOARD,
            )

    async def _cancel_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.user_data.get("custom_food_origin") == "recipe":
            self._clear_custom_food_wizard(context)
            title = str(context.user_data.get("recipe_list_title") or "").strip()
            draft_items = context.user_data.get("recipe_list_draft")
            unresolved = context.user_data.get("recipe_list_unresolved")
            steps = context.user_data.get("recipe_list_steps")
            portions = context.user_data.get("recipe_list_portions")
            if title and isinstance(draft_items, list):
                unresolved = unresolved if isinstance(unresolved, list) else []
                steps = steps if isinstance(steps, list) else []
                portions = portions if isinstance(portions, Decimal) else Decimal("1")
                context.user_data["mode"] = "recipe_list_confirm"
                await update.effective_message.reply_text(
                    _format_recipe_list_draft(title, draft_items, steps, unresolved, portions),
                    reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
                    parse_mode=ParseMode.HTML,
                )
                return
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
        source_group = self.storage.active_group_for_user(user.id)
        moved = (
            self.storage.owned_fatsecret_account_count(user.id, source_group.id)
            if source_group is not None
            else 0
        )
        group = self.storage.create_group(user.id, text)
        self._clear_recipe_cache(context)
        refreshed = await self._refresh_group_after_account_transfer(context, group.id)
        context.user_data.clear()
        transfer_note = f"\nПеренесено твоих FatSecret аккаунтов: {moved}." if moved else ""
        refresh_note = "\nНе удалось сразу обновить рецепты; открой список еще раз." if not refreshed else ""
        await update.effective_message.reply_text(
            f"Группа создана: {html.escape(group.name)}\n"
            f"Код для второго пользователя: <code>{group.invite_code}</code>"
            f"{transfer_note}{refresh_note}",
            reply_markup=MAIN_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )

    async def _handle_group_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        if user is None:
            return
        source_group = self.storage.active_group_for_user(user.id)
        moved = (
            self.storage.owned_fatsecret_account_count(user.id, source_group.id)
            if source_group is not None
            else 0
        )
        try:
            group = self.storage.join_group_by_code(user.id, text)
        except GroupMemberLimitError as exc:
            await update.effective_message.reply_text(
                f"Группа заполнена: сейчас поддерживается максимум {exc.limit} участника."
            )
            return
        if group is None:
            await update.effective_message.reply_text("Не нашел группу с таким кодом. Проверь код и пришли еще раз.")
            return
        self._clear_recipe_cache(context)
        refreshed = await self._refresh_group_after_account_transfer(context, group.id)
        context.user_data.clear()
        transfer_note = f" Перенесено твоих FatSecret аккаунтов: {moved}." if moved else ""
        refresh_note = " Не удалось сразу обновить рецепты; открой список еще раз." if not refreshed else ""
        await update.effective_message.reply_text(
            f"Подключился к группе: {html.escape(group.name)}.{transfer_note}{refresh_note}",
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
        group_id = context.user_data.get("group_id")
        group = self.storage.active_group_for_user(user.id)
        group_id = group_id or (group.id if group else None)
        if group_id is None:
            context.user_data.clear()
            await update.effective_chat.send_message("Сначала создай группу или подключись к группе.")
            return
        account = FatSecretAccountConfig(
            key=f"validate-{user.id}-{int(time.time() * 1000)}",
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
            await status.edit_text(f"FatSecret не принял логин/пароль: {user_safe_error_message(exc)}")
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
        group_id = str(pending["group_id"])
        account_key = self.storage.create_fatsecret_account(
            telegram_id=user.id,
            label=label[:32],
            username=str(pending["username"]),
            password=str(pending["password"]),
            market=str(pending["market"]),
            language=str(pending["language"]),
            group_id=group_id,
        )
        account = self.storage.get_fatsecret_account(account_key)
        if account is None:
            context.user_data.clear()
            await update.effective_message.reply_text("Не удалось сохранить FatSecret аккаунт.")
            return
        self._clear_recipe_cache(context)
        context.user_data.clear()
        status = await update.effective_message.reply_text("FatSecret аккаунт подключен. Загружаю рецепты из этого аккаунта...")
        try:
            imported = await self.sync_engine.refresh_account_recipes(account, group_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("FatSecret cookbook import failed after account connect")
            await status.edit_text(
                f"Аккаунт подключен, но рецепты не загрузились: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Аккаунты", callback_data="accounts:0")]]),
            )
            return
        await status.edit_text(
            f"FatSecret аккаунт подключен. Загружено/смёржено рецептов: {imported}.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Поиск рецептов", callback_data="list:0")],
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
        updated = self.storage.update_fatsecret_account_label(
            account_key,
            label,
            owner_telegram_id=user.id,
        )
        context.user_data.clear()
        await update.effective_message.reply_text(
            f"Ник обновлен: {html.escape(label)}." if updated else "Не удалось обновить ник.",
            reply_markup=self._accounts_keyboard(user.id, group),
            parse_mode=ParseMode.HTML,
        )

    @staticmethod
    def _clear_custom_food_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in (
            "custom_food_origin",
            "custom_food_title",
            "custom_food_barcode",
            "custom_food_barcode_type",
            "custom_food_manufacturer_name",
            "custom_food_brand_query",
            "custom_food_brand_suggestions",
            "custom_food_brand_choice_token",
            "custom_food_definition",
            "custom_food_unresolved_index",
            "custom_food_requested_query",
        ):
            context.user_data.pop(key, None)

    @staticmethod
    def _custom_food_macros_prompt() -> str:
        return (
            "Пришли 4 значения <b>на 100 г</b> в одной строке:\n"
            "<code>ккал белки жиры углеводы</code>\n\n"
            "Например: <code>250 12 8 30</code>"
        )

    @staticmethod
    def _custom_food_brand_prompt() -> str:
        return (
            "Пришли <b>бренд продукта</b> или его часть, например <code>McDonald's</code> или "
            "<code>Санта</code>. Я предложу существующее название из FatSecret. "
            "Также можно нажать «Без бренда»."
        )

    async def _start_recipe_list_food_create(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        unresolved = context.user_data.get("recipe_list_unresolved")
        if not isinstance(unresolved, list) or index < 0 or index >= len(unresolved):
            await query.edit_message_text(
                "Неизвестный ингредиент в черновике больше не найден.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]
                ),
            )
            return
        item = unresolved[index]
        context.user_data["custom_food_origin"] = "recipe"
        context.user_data["custom_food_unresolved_index"] = index
        context.user_data["custom_food_requested_query"] = item.query
        context.user_data["mode"] = "custom_food_title"
        await query.edit_message_text(
            f"Создаем продукт для «{html.escape(item.query)}».\n"
            "Пришли название продукта. Оно появится во всех FatSecret аккаунтах группы.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_custom_food_title(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        title = " ".join(text.strip().split())
        if not title:
            await update.effective_message.reply_text("Название продукта не должно быть пустым.")
            return
        if len(title) > 200:
            await update.effective_message.reply_text("Название слишком длинное. Сократи его до 200 символов.")
            return
        context.user_data["custom_food_title"] = title
        context.user_data["mode"] = "custom_food_barcode"
        context.user_data.pop("custom_food_definition", None)
        await update.effective_message.reply_text(
            "Теперь пришли <b>фото штрих-кода</b>, его цифры текстом или нажми «Без штрих-кода».\n"
            "По фото бот сам распознает EAN/UPC и проверит, нет ли продукта уже в FatSecret.",
            reply_markup=_custom_food_barcode_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_custom_food_barcode_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        if text.strip() == "-":
            context.user_data.pop("custom_food_barcode", None)
            context.user_data.pop("custom_food_barcode_type", None)
            context.user_data["mode"] = "custom_food_brand"
            await update.effective_message.reply_text(
                self._custom_food_brand_prompt(),
                reply_markup=_custom_food_brand_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            decoded = normalize_barcode(text)
        except BarcodeDecodeError as exc:
            await update.effective_message.reply_text(str(exc), reply_markup=_custom_food_barcode_keyboard())
            return
        status = await update.effective_message.reply_text("Проверяю штрих-код в FatSecret...")
        await self._accept_custom_food_barcode(status, context, decoded)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._require_user(update):
            return
        if context.user_data.get("mode") != "custom_food_barcode":
            await update.effective_message.reply_text(
                "Фото штрих-кода можно прислать на шаге создания продукта.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        photos = update.effective_message.photo
        if not photos:
            await update.effective_message.reply_text("В сообщении не нашел фото.")
            return
        status = await update.effective_message.reply_text("Распознаю и проверяю штрих-код...")
        try:
            telegram_file = await photos[-1].get_file()
            image_bytes = bytes(await telegram_file.download_as_bytearray())
            decoded = await asyncio.to_thread(decode_barcode_image, image_bytes)
        except BarcodeDecodeError as exc:
            await status.edit_text(str(exc), reply_markup=_custom_food_barcode_keyboard())
            return
        except Exception:  # noqa: BLE001 - Telegram download failures are user-retryable.
            logger.exception("barcode photo download/decode failed")
            await status.edit_text(
                "Не удалось скачать или прочитать фото. Пришли его еще раз или введи цифры штрих-кода.",
                reply_markup=_custom_food_barcode_keyboard(),
            )
            return
        await self._accept_custom_food_barcode(status, context, decoded)

    async def _accept_custom_food_barcode(
        self,
        status,
        context: ContextTypes.DEFAULT_TYPE,
        decoded: DecodedBarcode,
    ) -> None:
        group_id = context.user_data.get("group_id")
        if not group_id:
            await status.edit_text("Активная группа потеряна. Начни создание продукта заново.")
            return
        try:
            lookup = await self.sync_engine.lookup_barcode(str(group_id), decoded.code)
        except Exception as exc:  # noqa: BLE001 - do not attach an unchecked mapping.
            logger.exception("barcode lookup failed")
            await status.edit_text(
                f"Не удалось проверить штрих-код: {user_safe_error_message(exc)}\n"
                "Попробуй еще раз или создай продукт без штрих-кода.",
                reply_markup=_custom_food_barcode_keyboard(),
            )
            return

        if lookup.found and context.user_data.get("custom_food_origin") == "recipe":
            unresolved = context.user_data.get("recipe_list_unresolved")
            draft_items = context.user_data.get("recipe_list_draft")
            index = context.user_data.get("custom_food_unresolved_index")
            if not isinstance(unresolved, list) or not isinstance(draft_items, list) or not isinstance(index, int):
                await status.edit_text("Черновик рецепта потерян. Начни создание рецепта заново.")
                return
            if index < 0 or index >= len(unresolved):
                await status.edit_text("Неизвестный ингредиент больше не найден в черновике.")
                return
            pending_item = unresolved[index]
            try:
                resolved = await self.sync_engine.barcode_recipe_list_item(
                    str(group_id),
                    lookup,
                    pending_item.grams,
                    pending_item.query,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("known barcode ingredient hydration failed")
                await status.edit_text(
                    f"Штрих-код найден, но продукт не удалось загрузить: {user_safe_error_message(exc)}",
                    reply_markup=_custom_food_barcode_keyboard(),
                )
                return
            draft_items.append(resolved)
            unresolved.pop(index)
            context.user_data["recipe_list_draft"] = draft_items
            context.user_data["recipe_list_unresolved"] = unresolved
            self._clear_custom_food_wizard(context)
            context.user_data["mode"] = "recipe_list_confirm"
            title = str(context.user_data.get("recipe_list_title") or "").strip()
            steps = context.user_data.get("recipe_list_steps")
            steps = steps if isinstance(steps, list) else []
            portions = context.user_data.get("recipe_list_portions")
            portions = portions if isinstance(portions, Decimal) else Decimal("1")
            await status.edit_text(
                _format_recipe_list_draft(title, draft_items, steps, unresolved, portions),
                reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
                parse_mode=ParseMode.HTML,
            )
            return

        if lookup.found:
            known_name = lookup.food_name or f"ID {lookup.food_id}"
            await status.edit_text(
                f"FatSecret уже знает этот штрих-код: <b>{html.escape(known_name)}</b>"
                f"{f' ({html.escape(lookup.brand_name)})' if lookup.brand_name else ''}.\n"
                "Новый продукт с тем же кодом создавать небезопасно. Можно продолжить без привязки штрих-кода.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Продолжить без кода", callback_data="food_ignore_known_barcode:0")],
                        [InlineKeyboardButton("Отмена", callback_data="food_cancel:0")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        context.user_data["custom_food_barcode"] = decoded.code
        context.user_data["custom_food_barcode_type"] = decoded.barcode_type
        context.user_data["mode"] = "custom_food_brand"
        await status.edit_text(
            f"Штрих-код <code>{html.escape(decoded.code)}</code> распознан и пока не найден в FatSecret.\n\n"
            + self._custom_food_brand_prompt(),
            reply_markup=_custom_food_brand_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    async def _skip_custom_food_barcode(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("custom_food_barcode", None)
        context.user_data.pop("custom_food_barcode_type", None)
        context.user_data["mode"] = "custom_food_brand"
        await query.edit_message_text(
            self._custom_food_brand_prompt(),
            reply_markup=_custom_food_brand_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    async def _ignore_known_custom_food_barcode(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._skip_custom_food_barcode(query, context)

    async def _handle_custom_food_brand(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        brand = " ".join(text.strip().split())
        if brand == "-":
            self._set_custom_food_brand(context, "")
            await update.effective_message.reply_text(
                self._custom_food_macros_prompt(),
                parse_mode=ParseMode.HTML,
            )
            return
        elif not brand:
            await update.effective_message.reply_text(
                "Название бренда не должно быть пустым. Введи бренд или нажми «Без бренда».",
                reply_markup=_custom_food_brand_keyboard(),
            )
            return
        elif len(brand) > 200:
            await update.effective_message.reply_text(
                "Название бренда слишком длинное. Сократи его до 200 символов.",
                reply_markup=_custom_food_brand_keyboard(),
            )
            return
        group_id = context.user_data.get("group_id")
        if not group_id:
            await update.effective_message.reply_text("Активная группа потеряна. Начни создание продукта заново.")
            return

        status = await update.effective_message.reply_text("Ищу существующий бренд в FatSecret...")
        catalog_error = False
        try:
            suggestions = await self.sync_engine.suggest_custom_food_brands(str(group_id), brand)
        except Exception:  # noqa: BLE001 - catalog failure must not block manual product creation.
            logger.exception("custom food manufacturer catalog lookup failed")
            suggestions = []
            catalog_error = True

        context.user_data.pop("custom_food_manufacturer_name", None)
        context.user_data["custom_food_brand_query"] = brand
        context.user_data["custom_food_brand_suggestions"] = suggestions
        choice_token = f"{time.monotonic_ns():x}"[-10:]
        context.user_data["custom_food_brand_choice_token"] = choice_token
        context.user_data["mode"] = "custom_food_brand_choice"
        if suggestions:
            prompt = (
                "Нашёл существующие бренды. Выбери каноническое название FatSecret либо явно "
                "используй введённый вариант. Можно также прислать другой запрос."
            )
        elif catalog_error:
            prompt = (
                "Каталог брендов сейчас недоступен. Можно явно использовать введённый вариант, "
                "прислать другой запрос или продолжить без бренда."
            )
        else:
            prompt = (
                "Совпадений в каталоге FatSecret не найдено. Можно явно использовать введённый "
                "вариант как новый, прислать другой запрос или продолжить без бренда."
            )
        await status.edit_text(
            prompt,
            reply_markup=_custom_food_brand_suggestions_keyboard(suggestions, brand, choice_token),
        )

    @staticmethod
    def _set_custom_food_brand(context: ContextTypes.DEFAULT_TYPE, brand: str) -> None:
        if brand:
            context.user_data["custom_food_manufacturer_name"] = brand
        else:
            context.user_data.pop("custom_food_manufacturer_name", None)
        context.user_data.pop("custom_food_brand_query", None)
        context.user_data.pop("custom_food_brand_suggestions", None)
        context.user_data.pop("custom_food_brand_choice_token", None)
        context.user_data["mode"] = "custom_food_macros"

    async def _pick_custom_food_brand(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        value: str,
    ) -> None:
        choice_token, separator, index_text = value.partition(":")
        try:
            index = int(index_text)
        except ValueError:
            index = -1
        suggestions = context.user_data.get("custom_food_brand_suggestions")
        expected_token = str(context.user_data.get("custom_food_brand_choice_token") or "")
        if expected_token and choice_token != expected_token:
            await query.edit_message_text("Этот список брендов устарел. Используй последнее сообщение бота.")
            return
        if (
            not separator
            or not expected_token
            or not isinstance(suggestions, list)
            or index < 0
            or index >= len(suggestions)
        ):
            await query.edit_message_text(
                "Список брендов устарел. Пришли название бренда ещё раз.",
                reply_markup=_custom_food_brand_keyboard(),
            )
            context.user_data["mode"] = "custom_food_brand"
            return
        self._set_custom_food_brand(context, str(suggestions[index]))
        await query.edit_message_text(self._custom_food_macros_prompt(), parse_mode=ParseMode.HTML)

    async def _use_custom_food_brand_text(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        choice_token: str,
    ) -> None:
        brand = str(context.user_data.get("custom_food_brand_query") or "").strip()
        expected_token = str(context.user_data.get("custom_food_brand_choice_token") or "")
        if expected_token and choice_token != expected_token:
            await query.edit_message_text("Этот список брендов устарел. Используй последнее сообщение бота.")
            return
        if not brand or not expected_token:
            await query.edit_message_text(
                "Введённое название потеряно. Пришли бренд ещё раз.",
                reply_markup=_custom_food_brand_keyboard(),
            )
            context.user_data["mode"] = "custom_food_brand"
            return
        self._set_custom_food_brand(context, brand)
        await query.edit_message_text(self._custom_food_macros_prompt(), parse_mode=ParseMode.HTML)

    async def _skip_custom_food_brand(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._set_custom_food_brand(context, "")
        await query.edit_message_text(self._custom_food_macros_prompt(), parse_mode=ParseMode.HTML)

    async def _handle_custom_food_macros(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        title = str(context.user_data.get("custom_food_title") or "").strip()
        if not title:
            context.user_data["mode"] = "custom_food_title"
            await update.effective_message.reply_text("Название продукта потеряно. Пришли его еще раз.")
            return
        try:
            nutrients = _parse_custom_food_macros(text)
        except (InvalidOperation, ValueError) as exc:
            await update.effective_message.reply_text(str(exc), parse_mode=ParseMode.HTML)
            return
        definition = CustomFoodDefinition(
            source_recipe_id="",
            title=title,
            manufacturer_name=str(context.user_data.get("custom_food_manufacturer_name") or ""),
            serving_type="Per100g",
            serving_size="",
            metric_serving_size="100g",
            nutrients=nutrients,
            barcode=str(context.user_data.get("custom_food_barcode") or ""),
            barcode_type=str(context.user_data.get("custom_food_barcode_type") or ""),
        )
        context.user_data["custom_food_definition"] = definition
        context.user_data["mode"] = "custom_food_confirm"
        await update.effective_message.reply_text(
            _format_custom_food_draft(definition),
            reply_markup=_custom_food_confirm_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    async def _create_custom_food(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_id: int,
    ) -> None:
        group_id = context.user_data.get("group_id")
        definition = context.user_data.get("custom_food_definition")
        if not group_id or not isinstance(definition, CustomFoodDefinition):
            await query.edit_message_text("Черновик продукта потерян. Начни создание заново.")
            return
        await query.edit_message_text("Создаю продукт во всех FatSecret аккаунтах группы и проверяю результат...")
        try:
            created = await self.sync_engine.create_custom_food_for_group(
                str(group_id),
                definition,
                telegram_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("group custom food create failed")
            await query.edit_message_text(
                f"Ошибка создания продукта: {user_safe_error_message(exc)}",
                reply_markup=_custom_food_confirm_keyboard(),
            )
            return

        if context.user_data.get("custom_food_origin") == "recipe":
            unresolved = context.user_data.get("recipe_list_unresolved")
            draft_items = context.user_data.get("recipe_list_draft")
            index = context.user_data.get("custom_food_unresolved_index")
            if not isinstance(unresolved, list) or not isinstance(draft_items, list) or not isinstance(index, int):
                await query.edit_message_text(
                    "Продукт создан, но черновик рецепта потерян. Начни рецепт заново; продукт уже доступен в FatSecret."
                )
                return
            if index < 0 or index >= len(unresolved):
                await query.edit_message_text(
                    "Продукт создан, но неизвестная позиция исчезла из черновика. Продукт уже доступен в FatSecret."
                )
                return
            pending_item = unresolved[index]
            resolved = self.sync_engine.custom_food_recipe_list_item(
                definition,
                created,
                pending_item.grams,
                pending_item.query,
            )
            draft_items.append(resolved)
            unresolved.pop(index)
            context.user_data["recipe_list_draft"] = draft_items
            context.user_data["recipe_list_unresolved"] = unresolved
            self._clear_custom_food_wizard(context)
            context.user_data["mode"] = "recipe_list_confirm"
            await self._edit_recipe_list_draft(query, context)
            return

        account_labels = self._account_labels_for_group(str(group_id))
        context.user_data.clear()
        await query.edit_message_text(
            _format_custom_food_created(created.title, created.food_ids, account_labels),
            parse_mode=ParseMode.HTML,
        )
        await self._ensure_main_keyboard(query.message, context)

    async def _cancel_custom_food(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.user_data.get("custom_food_origin") == "recipe":
            self._clear_custom_food_wizard(context)
            await self._edit_recipe_list_draft(query, context)
            return
        context.user_data.clear()
        await query.edit_message_text("Создание продукта отменено.")
        await self._ensure_main_keyboard(query.message, context)

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
            "Пришли ингредиенты списком. Последнее число в строке считаю граммами.\n"
            "Первой строкой обязательно укажи количество порций: <code>Порций: 4</code>.\n"
            "Шаги можно добавить в этом же сообщении после строки <b>Шаги:</b>.\n\n"
            "Например:\n"
            "Порций: 4\n"
            "Филе 100\n"
            "Теос греческий 200\n\n"
            "Шаги:\n"
            "1. Нарезать\n"
            "2. Запечь",
            parse_mode=ParseMode.HTML,
        )

    async def _start_recipe_list_rename(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        draft_items = context.user_data.get("recipe_list_draft")
        if not title or not isinstance(draft_items, list):
            await query.edit_message_text(
                "Черновик устарел. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        context.user_data["mode"] = "recipe_list_rename"
        await query.edit_message_text(
            f"Текущее имя: <b>{html.escape(title)}</b>\nПришли новое имя рецепта.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]),
            parse_mode=ParseMode.HTML,
        )

    async def _start_recipe_list_steps(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        draft_items = context.user_data.get("recipe_list_draft")
        if not title or not isinstance(draft_items, list):
            await query.edit_message_text(
                "Черновик устарел. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        context.user_data["mode"] = "recipe_list_steps"
        await query.edit_message_text(
            "Пришли шаги приготовления, каждый шаг с новой строки.\n"
            f"Сохраню первые {MAX_RECIPE_STEPS} шагов в FatSecret.\n\n"
            "Отправь <code>-</code>, чтобы очистить шаги.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_recipe_list_rename(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        title = text.strip()
        draft_items = context.user_data.get("recipe_list_draft")
        if not isinstance(draft_items, list):
            context.user_data.clear()
            await update.effective_message.reply_text("Черновик устарел. Начни создание заново из списка рецептов.")
            return
        if not title:
            await update.effective_message.reply_text("Название не должно быть пустым.")
            return
        context.user_data["recipe_list_title"] = title
        context.user_data["mode"] = "recipe_list_confirm"
        steps = context.user_data.get("recipe_list_steps")
        steps = steps if isinstance(steps, list) else []
        unresolved = context.user_data.get("recipe_list_unresolved")
        unresolved = unresolved if isinstance(unresolved, list) else []
        portions = context.user_data.get("recipe_list_portions")
        portions = portions if isinstance(portions, Decimal) else Decimal("1")
        await update.effective_message.reply_text(
            _format_recipe_list_draft(title, draft_items, steps, unresolved, portions),
            reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_recipe_list_steps(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        draft_items = context.user_data.get("recipe_list_draft")
        if not title or not isinstance(draft_items, list):
            context.user_data.clear()
            await update.effective_message.reply_text("Черновик устарел. Начни создание заново из списка рецептов.")
            return
        steps = _parse_recipe_steps(text)
        context.user_data["recipe_list_steps"] = steps
        context.user_data["mode"] = "recipe_list_confirm"
        unresolved = context.user_data.get("recipe_list_unresolved")
        unresolved = unresolved if isinstance(unresolved, list) else []
        portions = context.user_data.get("recipe_list_portions")
        portions = portions if isinstance(portions, Decimal) else Decimal("1")
        await update.effective_message.reply_text(
            _format_recipe_list_draft(title, draft_items, steps, unresolved, portions),
            reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
            parse_mode=ParseMode.HTML,
        )

    async def _handle_recipe_list_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        user = update.effective_user
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        group_id = context.user_data.get("group_id")
        if user is None or not title or not group_id:
            context.user_data.clear()
            await update.effective_message.reply_text("Контекст создания рецепта потерян. Начни заново из списка рецептов.")
            return
        portions, items, bad_lines, steps = _parse_recipe_list_payload(text)
        if bad_lines:
            lines = "\n".join(f"- {html.escape(line)}" for line in bad_lines)
            await update.effective_message.reply_text(
                "Эти строки я совсем не понимаю:\n"
                f"{lines}\n\n"
                "Формат: название и последним токеном масса в граммах.",
                reply_markup=_recipe_list_input_error_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return
        if not items:
            await update.effective_message.reply_text(
                "Не вижу ингредиентов. Пришли строки вида: Филе 100",
                reply_markup=_recipe_list_input_error_keyboard(),
            )
            return
        if portions is None:
            await update.effective_message.reply_text(
                "Не вижу количество порций. Добавь первой строкой, например:\n"
                "<code>Порций: 4</code>",
                reply_markup=_recipe_list_input_error_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return
        status = await update.effective_message.reply_text("Подбираю ингредиенты по твоим прошлым рецептам и FatSecret...")
        try:
            draft = await self.sync_engine.resolve_recipe_list_items(str(group_id), items)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recipe list resolve failed")
            await status.edit_text(
                f"Не удалось подобрать ингредиенты: {user_safe_error_message(exc)}",
                reply_markup=_recipe_list_input_error_keyboard(),
            )
            return
        context.user_data["recipe_list_draft"] = draft.items
        context.user_data["recipe_list_unresolved"] = draft.unresolved
        context.user_data["recipe_list_portions"] = portions
        context.user_data["mode"] = "recipe_list_confirm"
        context.user_data["recipe_list_steps"] = steps
        await status.edit_text(
            _format_recipe_list_draft(title, draft.items, steps, draft.unresolved, portions),
            reply_markup=_recipe_list_draft_keyboard(draft.items, steps, draft.unresolved),
            parse_mode=ParseMode.HTML,
        )

    async def _edit_recipe_list_draft(self, query, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.user_data.get("custom_food_origin") == "recipe":
            self._clear_custom_food_wizard(context)
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
        context.user_data.pop("recipe_list_candidates_cache", None)
        context.user_data.pop("recipe_list_candidates_exhausted", None)
        context.user_data.pop("recipe_list_replace_query", None)
        context.user_data.pop("recipe_list_replace_kind", None)
        context.user_data.pop("recipe_list_duplicate_id", None)
        context.user_data.pop("recipe_list_duplicate_ref", None)
        context.user_data.pop("recipe_list_replace_existing_id", None)
        context.user_data.pop("recipe_list_replace_existing_ref", None)
        context.user_data.pop("recipe_list_copy_base_title", None)
        steps = context.user_data.get("recipe_list_steps")
        steps = steps if isinstance(steps, list) else []
        unresolved = context.user_data.get("recipe_list_unresolved")
        unresolved = unresolved if isinstance(unresolved, list) else []
        portions = context.user_data.get("recipe_list_portions")
        portions = portions if isinstance(portions, Decimal) else Decimal("1")
        await query.edit_message_text(
            _format_recipe_list_draft(title, draft_items, steps, unresolved, portions),
            reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
            parse_mode=ParseMode.HTML,
        )

    async def _show_recipe_list_duplicate(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        duplicate: Recipe,
    ) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        group_id = context.user_data.get("group_id")
        copy_title = _next_live_recipe_title(
            title,
            self._recipe_cache(context, str(group_id)) or [],
        )
        context.user_data["recipe_list_duplicate_id"] = duplicate.id
        context.user_data["recipe_list_duplicate_ref"] = duplicate
        await query.edit_message_text(
            "Рецепт с таким названием уже есть.\n\n"
            "Можно обновить существующий: я сначала создам новую версию с временным именем, "
            "потом удалю старую и переименую новую обратно.\n\n"
            f"Для копии использую имя: {html.escape(copy_title)}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Обновить существующий", callback_data="recipe_list_replace_existing:0")],
                    [InlineKeyboardButton("Создать копию", callback_data="recipe_list_copy:0")],
                    [InlineKeyboardButton("Изменить имя", callback_data="recipe_list_rename:0")],
                    [InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")],
                ]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _replace_existing_recipe_list_from_draft(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_id: int,
    ) -> None:
        duplicate_id = context.user_data.get("recipe_list_duplicate_id")
        duplicate_ref = context.user_data.get("recipe_list_duplicate_ref")
        if isinstance(duplicate_id, str) and self.storage.get_recipe(duplicate_id) is not None:
            context.user_data["recipe_list_replace_existing_id"] = duplicate_id
        elif isinstance(duplicate_ref, Recipe):
            context.user_data["recipe_list_replace_existing_ref"] = duplicate_ref
        else:
            await query.edit_message_text(
                "Не нашел рецепт для замены. Нажми создать еще раз.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]),
            )
            return
        context.user_data.pop("recipe_list_copy_base_title", None)
        await self._create_recipe_list_from_draft(query, context, telegram_id)

    async def _copy_existing_recipe_list_from_draft(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_id: int,
    ) -> None:
        title = str(context.user_data.get("recipe_list_title") or "").strip()
        group_id = context.user_data.get("group_id")
        if not title or not group_id:
            await query.edit_message_text(
                "Черновик устарел. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        context.user_data["recipe_list_copy_base_title"] = title
        context.user_data.pop("recipe_list_duplicate_id", None)
        context.user_data.pop("recipe_list_duplicate_ref", None)
        context.user_data.pop("recipe_list_replace_existing_id", None)
        context.user_data.pop("recipe_list_replace_existing_ref", None)
        await self._create_recipe_list_from_draft(query, context, telegram_id)

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
        context.user_data["recipe_list_replace_kind"] = "resolved"
        context.user_data["recipe_list_replace_index"] = index
        context.user_data.pop("recipe_list_candidates", None)
        context.user_data.pop("recipe_list_candidates_cache", None)
        context.user_data.pop("recipe_list_candidates_exhausted", None)
        context.user_data.pop("recipe_list_replace_query", None)
        await query.edit_message_text(
            f"Что искать вместо «{html.escape(item.ingredient.title)}»?\n"
            f"Массу оставлю {_format_decimal(item.grams)}г.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _start_recipe_list_resolve(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        unresolved = context.user_data.get("recipe_list_unresolved")
        if not isinstance(unresolved, list) or index < 0 or index >= len(unresolved):
            await query.edit_message_text(
                "Неизвестный ингредиент в черновике больше не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]),
            )
            return
        item = unresolved[index]
        context.user_data["mode"] = "recipe_list_replace_query"
        context.user_data["recipe_list_replace_kind"] = "unresolved"
        context.user_data["recipe_list_replace_index"] = index
        context.user_data.pop("recipe_list_candidates", None)
        context.user_data.pop("recipe_list_candidates_cache", None)
        context.user_data.pop("recipe_list_candidates_exhausted", None)
        context.user_data.pop("recipe_list_replace_query", None)
        await query.edit_message_text(
            f"Что искать для «{html.escape(item.query)}»?\n"
            f"Массу оставлю {_format_decimal(item.grams)}г.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    async def _drop_recipe_list_unresolved(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        index: int,
    ) -> None:
        unresolved = context.user_data.get("recipe_list_unresolved")
        if not isinstance(unresolved, list) or index < 0 or index >= len(unresolved):
            await query.edit_message_text(
                "Неизвестный ингредиент в черновике больше не найден.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]),
            )
            return
        unresolved.pop(index)
        context.user_data["recipe_list_unresolved"] = unresolved
        await self._edit_recipe_list_draft(query, context)

    async def _handle_recipe_list_replace_query(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        user = update.effective_user
        group_id = context.user_data.get("group_id")
        draft_items = context.user_data.get("recipe_list_draft")
        unresolved = context.user_data.get("recipe_list_unresolved")
        index = context.user_data.get("recipe_list_replace_index")
        replace_kind = context.user_data.get("recipe_list_replace_kind") or "resolved"
        if user is None or not group_id or not isinstance(draft_items, list) or not isinstance(index, int):
            context.user_data.clear()
            await update.effective_message.reply_text(
                "Контекст замены потерян. Начни создание заново из списка рецептов."
            )
            return
        if replace_kind == "unresolved":
            if not isinstance(unresolved, list) or index < 0 or index >= len(unresolved):
                context.user_data.clear()
                await update.effective_message.reply_text("Неизвестный ингредиент больше не найден. Начни создание заново.")
                return
        elif index < 0 or index >= len(draft_items):
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
        context.user_data["recipe_list_candidates_cache"] = []
        context.user_data["recipe_list_candidates_exhausted"] = False
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
        unresolved = context.user_data.get("recipe_list_unresolved")
        index = context.user_data.get("recipe_list_replace_index")
        search_query = str(context.user_data.get("recipe_list_replace_query") or "").strip()
        replace_kind = context.user_data.get("recipe_list_replace_kind") or "resolved"
        if not group_id or not isinstance(draft_items, list) or not isinstance(index, int) or not search_query:
            context.user_data.clear()
            await self._edit_flow_message(
                message,
                "Контекст замены потерян. Начни создание заново из списка рецептов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        if replace_kind == "unresolved":
            if not isinstance(unresolved, list) or index < 0 or index >= len(unresolved):
                context.user_data.clear()
                await self._edit_flow_message(
                    message,
                    "Неизвестный ингредиент больше не найден. Начни создание заново.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
                )
                return
            grams = unresolved[index].grams
        elif index < 0 or index >= len(draft_items):
            context.user_data.clear()
            await self._edit_flow_message(
                message,
                "Ингредиент в черновике больше не найден. Начни создание заново.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("К списку", callback_data="list:0")]]),
            )
            return
        else:
            grams = draft_items[index].grams
        page = max(0, page)
        start = page * RECIPE_LIST_CANDIDATES_PAGE_SIZE
        end = start + RECIPE_LIST_CANDIDATES_PAGE_SIZE
        try:
            await self._ensure_recipe_list_candidate_cache(context, str(group_id), search_query, grams, end + 1)
        except Exception as exc:  # noqa: BLE001
            logger.exception("recipe list replacement search failed")
            await self._edit_flow_message(
                message,
                f"Не удалось найти замену: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")],
                        [InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")],
                    ]
                ),
            )
            return

        cache = context.user_data.get("recipe_list_candidates_cache")
        if not isinstance(cache, list):
            cache = []
        exhausted = bool(context.user_data.get("recipe_list_candidates_exhausted"))
        visible_candidates = cache[start:end]
        has_next = len(cache) > end or not exhausted
        if not visible_candidates:
            await self._edit_flow_message(
                message,
                f"Не нашел вариантов для «{html.escape(search_query)}». Пришли другой запрос.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Назад к проверке", callback_data="recipe_list_back:0")],
                        [InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")],
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return
        context.user_data["mode"] = "recipe_list_replace_query"
        context.user_data["recipe_list_replace_page"] = page
        context.user_data["recipe_list_candidates"] = visible_candidates
        await self._edit_flow_message(
            message,
            _format_recipe_list_candidates(search_query, grams, visible_candidates, page),
            reply_markup=_recipe_list_candidate_keyboard(visible_candidates, page, has_next),
            parse_mode=ParseMode.HTML,
        )

    async def _ensure_recipe_list_candidate_cache(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        group_id: str,
        search_query: str,
        grams: Decimal,
        min_count: int,
    ) -> None:
        cache = context.user_data.get("recipe_list_candidates_cache")
        if not isinstance(cache, list):
            cache = []
        exhausted = bool(context.user_data.get("recipe_list_candidates_exhausted"))
        while len(cache) < min_count and not exhausted:
            offset = len(cache)
            fetched = await self.sync_engine.recipe_list_candidates(
                group_id,
                search_query,
                grams,
                limit=RECIPE_LIST_CANDIDATES_PREFETCH_SIZE + 1,
                offset=offset,
            )
            if len(fetched) <= RECIPE_LIST_CANDIDATES_PREFETCH_SIZE:
                exhausted = True
                cache.extend(fetched)
            else:
                cache.extend(fetched[:RECIPE_LIST_CANDIDATES_PREFETCH_SIZE])
        context.user_data["recipe_list_candidates_cache"] = cache
        context.user_data["recipe_list_candidates_exhausted"] = exhausted

    async def _pick_recipe_list_candidate(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        candidate_index: int,
    ) -> None:
        draft_items = context.user_data.get("recipe_list_draft")
        unresolved = context.user_data.get("recipe_list_unresolved")
        candidates = context.user_data.get("recipe_list_candidates")
        replace_index = context.user_data.get("recipe_list_replace_index")
        replace_kind = context.user_data.get("recipe_list_replace_kind") or "resolved"
        if (
            not isinstance(draft_items, list)
            or not isinstance(candidates, list)
            or not isinstance(replace_index, int)
            or candidate_index < 0
            or candidate_index >= len(candidates)
        ):
            await query.edit_message_text(
                "Выбор замены устарел. Вернись к проверке и попробуй еще раз.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]
                ),
            )
            return
        if replace_kind == "unresolved":
            if not isinstance(unresolved, list) or replace_index < 0 or replace_index >= len(unresolved):
                await query.edit_message_text(
                    "Неизвестный ингредиент больше не найден. Вернись к проверке и попробуй еще раз.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")]]
                    ),
                )
                return
            draft_items.append(candidates[candidate_index])
            unresolved.pop(replace_index)
            context.user_data["recipe_list_unresolved"] = unresolved
        else:
            if replace_index < 0 or replace_index >= len(draft_items):
                await query.edit_message_text(
                    "Ингредиент в черновике больше не найден. Вернись к проверке и попробуй еще раз.",
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
        unresolved = context.user_data.get("recipe_list_unresolved")
        portions = context.user_data.get("recipe_list_portions")
        steps = context.user_data.get("recipe_list_steps")
        if not title or not group_id or not isinstance(draft_items, list):
            await query.edit_message_text("Черновик устарел. Начни создание заново из списка рецептов.")
            return
        if not isinstance(portions, Decimal):
            await query.edit_message_text("Количество порций потеряно. Начни создание заново из списка рецептов.")
            return
        unresolved = unresolved if isinstance(unresolved, list) else []
        steps = steps if isinstance(steps, list) else []
        if unresolved:
            await query.edit_message_text(
                "Сначала заполни или удали неизвестные ингредиенты.",
                reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
            )
            return
        replace_existing_id = context.user_data.get("recipe_list_replace_existing_id")
        if replace_existing_id is not None and not isinstance(replace_existing_id, str):
            await query.edit_message_text("Контекст замены устарел. Нажми создать еще раз.")
            context.user_data.pop("recipe_list_replace_existing_id", None)
            return
        replace_existing_ref = context.user_data.get("recipe_list_replace_existing_ref")
        if replace_existing_ref is not None and not isinstance(replace_existing_ref, Recipe):
            await query.edit_message_text("Контекст замены устарел. Нажми создать еще раз.")
            context.user_data.pop("recipe_list_replace_existing_ref", None)
            return
        if not draft_items:
            await query.edit_message_text(
                "В рецепте не осталось ингредиентов. Добавь хотя бы один ингредиент или отмени черновик.",
                reply_markup=_recipe_list_draft_keyboard(draft_items, steps, unresolved),
            )
            return
        await query.edit_message_text("Проверяю актуальный список рецептов в FatSecret...")
        try:
            live_recipes = await self.sync_engine.load_remote_recipe_index(str(group_id))
        except Exception as exc:  # noqa: BLE001 - creation must not rely on stale duplicate data.
            logger.exception("live recipe duplicate check failed")
            await query.edit_message_text(
                f"Не удалось проверить актуальные названия рецептов: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")],
                        [InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")],
                    ]
                ),
            )
            return
        self._set_recipe_cache(context, str(group_id), live_recipes)

        copy_base_title = context.user_data.get("recipe_list_copy_base_title")
        if isinstance(copy_base_title, str) and copy_base_title.strip():
            title = _next_live_recipe_title(copy_base_title, live_recipes)
            context.user_data["recipe_list_title"] = title
            context.user_data.pop("recipe_list_copy_base_title", None)
        elif replace_existing_id is not None or replace_existing_ref is not None:
            selected = replace_existing_ref or self.storage.get_recipe(str(replace_existing_id))
            selected_identities = _recipe_remote_identities(selected) if selected is not None else set()
            fresh_replacement = next(
                (
                    recipe
                    for recipe in live_recipes
                    if selected_identities.intersection(_recipe_remote_identities(recipe))
                ),
                None,
            )
            if fresh_replacement is None:
                context.user_data.pop("recipe_list_replace_existing_id", None)
                context.user_data.pop("recipe_list_replace_existing_ref", None)
                await query.edit_message_text(
                    "Рецепт для замены изменился или был удален. Проверь свежий список и выбери замену заново.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0")],
                            [InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")],
                        ]
                    ),
                )
                return
            replace_existing_id = None
            replace_existing_ref = fresh_replacement
            context.user_data.pop("recipe_list_replace_existing_id", None)
            context.user_data["recipe_list_replace_existing_ref"] = fresh_replacement
        else:
            duplicate = self._duplicate_recipe_for_title(context, str(group_id), title)
            if duplicate is not None:
                await self._show_recipe_list_duplicate(query, context, duplicate)
                return
        await query.edit_message_text("Создаю рецепт в FatSecret аккаунтах группы...")
        try:
            created = await self.sync_engine.create_recipe_from_list(
                str(group_id),
                title,
                draft_items,
                telegram_id,
                portions=portions,
                steps=steps,
                replace_existing_recipe_id=replace_existing_id,
                replace_existing_recipe_ref=replace_existing_ref,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("recipe list create failed")
            await query.edit_message_text(
                f"Ошибка создания рецепта: {user_safe_error_message(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Изменить имя", callback_data="recipe_list_rename:0"),
                            InlineKeyboardButton("К проверке", callback_data="recipe_list_back:0"),
                        ],
                        [InlineKeyboardButton("Отмена", callback_data="recipe_list_cancel:0")],
                    ]
                ),
            )
            return
        stored_recipe = self.storage.get_recipe(created.recipe_id)
        if created.replaced_recipe_id is not None:
            self._remove_cached_recipe(context, str(group_id), created.replaced_recipe_id)
        if stored_recipe is not None:
            self._replace_cached_recipe(context, str(group_id), stored_recipe)
        context.user_data.clear()
        account_labels = self._account_labels_for_group(str(group_id))
        lines = [
            f"{account_labels.get(result.account_key, result.account_key)}: "
            f"{'OK' if result.ok else 'ERROR'} {result.remote_recipe_id or ''} {result.message}"
            for result in created.results
        ]
        if created.replaced_recipe_id is not None:
            if created.temporary_title:
                lines.append(f"Временное имя: {created.temporary_title}")
            if created.title:
                lines.append(f"Итоговое имя: {created.title}")
            if created.replacement_results:
                lines.append("")
                lines.append("Удаление старого:")
                lines.extend(
                    f"{account_labels.get(result.account_key, result.account_key)}: "
                    f"{'OK' if result.ok else 'ERROR'} {result.remote_recipe_id or ''} {result.message}"
                    for result in created.replacement_results
                )
            if created.rename_results:
                lines.append("")
                lines.append("Переименование нового:")
                lines.extend(
                    f"{account_labels.get(result.account_key, result.account_key)}: "
                    f"{'OK' if result.ok else 'ERROR'} {result.remote_recipe_id or ''} {result.message}"
                    for result in created.rename_results
                )
        header = "Замена завершена:" if created.replaced_recipe_id is not None else "Создание завершено:"
        await query.edit_message_text(
            header + "\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Открыть рецепт", callback_data=f"open:{created.recipe_id}")],
                    [InlineKeyboardButton("К списку", callback_data="list:0")],
                ]
            ),
        )

    async def _handle_recipe_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        self._cancel_recipe_warning_render(context)
        group_id = context.user_data.get("group_id")
        if not group_id:
            group = await self._require_active_group(update)
            if group is None:
                return
            group_id = group.id
        cached = self._recipe_cache(context, str(group_id))
        if cached is None:
            await update.effective_message.reply_text(
                "Список рецептов еще не загружен. Нажми «Поиск рецептов», потом пришли текст для поиска.",
                reply_markup=MAIN_KEYBOARD,
            )
            context.chat_data["reply_keyboard"] = "main"
            return
        recipes = self._filter_recipes(text, cached)
        context.user_data["recipe_search_query"] = text
        context.user_data[RECIPE_SEARCH_IDS_KEY] = [recipe.id for recipe in recipes]
        context.user_data["group_id"] = group_id
        context.user_data["mode"] = "recipe_search"
        if not recipes:
            await update.effective_message.reply_text(
                f"По запросу «{html.escape(text)}» ничего не найдено. Пришли другой текст.",
                parse_mode=ParseMode.HTML,
            )
            return
        page_recipes, page, total_count = self._recipe_page(recipes, 0)
        needs_reload = self._recipe_cache_needs_reload(context, str(group_id))
        product_difference_ids, pending, connected_account_keys = self._recipe_warning_state(
            context,
            str(group_id),
            cached,
            refresh_expired=not needs_reload,
        )
        visible_difference_ids = product_difference_ids & {recipe.id for recipe in page_recipes}
        account_labels = self._account_labels_for_group(group_id)
        title = f"Найдено рецептов: {len(recipes)}"
        sent = await update.effective_message.reply_text(
            _recipe_list_message(
                title,
                has_product_differences=bool(visible_difference_ids),
                checking_versions=bool(pending and getattr(context, "application", None)),
                needs_reload=needs_reload,
            ),
            reply_markup=self._recipe_list_keyboard(
                page_recipes,
                page,
                "searchpage",
                account_labels,
                total_count=total_count,
                product_difference_ids=visible_difference_ids,
                needs_reload=needs_reload,
            ),
        )
        self._schedule_recipe_warning_update(
            sent,
            context,
            group_id=str(group_id),
            recipes=page_recipes,
            pending=pending,
            connected_account_keys=connected_account_keys,
            page=page,
            page_action="searchpage",
            total_count=total_count,
            title=title,
            account_labels=account_labels,
        )
