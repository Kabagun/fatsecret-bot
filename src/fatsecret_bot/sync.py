from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .fatsecret_client import (
    FatSecretActionError,
    FatSecretClient,
    FatSecretError,
    FatSecretNotCustomFoodError,
    user_safe_error_message,
)
from .models import (
    BarcodeLookupResult,
    CustomFoodDefinition,
    DiaryCopyDateResult,
    DiaryCopyPreview,
    DiaryCopyResult,
    FatSecretAccountConfig,
    FatSecretDeviceConfig,
    FoodDiaryEntry,
    FoodDiaryWriteEntry,
    FoodSearchResult,
    Ingredient,
    Recipe,
    RemoteRecipeVariant,
)
from .portions import grams_from_portion, is_explicit_weight_portion, portion_unit_size
from .recipe_compare import recipe_content_fingerprint, recipe_fingerprint, recipe_fingerprint_diff
from .storage import Storage, normalize_title

logger = logging.getLogger(__name__)
SEARCH_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
INGREDIENT_NORMALIZE_CONCURRENCY = 6
MAX_DIARY_COPY_DAYS = 7


@dataclass(frozen=True)
class AccountSyncResult:
    account_key: str
    remote_recipe_id: str | None
    ok: bool
    message: str


@dataclass(frozen=True)
class IngredientSyncStats:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0

    def message(self) -> str:
        """Return a compact user-facing summary of ingredient propagation."""
        parts: list[str] = []
        if self.added:
            parts.append(f"добавлено ингредиентов: {self.added}")
        if self.updated:
            parts.append(f"обновлено ингредиентов: {self.updated}")
        if self.unchanged:
            parts.append(f"без изменений: {self.unchanged}")
        if self.deleted:
            parts.append(f"удалено лишних ингредиентов: {self.deleted}")
        return "; ".join(parts) if parts else "ингредиентов нет"


class _TargetSyncFailure(FatSecretError):
    """Carry a newly created target identity through a handled rollback failure."""

    def __init__(self, message: str, remote_id: str, rolled_back: bool) -> None:
        super().__init__(message)
        self.remote_id = remote_id
        self.rolled_back = rolled_back


@dataclass(frozen=True)
class _RecipeSourceSnapshot:
    transport: Recipe
    display: Recipe


@dataclass(frozen=True)
class RecipeListItem:
    query: str
    grams: Decimal


@dataclass(frozen=True)
class ResolvedRecipeListItem:
    requested_query: str
    grams: Decimal
    ingredient: Ingredient
    source: str
    brand: str = ""
    usage_count: int = 0
    energy_per_100g: Decimal | None = None
    protein_per_100g: Decimal | None = None
    fat_per_100g: Decimal | None = None
    carbohydrate_per_100g: Decimal | None = None
    custom_food_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RecipeListDraft:
    items: list[ResolvedRecipeListItem]
    unresolved: list[RecipeListItem]
    steps: list[str] | None = None


@dataclass(frozen=True)
class RecipeCreateResult:
    recipe_id: str
    results: list[AccountSyncResult]
    title: str = ""
    temporary_title: str | None = None
    replaced_recipe_id: str | None = None
    replacement_results: list[AccountSyncResult] = field(default_factory=list)
    rename_results: list[AccountSyncResult] = field(default_factory=list)


@dataclass(frozen=True)
class CustomFoodCreateResult:
    """Verified account-specific IDs for one group-wide personal product."""

    run_id: str
    title: str
    food_ids: dict[str, str]
    reused_accounts: tuple[str, ...] = ()


def _recipe_list_request_fingerprint(
    title: str,
    portions: Decimal,
    steps: list[str],
    items: list[ResolvedRecipeListItem],
) -> str:
    """Return a stable semantic key that excludes generated row IDs and timestamps."""
    payload = {
        "title": normalize_title(title),
        "portions": str(portions.normalize()),
        "steps": [step.strip() for step in steps if step.strip()],
        "items": [
            {
                "requested_query": normalize_title(item.requested_query),
                "grams": str(item.grams.normalize()),
                "food_id": item.ingredient.food_id,
                "title": normalize_title(item.ingredient.title),
                "portion_id": item.ingredient.portion_id or "0",
                "amount": str(item.ingredient.amount.normalize()),
                "portion_description": item.ingredient.portion_description,
                "ingredient_grams": (
                    str(item.ingredient.grams.normalize()) if item.ingredient.grams is not None else None
                ),
                "custom_food_ids": dict(sorted(item.custom_food_ids.items())),
            }
            for item in items
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _recipe_list_payload_json(
    recipe: Recipe,
    requested_queries: list[str],
    custom_food_ids: dict[str, dict[str, str]] | None = None,
) -> str:
    """Serialize the exact candidate used by a recoverable recipe-list operation."""
    payload = {
        "recipe": {
            "id": recipe.id,
            "title": recipe.title,
            "description": recipe.description,
            "portions": str(recipe.portions),
            "prep_time": recipe.prep_time,
            "cook_time": recipe.cook_time,
            "steps": list(recipe.steps),
            "group_id": recipe.group_id,
            "ingredients": [
                {
                    "id": ingredient.id,
                    "food_id": ingredient.food_id,
                    "title": ingredient.title,
                    "portion_id": ingredient.portion_id,
                    "amount": str(ingredient.amount),
                    "portion_description": ingredient.portion_description,
                    "grams": str(ingredient.grams) if ingredient.grams is not None else None,
                }
                for ingredient in recipe.ingredients
            ],
        },
        "requested_queries": list(requested_queries),
        "custom_food_ids": custom_food_ids or {},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _recipe_list_payload(
    payload_json: str,
) -> tuple[Recipe, list[str], dict[str, dict[str, str]]]:
    """Restore an exact recipe-list candidate from its durable journal payload."""
    payload = json.loads(payload_json)
    recipe_data = payload["recipe"]
    recipe = Recipe(
        id=str(recipe_data["id"]),
        title=str(recipe_data["title"]),
        description=str(recipe_data.get("description") or ""),
        portions=Decimal(str(recipe_data.get("portions") or "1")),
        prep_time=int(recipe_data.get("prep_time") or 0),
        cook_time=int(recipe_data.get("cook_time") or 0),
        steps=[str(step) for step in recipe_data.get("steps") or []],
        group_id=(str(recipe_data["group_id"]) if recipe_data.get("group_id") is not None else None),
    )
    for ingredient_data in recipe_data.get("ingredients") or []:
        recipe.ingredients.append(
            Ingredient(
                id=str(ingredient_data["id"]),
                recipe_id=recipe.id,
                food_id=str(ingredient_data["food_id"]),
                title=str(ingredient_data["title"]),
                portion_id=str(ingredient_data.get("portion_id") or "0"),
                amount=Decimal(str(ingredient_data.get("amount") or "0")),
                portion_description=str(ingredient_data.get("portion_description") or ""),
                grams=(
                    Decimal(str(ingredient_data["grams"]))
                    if ingredient_data.get("grams") is not None
                    else None
                ),
            )
        )
    custom_food_ids: dict[str, dict[str, str]] = {}
    raw_custom_food_ids = payload.get("custom_food_ids") or {}
    if isinstance(raw_custom_food_ids, dict):
        for ingredient_id, mappings in raw_custom_food_ids.items():
            if isinstance(mappings, dict):
                custom_food_ids[str(ingredient_id)] = {
                    str(account_key): str(food_id)
                    for account_key, food_id in mappings.items()
                    if str(account_key) and str(food_id)
                }
    return recipe, [str(query) for query in payload.get("requested_queries") or []], custom_food_ids


def _inclusive_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def _food_diary_entry_to_dict(entry: FoodDiaryEntry) -> dict[str, object]:
    return {
        "entry_id": entry.entry_id,
        "recipe_id": entry.recipe_id,
        "meal": entry.meal,
        "name": entry.name,
        "recipe_source": entry.recipe_source,
        "recipe_portion_id": entry.recipe_portion_id,
        "portion_amount": str(entry.portion_amount),
        "serving_description": entry.serving_description,
    }


def _food_diary_entry_from_dict(data: dict[str, object]) -> FoodDiaryEntry:
    return FoodDiaryEntry(
        entry_id=str(data.get("entry_id") or ""),
        recipe_id=str(data.get("recipe_id") or ""),
        meal=int(data.get("meal") or 0),
        name=str(data.get("name") or ""),
        recipe_source=str(data.get("recipe_source") or ""),
        recipe_portion_id=str(data.get("recipe_portion_id") or "0"),
        portion_amount=Decimal(str(data.get("portion_amount") or "0")),
        serving_description=str(data.get("serving_description") or ""),
    )


def _diary_copy_date_result_to_dict(result: DiaryCopyDateResult) -> dict[str, object]:
    return {
        "account_key": result.account_key,
        "date": result.date.isoformat(),
        "inserted": result.inserted,
        "failed": result.failed,
        "message": result.message,
    }


def _diary_copy_result_from_dict(run_id: str, status: str, data: dict[str, object]) -> DiaryCopyResult:
    raw_dates = data.get("dates")
    dates = (
        [
            DiaryCopyDateResult(
                account_key=str(item.get("account_key") or ""),
                date=dt.date.fromisoformat(str(item.get("date"))),
                inserted=int(item.get("inserted") or 0),
                failed=int(item.get("failed") or 0),
                message=str(item.get("message") or ""),
            )
            for item in raw_dates
            if isinstance(item, dict)
        ]
        if isinstance(raw_dates, list)
        else []
    )
    return DiaryCopyResult(run_id=run_id, status=status, dates=dates)


def _same_decimal(left: Decimal, right: Decimal) -> bool:
    return left.quantize(Decimal("0.001")) == right.quantize(Decimal("0.001"))


def _ingredient_identity(ingredient: Ingredient) -> str:
    return ingredient.remote_ingredient_id or ingredient.id


def _find_matching_ingredient(
    target_ingredients: list[Ingredient],
    source: Ingredient,
    used_target_ids: set[str],
) -> Ingredient | None:
    for target in target_ingredients:
        target_id = _ingredient_identity(target)
        if target_id in used_target_ids:
            continue
        if target.food_id and target.food_id == source.food_id:
            return target
    source_title = source.title.casefold()
    for target in target_ingredients:
        target_id = _ingredient_identity(target)
        if target_id in used_target_ids:
            continue
        if target.title.casefold() == source_title:
            return target
    return None


def _ingredient_needs_update(target: Ingredient, source: Ingredient) -> bool:
    return (
        target.food_id != source.food_id
        or target.title != source.title
        or (target.portion_id or "0") != (source.portion_id or "0")
        or not _same_decimal(target.amount, source.amount)
    )


def _search_tokens(value: str) -> list[str]:
    return [token.replace("ё", "е") for token in SEARCH_TOKEN_RE.findall(value.casefold())]


def _token_matches(query_token: str, candidate_token: str) -> bool:
    if query_token == candidate_token:
        return True
    if query_token.isdigit() or candidate_token.isdigit():
        return False
    if len(query_token) <= 3 or len(candidate_token) <= 3:
        return False
    if candidate_token.startswith(query_token) and len(candidate_token) - len(query_token) <= 2:
        return True
    if query_token.startswith(candidate_token) and len(query_token) - len(candidate_token) <= 2:
        return True
    return len(query_token) >= 5 and len(candidate_token) >= 5 and query_token[:5] == candidate_token[:5]


def _optional_query_token(query_token: str, candidate_tokens: list[str]) -> bool:
    if query_token.startswith("курин") and any(token.startswith("яйц") for token in candidate_tokens):
        return True
    return False


def _missing_search_tokens(query: str, search_text: str) -> list[str]:
    candidate_tokens = _search_tokens(search_text)
    missing: list[str] = []
    for query_token in _search_tokens(query):
        if _optional_query_token(query_token, candidate_tokens):
            continue
        if not any(_token_matches(query_token, candidate_token) for candidate_token in candidate_tokens):
            missing.append(query_token)
    return missing


def _matches_requested_food(query: str, title: str, search_text: str = "") -> bool:
    text = " ".join([title, search_text])
    return not _missing_search_tokens(query, text)


def _title_has_extra_meaningful_tokens(query: str, title: str) -> bool:
    query_tokens = _search_tokens(query)
    for title_token in _search_tokens(title):
        if len(title_token) <= 2:
            continue
        if not any(_token_matches(query_token, title_token) for query_token in query_tokens):
            return True
    return False


def _rank_text(query: str, title: str, search_text: str) -> tuple[int, int, int, int, int, int, int, int, str]:
    normalized_query = normalize_title(query)
    normalized_title = normalize_title(title)
    terms = normalized_query.split()
    words = set(normalized_title.split())
    all_terms_as_words = all(term in words for term in terms)
    all_terms_present = all(term in normalized_title for term in terms)
    missing_terms = len(_missing_search_tokens(query, search_text))
    title_missing_terms = len(_missing_search_tokens(query, title))
    extra_title_words = len(words - set(terms)) if all_terms_as_words else len(words)
    return (
        missing_terms,
        0 if normalized_title == normalized_query else 1,
        title_missing_terms,
        extra_title_words,
        0 if all_terms_present else 1,
        0 if all_terms_as_words else 1,
        len(normalized_title.split()),
        len(normalized_title),
        normalized_title,
    )


def _food_search_text(result: FoodSearchResult) -> str:
    raw_values: list[str] = []
    for value in result.raw.values():
        if isinstance(value, (str, int, float, Decimal)):
            raw_values.append(str(value))
        elif isinstance(value, dict):
            raw_values.extend(str(item) for item in value.values() if isinstance(item, (str, int, float, Decimal)))
    return " ".join([result.title, result.brand, result.description, *raw_values])


def _resolved_search_text(item: ResolvedRecipeListItem) -> str:
    return " ".join([item.ingredient.title, item.brand])


def _unrequested_brand_priority(query: str, brand: str) -> int:
    if not brand.strip():
        return 0
    return 0 if not _missing_search_tokens(brand, query) else 1


def _food_result_rank(query: str, result: FoodSearchResult) -> tuple[int | str, ...]:
    missing_terms, exact_title, title_missing_terms, extra_title_words, all_terms_present, all_terms_as_words, title_words, title_length, normalized_title = _rank_text(
        query,
        result.title,
        _food_search_text(result),
    )
    own_priority = 0 if result.is_own else 1
    brand_priority = _unrequested_brand_priority(query, result.brand)
    if len(_search_tokens(query)) <= 1:
        return (
            missing_terms,
            title_missing_terms,
            own_priority,
            brand_priority,
            exact_title,
            extra_title_words,
            all_terms_present,
            all_terms_as_words,
            title_words,
            title_length,
            normalized_title,
        )
    return (
        missing_terms,
        title_missing_terms,
        extra_title_words,
        brand_priority,
        exact_title,
        own_priority,
        all_terms_present,
        all_terms_as_words,
        title_words,
        title_length,
        normalized_title,
    )


def _matches_direct_food_metadata(result: FoodSearchResult, direct_metadata: FoodSearchResult | None) -> bool:
    if direct_metadata is None or not direct_metadata.brand:
        return True
    return _matches_requested_food(direct_metadata.brand, result.title, _food_search_text(result))


def _food_result_has_detail(result: FoodSearchResult) -> bool:
    has_macros = _food_result_has_macros(result)
    return has_macros and _food_result_has_usable_gram_portion(result)


def _food_result_has_macros(result: FoodSearchResult) -> bool:
    return result.raw.get("_source_endpoint") == "food_search_data" or any(
        value is not None
        for value in (
            result.energy_per_portion,
            result.protein_per_portion,
            result.fat_per_portion,
            result.carbohydrate_per_portion,
        )
    )


def _resolved_candidate_rank(
    query: str,
    item: ResolvedRecipeListItem,
) -> tuple[int | str, ...]:
    missing_terms, exact_title, title_missing_terms, extra_title_words, all_terms_present, all_terms_as_words, title_words, title_length, normalized_title = _rank_text(
        query,
        item.ingredient.title,
        _resolved_search_text(item),
    )
    source_priority = 0 if item.source == "часто использовался" else 1
    brand_priority = _unrequested_brand_priority(query, item.brand)
    if len(_search_tokens(query)) <= 1:
        return (
            missing_terms,
            title_missing_terms,
            source_priority,
            -item.usage_count,
            brand_priority,
            exact_title,
            extra_title_words,
            all_terms_present,
            all_terms_as_words,
            title_words,
            title_length,
            normalized_title,
        )
    return (
        missing_terms,
        title_missing_terms,
        extra_title_words,
        source_priority,
        -item.usage_count,
        brand_priority,
        exact_title,
        all_terms_present,
        all_terms_as_words,
        title_words,
        title_length,
        normalized_title,
    )


def _query_variants(query: str) -> list[str]:
    normalized = query.strip()
    terms = normalized.split()
    variants: list[str] = []
    for candidate in [
        normalized,
        " ".join(terms[1:]) if len(terms) > 2 else "",
        " ".join(terms[-2:]) if len(terms) > 1 else "",
        *(term for term in terms if len(term) > 2),
    ]:
        candidate = candidate.strip()
        if candidate and candidate.casefold() not in {item.casefold() for item in variants}:
            variants.append(candidate)
    return variants[:4] or [normalized]


def _dedupe_food_results(results: list[FoodSearchResult]) -> list[FoodSearchResult]:
    deduped: list[FoodSearchResult] = []
    seen: set[tuple[str, str, str]] = set()
    for item in results:
        key = (item.food_id, normalize_title(item.title), normalize_title(item.brand or item.description))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _macro_energy(
    protein: Decimal | None,
    fat: Decimal | None,
    carbohydrate: Decimal | None,
) -> Decimal | None:
    if protein is None or fat is None or carbohydrate is None:
        return None
    return protein * Decimal("4") + fat * Decimal("9") + carbohydrate * Decimal("4")


def _correct_energy(
    energy: Decimal | None,
    protein: Decimal | None,
    fat: Decimal | None,
    carbohydrate: Decimal | None,
) -> Decimal | None:
    calculated = _macro_energy(protein, fat, carbohydrate)
    if energy is None:
        return calculated
    if calculated is not None and calculated > 0 and energy < calculated * Decimal("0.5"):
        return calculated
    return energy


def _macro_field_count(item: ResolvedRecipeListItem) -> int:
    return sum(
        value is not None
        for value in (
            item.energy_per_100g,
            item.protein_per_100g,
            item.fat_per_100g,
            item.carbohydrate_per_100g,
        )
    )


def _sync_description(now: dt.datetime | None = None, timezone: str = "Europe/Minsk") -> str:
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        tz = dt.datetime.now().astimezone().tzinfo
    value = now or dt.datetime.now(tz)
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    value = value.astimezone(tz)
    return f"Последняя синхронизация: {value:%d.%m.%Y %H:%M}"


def _amount_for_grams(grams: Decimal, portion_description: str) -> Decimal:
    unit_size = portion_unit_size(portion_description)
    if unit_size is None or unit_size == 0:
        return grams
    return grams / unit_size


def _gram_portion_amount(grams: Decimal) -> Decimal:
    return _amount_for_grams(grams, "100г")


def _ingredient_grams(ingredient: Ingredient) -> Decimal:
    value = _ingredient_grams_or_none(ingredient)
    return value if value is not None else ingredient.amount


def _ingredient_grams_or_none(ingredient: Ingredient) -> Decimal | None:
    if ingredient.grams is not None:
        return ingredient.grams
    return grams_from_portion(ingredient.amount, ingredient.portion_description)


def _food_result_portion_grams(result: FoodSearchResult) -> Decimal | None:
    if result.grams_per_portion is not None and result.grams_per_portion > 0:
        return result.grams_per_portion
    unit_size = portion_unit_size(result.default_portion_description)
    if unit_size is not None and unit_size > 0:
        return unit_size
    return None


def _ingredient_current_portion_sends_grams(ingredient: Ingredient, grams: Decimal | None) -> bool:
    if grams is None:
        return False
    portion_id = ingredient.portion_id or "0"
    if portion_id == "0":
        return False
    if not is_explicit_weight_portion(ingredient.portion_description):
        return False
    return _same_decimal(
        ingredient.amount,
        _amount_for_grams(grams, ingredient.portion_description),
    )


def _food_result_has_usable_gram_portion(result: FoodSearchResult) -> bool:
    if result.raw.get("_gram_portion_id"):
        return True
    if result.raw.get("_source_endpoint") == "food_search_data":
        return False
    return (result.default_portion_id or "0") != "0" and is_explicit_weight_portion(
        result.default_portion_description
    )


def _ingredient_from_food_result(
    result: FoodSearchResult,
    grams: Decimal,
    *,
    id: str | None = None,
    recipe_id: str = "",
    food_id: str | None = None,
    title: str | None = None,
    remote_ingredient_id: str | None = None,
) -> Ingredient:
    gram_portion_id = str(result.raw.get("_gram_portion_id") or "").strip()
    if gram_portion_id:
        gram_portion_description = str(result.raw.get("_gram_portion_description") or "г").strip()
        return Ingredient(
            id=id or str(uuid.uuid4()),
            recipe_id=recipe_id,
            food_id=food_id or result.food_id,
            title=title or result.title,
            portion_id=gram_portion_id,
            amount=_amount_for_grams(grams, gram_portion_description),
            portion_description=gram_portion_description,
            remote_ingredient_id=remote_ingredient_id,
            grams=grams,
        )
    portion_id = result.default_portion_id or "0"
    portion_description = result.default_portion_description or ""
    if portion_id != "0" and is_explicit_weight_portion(portion_description):
        return Ingredient(
            id=id or str(uuid.uuid4()),
            recipe_id=recipe_id,
            food_id=food_id or result.food_id,
            title=title or result.title,
            portion_id=portion_id,
            amount=_amount_for_grams(grams, portion_description),
            portion_description=portion_description,
            remote_ingredient_id=remote_ingredient_id,
            grams=grams,
        )
    return Ingredient(
        id=id or str(uuid.uuid4()),
        recipe_id=recipe_id,
        food_id=food_id or result.food_id,
        title=title or result.title,
        portion_id="0",
        amount=_gram_portion_amount(grams),
        portion_description="100г",
        remote_ingredient_id=remote_ingredient_id,
        grams=grams,
    )


def _copy_remote_ingredients(recipe_id: str, ingredients: list[Ingredient]) -> list[Ingredient]:
    return [
        Ingredient(
            id=f"{recipe_id}:{item.remote_ingredient_id or item.id}",
            recipe_id=recipe_id,
            food_id=item.food_id,
            title=item.title,
            portion_id=item.portion_id,
            amount=item.amount,
            portion_description=item.portion_description,
            remote_ingredient_id=item.remote_ingredient_id,
            grams=item.grams,
        )
        for item in ingredients
    ]


def _copy_recipe_from_remote(recipe_id: str, remote: Recipe) -> Recipe:
    recipe = Recipe(
        id=recipe_id,
        title=remote.title,
        description=remote.description,
        portions=remote.portions,
        prep_time=remote.prep_time,
        cook_time=remote.cook_time,
        steps=list(remote.steps),
        default_portion_id=remote.default_portion_id,
        default_portion_description=remote.default_portion_description,
    )
    recipe.ingredients = _copy_remote_ingredients(recipe_id, remote.ingredients)
    return recipe


def _remote_ids_for_account(recipe: Recipe, account_key: str) -> list[str]:
    remote_ids = list(recipe.remote_ids_by_account.get(account_key) or [])
    primary = recipe.remote_ids.get(account_key)
    if primary and primary not in remote_ids:
        remote_ids.insert(0, primary)
    return [remote_id for remote_id in remote_ids if remote_id]


def _remember_remote_recipe_id(recipe: Recipe, account_key: str, remote_id: str) -> None:
    recipe.remote_ids[account_key] = remote_id
    remote_ids = recipe.remote_ids_by_account.setdefault(account_key, [])
    if remote_id not in remote_ids:
        remote_ids.append(remote_id)


def _forget_remote_recipe_id(recipe: Recipe, account_key: str, remote_id: str) -> None:
    if recipe.remote_ids.get(account_key) == remote_id:
        recipe.remote_ids.pop(account_key, None)
    remote_ids = [item for item in recipe.remote_ids_by_account.get(account_key, []) if item != remote_id]
    if remote_ids:
        recipe.remote_ids_by_account[account_key] = remote_ids
    else:
        recipe.remote_ids_by_account.pop(account_key, None)


def _ingredient_with_search_result(ingredient: Ingredient, result: FoodSearchResult) -> Ingredient:
    return _ingredient_from_food_result(
        result,
        _ingredient_grams(ingredient),
        id=ingredient.id,
        recipe_id=ingredient.recipe_id,
        food_id=result.food_id or ingredient.food_id,
        title=result.title or ingredient.title,
        remote_ingredient_id=ingredient.remote_ingredient_id,
    )


def _ingredient_with_food_id(ingredient: Ingredient, food_id: str) -> Ingredient:
    return Ingredient(
        id=ingredient.id,
        recipe_id=ingredient.recipe_id,
        food_id=food_id,
        title=ingredient.title,
        portion_id=ingredient.portion_id,
        amount=ingredient.amount,
        portion_description=ingredient.portion_description,
        remote_ingredient_id=ingredient.remote_ingredient_id,
        grams=ingredient.grams,
    )


def _ingredient_for_target_create(ingredient: Ingredient) -> Ingredient:
    return Ingredient(
        id=ingredient.id,
        recipe_id=ingredient.recipe_id,
        food_id=ingredient.food_id,
        title=ingredient.title,
        portion_id=ingredient.portion_id,
        amount=ingredient.amount,
        portion_description=ingredient.portion_description,
        remote_ingredient_id=None,
        grams=ingredient.grams,
    )


def _recipe_ingredient_membership_differences(expected: Recipe, actual: Recipe) -> list[str]:
    """Report missing or unexpected ingredient identities without comparing transport formatting."""
    expected_counts = Counter(ingredient.food_id for ingredient in expected.ingredients)
    actual_counts = Counter(ingredient.food_id for ingredient in actual.ingredients)
    expected_titles = {ingredient.food_id: ingredient.title for ingredient in expected.ingredients}
    actual_titles = {ingredient.food_id: ingredient.title for ingredient in actual.ingredients}
    differences: list[str] = []
    for food_id, count in sorted((expected_counts - actual_counts).items()):
        title = expected_titles.get(food_id) or food_id
        differences.append(f"не добавлен продукт «{title}» ({count} шт.)")
    for food_id, count in sorted((actual_counts - expected_counts).items()):
        title = actual_titles.get(food_id) or food_id
        differences.append(f"добавлен лишний продукт «{title}» ({count} шт.)")
    return differences


def _custom_food_content_hash(definition: CustomFoodDefinition) -> str:
    """Fingerprint fields that can be verified through authoritative food readback."""
    return hashlib.sha256(
        json.dumps(
            {
                "title": normalize_title(definition.title),
                "manufacturer": normalize_title(definition.manufacturer_name),
                "serving_type": definition.serving_type,
                "serving_size": definition.serving_size,
                "metric_serving_size": definition.metric_serving_size,
                "nutrients": {
                    key: format(value.normalize(), "f")
                    for key, value in sorted(definition.nutrients.items())
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _custom_food_definition_json(definition: CustomFoodDefinition) -> str:
    return json.dumps(
        {
            "source_recipe_id": definition.source_recipe_id,
            "title": definition.title,
            "manufacturer_name": definition.manufacturer_name,
            "serving_type": definition.serving_type,
            "serving_size": definition.serving_size,
            "metric_serving_size": definition.metric_serving_size,
            "nutrients": {key: str(value) for key, value in sorted(definition.nutrients.items())},
            "barcode": definition.barcode,
            "barcode_type": definition.barcode_type,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _custom_food_definition(payload_json: str) -> CustomFoodDefinition:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("invalid custom-food journal payload")
    nutrients = payload.get("nutrients") or {}
    if not isinstance(nutrients, dict):
        raise ValueError("invalid custom-food journal nutrients")
    return CustomFoodDefinition(
        source_recipe_id=str(payload.get("source_recipe_id") or ""),
        title=str(payload.get("title") or ""),
        manufacturer_name=str(payload.get("manufacturer_name") or ""),
        serving_type=str(payload.get("serving_type") or "Per100g"),
        serving_size=str(payload.get("serving_size") or "100"),
        metric_serving_size=str(payload.get("metric_serving_size") or "100g"),
        nutrients={str(key): Decimal(str(value)) for key, value in nutrients.items()},
        barcode=str(payload.get("barcode") or ""),
        barcode_type=str(payload.get("barcode_type") or ""),
    )


def _custom_food_request_fingerprint(definition: CustomFoodDefinition) -> str:
    return hashlib.sha256(_custom_food_definition_json(definition).encode("utf-8")).hexdigest()


def _custom_food_definition_matches(
    expected: CustomFoodDefinition,
    actual: CustomFoodDefinition,
) -> bool:
    return _custom_food_content_hash(expected) == _custom_food_content_hash(actual)


def _recipe_for_account(
    recipe: Recipe,
    account_key: str,
    custom_food_ids: dict[str, dict[str, str]],
) -> Recipe:
    """Return an account-specific recipe with every personal product ID replaced."""
    account_recipe = _copy_recipe_from_remote(recipe.id, recipe)
    account_recipe.group_id = recipe.group_id
    account_recipe.title = recipe.title
    account_recipe.description = recipe.description
    account_recipe.portions = recipe.portions
    account_recipe.prep_time = recipe.prep_time
    account_recipe.cook_time = recipe.cook_time
    account_recipe.steps = list(recipe.steps)
    account_recipe.ingredients = []
    for ingredient in recipe.ingredients:
        mappings = custom_food_ids.get(ingredient.id)
        if mappings is None:
            account_recipe.ingredients.append(_ingredient_for_target_create(ingredient))
            continue
        food_id = mappings.get(account_key)
        if not food_id:
            raise FatSecretError(
                f"Для личного продукта «{ingredient.title}» потеряна привязка аккаунта {account_key}."
            )
        account_recipe.ingredients.append(
            _ingredient_with_food_id(_ingredient_for_target_create(ingredient), food_id)
        )
    return account_recipe


def _is_ambiguous_mutation_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, FatSecretActionError) and 500 <= exc.status_code < 600


class RecipeSyncEngine:
    def __init__(self, storage: Storage, device: FatSecretDeviceConfig, timezone: str = "Europe/Minsk") -> None:
        self.storage = storage
        self.device = device
        self.timezone = timezone

    async def close(self) -> None:
        return None

    def _current_date(self) -> dt.date:
        """Return today's date in the configured timezone for generic FatSecret fields."""
        try:
            timezone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            timezone = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
        return dt.datetime.now(timezone).date()

    def _build_clients(self, group_id: str | None = None) -> dict[str, FatSecretClient]:
        accounts = self.storage.list_fatsecret_accounts(group_id)
        if not accounts:
            raise FatSecretError("Сначала подключи хотя бы один FatSecret аккаунт через кнопку «Аккаунты».")
        return {account.key: self._build_client(account) for account in accounts}

    def _build_client(self, account: FatSecretAccountConfig) -> FatSecretClient:
        return FatSecretClient(
            account,
            self.device,
            session=self.storage.get_fatsecret_session(account.key),
            session_saver=lambda session, account_key=account.key: self.storage.update_fatsecret_session(
                account_key,
                session,
            ),
            today_provider=self._current_date,
        )

    async def _close_clients(self, clients: dict[str, FatSecretClient]) -> None:
        for client in clients.values():
            await client.close()

    async def _load_recipe_source_snapshot(
        self,
        client: FatSecretClient,
        remote_id: str,
        local_id: str,
    ) -> _RecipeSourceSnapshot:
        remote = await client.get_recipe(remote_id)
        transport = _copy_recipe_from_remote(local_id, remote)
        normalized_ingredients = await self._normalize_recipe_ingredients(client, remote.ingredients)
        display = _copy_recipe_from_remote(local_id, remote)
        display.ingredients = _copy_remote_ingredients(local_id, normalized_ingredients)
        return _RecipeSourceSnapshot(transport=transport, display=display)

    async def validate_account(self, account: FatSecretAccountConfig) -> None:
        """Verify FatSecret credentials by performing a real mobile API login."""
        client = self._build_client(account)
        try:
            await client.login()
        finally:
            await client.close()

    async def prepare_diary_copy(
        self,
        group_id: str,
        initiated_by: int,
        source_account_key: str,
        source_date: dt.date,
        target_start: dt.date,
        target_end: dt.date,
        target_account_keys: list[str] | None = None,
    ) -> DiaryCopyPreview:
        """Load the source diary and persist an immutable copy preview."""
        if target_end < target_start:
            raise FatSecretError("Конечная дата не может быть раньше начальной.")
        days = (target_end - target_start).days + 1
        if days > MAX_DIARY_COPY_DAYS:
            raise FatSecretError(f"За один раз можно выбрать не больше {MAX_DIARY_COPY_DAYS} дней.")
        accounts = self.storage.list_fatsecret_accounts(group_id)
        if len(accounts) < 2:
            raise FatSecretError("Для копирования дневника подключи минимум два FatSecret аккаунта группы.")
        account_by_key = {account.key: account for account in accounts}
        source_account = account_by_key.get(source_account_key)
        if source_account is None:
            raise FatSecretError("Аккаунт-источник больше не подключен к активной группе.")
        target_keys = list(dict.fromkeys(target_account_keys or account_by_key))
        if not target_keys or any(key not in account_by_key for key in target_keys):
            raise FatSecretError("Выбери минимум один актуальный целевой FatSecret аккаунт.")

        client = self._build_client(source_account)
        try:
            source_day = await client.get_food_diary_day(source_date)
        finally:
            await client.close()
        if not source_day.entries:
            raise FatSecretError("В выбранный день у аккаунта-источника нет записей еды.")

        request = {
            "target_account_keys": target_keys,
            "entries": [_food_diary_entry_to_dict(entry) for entry in source_day.entries],
        }
        run_id = self.storage.create_diary_copy_run(
            group_id,
            initiated_by,
            source_account_key,
            source_date,
            target_start,
            target_end,
            request,
        )
        skipped_source_day = source_account_key in target_keys and target_start <= source_date <= target_end
        operations = len(target_keys) * days - int(skipped_source_day)
        return DiaryCopyPreview(
            run_id=run_id,
            source_account_key=source_account_key,
            source_date=source_date,
            target_start=target_start,
            target_end=target_end,
            source_entries=source_day.entries,
            target_operations=operations,
            skipped_source_day=skipped_source_day,
        )

    async def execute_diary_copy(self, run_id: str) -> DiaryCopyResult:
        """Execute one confirmed diary copy once and persist its terminal result."""
        run = self.storage.diary_copy_run(run_id)
        if run is None:
            raise FatSecretError("Операция копирования устарела или не найдена.")
        if run["status"] in {"completed", "partial", "failed"}:
            result = run.get("result")
            if isinstance(result, dict):
                return _diary_copy_result_from_dict(run_id, str(run["status"]), result)
            raise FatSecretError("Сохраненный результат операции поврежден.")

        request = run.get("request")
        if not isinstance(request, dict):
            raise FatSecretError("Параметры операции повреждены.")
        entries_raw = request.get("entries")
        if not isinstance(entries_raw, list):
            raise FatSecretError("Список еды операции поврежден.")
        entries = [_food_diary_entry_from_dict(item) for item in entries_raw if isinstance(item, dict)]
        target_account_keys = [
            str(value)
            for value in request.get("target_account_keys", request.get("account_keys", []))
        ]
        source_account_key = str(run["source_account_key"])
        source_date = dt.date.fromisoformat(str(run["source_date"]))
        target_start = dt.date.fromisoformat(str(run["target_start"]))
        target_end = dt.date.fromisoformat(str(run["target_end"]))
        if not self.storage.claim_diary_copy_run(run_id):
            raise FatSecretError("Эта операция уже выполняется. Повторное нажатие ничего не добавит.")

        clients: dict[str, FatSecretClient] = {}
        date_results: list[DiaryCopyDateResult] = []
        mapping_cache: dict[tuple[str, str], tuple[str, str]] = {}
        try:
            clients = self._build_clients(str(run["group_id"]))
            if not target_account_keys or not set(target_account_keys).issubset(clients):
                raise FatSecretError("Состав FatSecret аккаунтов изменился после подтверждения. Начни заново.")
            source_client = clients.get(source_account_key)
            if source_client is None:
                raise FatSecretError("Аккаунт-источник больше не подключен.")
            for target_account_key in target_account_keys:
                target_client = clients[target_account_key]
                for target_date in _inclusive_dates(target_start, target_end):
                    if target_account_key == source_account_key and target_date == source_date:
                        continue
                    self.storage.touch_diary_copy_run(run_id)
                    try:
                        writes: list[FoodDiaryWriteEntry] = []
                        for index, entry in enumerate(entries):
                            self.storage.touch_diary_copy_run(run_id)
                            mapped_recipe_id, mapped_portion_id = await self._map_diary_entry(
                                source_account_key,
                                target_account_key,
                                entry,
                                source_client,
                                target_client,
                                mapping_cache,
                            )
                            writes.append(
                                FoodDiaryWriteEntry(
                                    reference=f"{run_id[:16]}-{target_account_key}-{target_date:%Y%m%d}-{index}",
                                    recipe_id=mapped_recipe_id,
                                    name=entry.name,
                                    recipe_portion_id=mapped_portion_id,
                                    portion_amount=entry.portion_amount,
                                    meal=entry.meal,
                                    serving_description=entry.serving_description,
                                )
                            )
                        response = await target_client.bulk_update_food_diary(target_date, writes)
                        expected_references = {write.reference for write in writes}
                        inserted_references = expected_references.intersection(response.inserted_entries)
                        inserted = len(inserted_references)
                        failed = len(writes) - inserted
                        explicit_failures = {
                            reference: message
                            for reference, message in response.failed_entries.items()
                            if reference in expected_references and reference not in inserted_references
                        }
                        failure_messages = sorted(set(explicit_failures.values()))
                        unconfirmed = failed - len(explicit_failures)
                        if unconfirmed > 0:
                            failure_messages.append(f"FatSecret не подтвердил записей: {unconfirmed}")
                        message = "добавлено" if not failed else "; ".join(failure_messages)
                        date_results.append(
                            DiaryCopyDateResult(
                                account_key=target_account_key,
                                date=target_date,
                                inserted=inserted,
                                failed=failed,
                                message=message,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - report and continue other dates/accounts.
                        logger.exception("diary copy failed for %s on %s", target_account_key, target_date)
                        date_results.append(
                            DiaryCopyDateResult(
                                account_key=target_account_key,
                                date=target_date,
                                inserted=0,
                                failed=len(entries),
                                message=user_safe_error_message(exc),
                            )
                        )
                    finally:
                        self.storage.touch_diary_copy_run(run_id)
        except Exception as exc:  # noqa: BLE001 - persist a terminal idempotency result.
            result_data = {
                "dates": [_diary_copy_date_result_to_dict(item) for item in date_results],
                "error": user_safe_error_message(exc),
            }
            self.storage.finish_diary_copy_run(run_id, "failed", result_data)
            raise
        finally:
            if clients:
                await self._close_clients(clients)

        failed_count = sum(item.failed for item in date_results)
        status = "completed" if failed_count == 0 else "partial"
        result_data = {"dates": [_diary_copy_date_result_to_dict(item) for item in date_results]}
        self.storage.finish_diary_copy_run(run_id, status, result_data)
        return DiaryCopyResult(run_id=run_id, status=status, dates=date_results)

    async def _map_diary_entry(
        self,
        source_account_key: str,
        target_account_key: str,
        entry: FoodDiaryEntry,
        source_client: FatSecretClient,
        target_client: FatSecretClient,
        cache: dict[tuple[str, str], tuple[str, str]],
    ) -> tuple[str, str]:
        cache_key = (target_account_key, entry.recipe_id)
        if cache_key in cache:
            return cache[cache_key]
        if target_account_key == source_account_key:
            mapped = (entry.recipe_id, entry.recipe_portion_id)
            cache[cache_key] = mapped
            return mapped

        if entry.recipe_source.casefold() == "facebook" or entry.recipe_portion_id == "-1":
            target_food_id = self.storage.custom_food_mapping(
                source_account_key,
                entry.recipe_id,
                target_account_key,
            )
            if target_food_id is None:
                definition = await source_client.get_custom_food_definition(entry.recipe_id)
                target_food_id = await target_client.create_custom_food(definition)
                self.storage.set_custom_food_mapping(
                    source_account_key,
                    entry.recipe_id,
                    target_account_key,
                    target_food_id,
                    _custom_food_content_hash(definition),
                )
            mapped = (target_food_id, "-1")
            cache[cache_key] = mapped
            return mapped

        local_recipe_id = self.storage.local_recipe_id_for_remote(source_account_key, entry.recipe_id)
        if local_recipe_id is None:
            mapped = (entry.recipe_id, entry.recipe_portion_id)
            cache[cache_key] = mapped
            return mapped
        recipe = self.storage.get_recipe(local_recipe_id)
        if recipe is None:
            raise FatSecretError(f"Локальная карточка рецепта {entry.name} потеряна.")
        target_recipe_id = recipe.remote_ids.get(target_account_key)
        if target_recipe_id is None:
            source_recipe = await source_client.get_recipe(entry.recipe_id)
            source_recipe.id = recipe.id
            source_recipe.group_id = recipe.group_id
            target_recipe_id = await target_client.create_recipe(source_recipe)
            try:
                await self._sync_ingredients(
                    target_client,
                    source_recipe,
                    target_recipe_id,
                    source_client=source_client,
                    source_account_key=source_account_key,
                    target_account_key=target_account_key,
                )
                if not await target_client.save_recipe_meta(source_recipe, target_recipe_id):
                    raise FatSecretError(f"{target_client.account.label}: recipe metadata save returned false")
            except Exception:
                await self._rollback_created_recipe_with_status(target_client, target_recipe_id)
                raise
            self.storage.mark_synced(recipe.id, target_account_key, target_recipe_id, recipe.version)
        target_recipe = await target_client.get_recipe(target_recipe_id)
        target_portion_id = target_recipe.default_portion_id or entry.recipe_portion_id
        mapped = (target_recipe_id, target_portion_id)
        cache[cache_key] = mapped
        return mapped

    async def refresh_account_recipes(self, account: FatSecretAccountConfig, group_id: str | None = None) -> int:
        """Import cookbook recipes for one connected FatSecret account."""
        client = self._build_client(account)
        imported = 0
        try:
            recipes = await client.cookbook()
            for summary in recipes:
                self.storage.import_remote_recipe(account.key, summary, group_id)
                self.storage.upsert_remote_recipe_summary(account.key, summary.remote_id, summary.title)
                imported += 1
            self.storage.reconcile_remote_recipe_snapshots(
                account.key,
                {summary.remote_id for summary in recipes},
            )
        finally:
            await client.close()
        return imported

    async def refresh_remote_recipes(self, group_id: str | None = None) -> int:
        imported = 0
        clients = self._build_clients(group_id)
        try:
            for account_key, client in clients.items():
                recipes = await client.cookbook()
                for summary in recipes:
                    self.storage.import_remote_recipe(account_key, summary, group_id)
                    imported += 1
        finally:
            await self._close_clients(clients)
        return imported

    async def refresh_food_usage_cache(self, group_id: str) -> int:
        """Refresh frequently used foods from live FatSecret recipe ingredients for one group."""
        clients = self._build_clients(group_id)
        ingredients: list[Ingredient] = []
        try:
            for account_key, client in clients.items():
                detail_tasks: dict[str, asyncio.Task[FoodSearchResult]] = {}
                summaries = await client.cookbook()
                for summary in summaries:
                    try:
                        recipe = await client.get_recipe(summary.remote_id)
                        recipe.ingredients = await self._normalize_recipe_ingredients(
                            client,
                            recipe.ingredients,
                            detail_tasks=detail_tasks,
                        )
                    except Exception:  # noqa: BLE001 - keep one broken recipe from poisoning the whole cache.
                        logger.debug(
                            "food usage cache recipe load failed for %s/%s",
                            account_key,
                            summary.remote_id,
                            exc_info=True,
                        )
                        continue
                    ingredients.extend(recipe.ingredients)
        finally:
            await self._close_clients(clients)
        return self.storage.replace_food_usage_cache(group_id, ingredients)

    async def refresh_food_usage_cache_for_all_groups(self) -> dict[str, int]:
        """Refresh frequently used foods for every group that has connected FatSecret accounts."""
        refreshed: dict[str, int] = {}
        for group_id in self.storage.list_group_ids():
            if self.storage.fatsecret_account_count(group_id) == 0:
                continue
            try:
                refreshed[group_id] = await self.refresh_food_usage_cache(group_id)
            except Exception:  # noqa: BLE001 - one group should not block other groups.
                logger.exception("food usage cache refresh failed for group %s", group_id)
        return refreshed

    async def ensure_food_usage_cache(self, group_id: str) -> None:
        """Refresh the FatSecret-derived food usage cache at most once per day."""
        if self.storage.food_usage_cache_is_fresh(group_id):
            return
        if self.storage.fatsecret_account_count(group_id) == 0:
            return
        await self.refresh_food_usage_cache(group_id)

    async def lookup_barcode(self, group_id: str, barcode: str) -> BarcodeLookupResult:
        """Look up a barcode with one connected account because mappings are global."""
        clients = self._build_clients(group_id)
        try:
            return await next(iter(clients.values())).lookup_barcode(barcode)
        finally:
            await self._close_clients(clients)

    async def barcode_recipe_list_item(
        self,
        group_id: str,
        lookup: BarcodeLookupResult,
        grams: Decimal,
        requested_query: str,
    ) -> ResolvedRecipeListItem:
        """Hydrate a known barcode mapping into a normal recipe-list ingredient."""
        if not lookup.food_id:
            raise FatSecretError("FatSecret пока не знает этот штрих-код.")
        clients = self._build_clients(group_id)
        try:
            client = next(iter(clients.values()))
            found = await client.resolve_food_detail(
                FoodSearchResult(
                    food_id=lookup.food_id,
                    title=lookup.food_name or requested_query,
                    brand=lookup.brand_name,
                )
            )
        finally:
            await self._close_clients(clients)
        protein = found.protein_per_portion
        fat = found.fat_per_portion
        carbohydrate = found.carbohydrate_per_portion
        return ResolvedRecipeListItem(
            requested_query=requested_query,
            grams=grams,
            ingredient=_ingredient_from_food_result(found, grams),
            source="штрих-код FatSecret",
            brand=found.brand or lookup.brand_name,
            energy_per_100g=_correct_energy(found.energy_per_portion, protein, fat, carbohydrate),
            protein_per_100g=protein,
            fat_per_100g=fat,
            carbohydrate_per_100g=carbohydrate,
        )

    @staticmethod
    def _validate_custom_food_definition(definition: CustomFoodDefinition) -> None:
        title = definition.title.strip()
        if not title:
            raise FatSecretError("Название продукта не должно быть пустым.")
        if len(title) > 200:
            raise FatSecretError("Название продукта слишком длинное.")
        if definition.serving_type != "Per100g":
            raise FatSecretError("Сейчас поддерживаются только значения на 100 г.")
        required = {
            "calories": Decimal("10000"),
            "protein": Decimal("1000"),
            "totalFat": Decimal("1000"),
            "carbohydrate": Decimal("1000"),
        }
        for name, maximum in required.items():
            value = definition.nutrients.get(name)
            if value is None or not value.is_finite() or value < 0 or value > maximum:
                raise FatSecretError("Ккал, белки, жиры и углеводы должны быть неотрицательными числами.")
        if definition.barcode and definition.barcode_type not in {
            "EAN_8",
            "EAN_13",
            "UPC_A",
            "UPC_E",
            "Other",
        }:
            raise FatSecretError("FatSecret не поддерживает этот тип штрих-кода.")

    async def _existing_custom_food_id(
        self,
        client: FatSecretClient,
        definition: CustomFoodDefinition,
    ) -> tuple[str | None, bool]:
        """Find an exact owned match and report whether the title is already conflicting."""
        exact_title = normalize_title(definition.title)
        candidates: list[FoodSearchResult] = []
        for page in range(3):
            page_items = await client.search_food(definition.title, page=page)
            candidates.extend(
                item
                for item in page_items
                if item.is_own and normalize_title(item.title) == exact_title and item.food_id
            )
            if not page_items:
                break
        matching_ids: list[str] = []
        title_conflict = False
        for candidate in _dedupe_food_results(candidates):
            try:
                actual = await client.get_custom_food_definition(candidate.food_id)
            except FatSecretNotCustomFoodError:
                continue
            if _custom_food_definition_matches(definition, actual):
                matching_ids.append(candidate.food_id)
            else:
                title_conflict = True
        if matching_ids:
            matching_ids.sort(key=lambda item: (not item.isdigit(), int(item) if item.isdigit() else item))
            return matching_ids[0], title_conflict
        return None, title_conflict

    async def create_custom_food_for_group(
        self,
        group_id: str,
        definition: CustomFoodDefinition,
        initiated_by: int,
    ) -> CustomFoodCreateResult:
        """Create and read back one personal product in every connected group account."""
        self._validate_custom_food_definition(definition)
        request_fingerprint = _custom_food_request_fingerprint(definition)
        content_hash = _custom_food_content_hash(definition)
        clients = self._build_clients(group_id)
        try:
            run = self.storage.matching_custom_food_run(group_id, request_fingerprint)
            if run is None:
                run_id = self.storage.create_custom_food_run(
                    group_id,
                    initiated_by,
                    definition.title.strip(),
                    _custom_food_definition_json(definition),
                    request_fingerprint,
                    content_hash,
                    list(clients),
                )
                run = self.storage.custom_food_run(run_id)
                if run is None:
                    raise FatSecretError("Не удалось сохранить журнал создания продукта.")
            else:
                definition = _custom_food_definition(str(run["definition_json"]))

            account_rows = {str(row["account_key"]): row for row in list(run.get("accounts") or [])}
            if set(account_rows) != set(clients):
                message = "Набор FatSecret аккаунтов изменился; создание продукта остановлено для проверки."
                self.storage.update_custom_food_run(str(run["id"]), "recovery_pending", error=message)
                raise FatSecretError(message)
            if str(run["status"]) == "completed":
                completed_food_ids = {
                    account_key: str(row["remote_food_id"] or "")
                    for account_key, row in account_rows.items()
                }
                if any(not food_id for food_id in completed_food_ids.values()):
                    message = "В завершенном журнале продукта потерян FatSecret ID; нужна ручная проверка."
                    self.storage.update_custom_food_run(str(run["id"]), "recovery_pending", error=message)
                    raise FatSecretError(message)
                return CustomFoodCreateResult(
                    run_id=str(run["id"]),
                    title=definition.title,
                    food_ids=completed_food_ids,
                    reused_accounts=tuple(sorted(account_rows)),
                )

            reused_accounts: list[str] = []
            barcode_account_key = sorted(clients)[0]
            for account_key, client in clients.items():
                row = account_rows[account_key]
                remote_food_id = str(row["remote_food_id"] or "")
                account_definition = (
                    definition
                    if account_key == barcode_account_key
                    else replace(definition, barcode="", barcode_type="")
                )
                try:
                    if remote_food_id:
                        actual = await client.get_custom_food_definition(remote_food_id)
                        if not _custom_food_definition_matches(account_definition, actual):
                            raise FatSecretError(
                                f"{client.account.label}: сохраненный продукт «{definition.title}» изменился."
                            )
                        self.storage.update_custom_food_run_account(
                            str(run["id"]),
                            account_key,
                            "verified",
                            remote_food_id=remote_food_id,
                        )
                        row["status"] = "verified"
                    else:
                        remote_food_id, title_conflict = await self._existing_custom_food_id(
                            client,
                            account_definition,
                        )
                        if remote_food_id:
                            reused_accounts.append(account_key)
                        elif title_conflict:
                            raise FatSecretError(
                                f"{client.account.label}: личный продукт «{definition.title}» уже есть с другими БЖУ. "
                                "Измени название нового продукта."
                            )
                        else:
                            try:
                                remote_food_id = await client.create_custom_food(account_definition)
                            except Exception as exc:
                                if not _is_ambiguous_mutation_error(exc):
                                    raise
                                logger.warning(
                                    "Custom food create ambiguous; searching exact readback account=%s title=%r",
                                    client.account.label,
                                    definition.title,
                                    exc_info=True,
                                )
                                remote_food_id, _ = await self._existing_custom_food_id(
                                    client,
                                    account_definition,
                                )
                                if not remote_food_id:
                                    raise FatSecretError(
                                        f"{client.account.label}: ответ создания продукта потерян. "
                                        "Повтори действие: журнал сохранен и дубль не будет создан."
                                    ) from exc
                            actual = await client.get_custom_food_definition(remote_food_id)
                            if not _custom_food_definition_matches(account_definition, actual):
                                raise FatSecretError(
                                    f"{client.account.label}: FatSecret вернул продукт с другими значениями."
                                )
                        self.storage.update_custom_food_run_account(
                            str(run["id"]),
                            account_key,
                            "verified",
                            remote_food_id=remote_food_id,
                        )
                        row["remote_food_id"] = remote_food_id
                        row["status"] = "verified"
                except Exception as exc:
                    message = user_safe_error_message(exc)
                    self.storage.update_custom_food_run_account(
                        str(run["id"]),
                        account_key,
                        str(row["status"]),
                        remote_food_id=remote_food_id or None,
                        error=message,
                    )
                    self.storage.update_custom_food_run(str(run["id"]), "recovery_pending", error=message)
                    raise FatSecretError(
                        "Продукт создан не во всех аккаунтах. Уже подтвержденные ID сохранены; "
                        f"повтори действие для безопасного продолжения. {message}"
                    ) from exc

            food_ids = {
                account_key: str(row["remote_food_id"])
                for account_key, row in account_rows.items()
                if row.get("remote_food_id")
            }
            if set(food_ids) != set(clients):
                raise FatSecretError("Не для всех аккаунтов подтвержден ID созданного продукта.")
            for source_account_key, source_food_id in food_ids.items():
                for target_account_key, target_food_id in food_ids.items():
                    if source_account_key == target_account_key:
                        continue
                    self.storage.set_custom_food_mapping(
                        source_account_key,
                        source_food_id,
                        target_account_key,
                        target_food_id,
                        content_hash,
                    )
            self.storage.update_custom_food_run(str(run["id"]), "completed")
            return CustomFoodCreateResult(
                run_id=str(run["id"]),
                title=definition.title,
                food_ids=food_ids,
                reused_accounts=tuple(sorted(reused_accounts)),
            )
        finally:
            await self._close_clients(clients)

    @staticmethod
    def custom_food_recipe_list_item(
        definition: CustomFoodDefinition,
        created: CustomFoodCreateResult,
        grams: Decimal,
        requested_query: str,
    ) -> ResolvedRecipeListItem:
        """Build a recipe ingredient backed by verified account-specific personal-food IDs."""
        if not created.food_ids:
            raise FatSecretError("У созданного продукта нет подтвержденных FatSecret ID.")
        canonical_account_key = sorted(created.food_ids)[0]
        canonical_food_id = created.food_ids[canonical_account_key]
        return ResolvedRecipeListItem(
            requested_query=requested_query,
            grams=grams,
            ingredient=Ingredient(
                id=str(uuid.uuid4()),
                recipe_id="",
                food_id=canonical_food_id,
                title=definition.title,
                portion_id="0",
                amount=_gram_portion_amount(grams),
                portion_description="100г",
                grams=grams,
            ),
            source="создан в группе",
            energy_per_100g=definition.nutrients.get("calories"),
            protein_per_100g=definition.nutrients.get("protein"),
            fat_per_100g=definition.nutrients.get("totalFat"),
            carbohydrate_per_100g=definition.nutrients.get("carbohydrate"),
            custom_food_ids=dict(created.food_ids),
        )

    async def _normalize_recipe_ingredients(
        self,
        client: FatSecretClient,
        ingredients: list[Ingredient],
        *,
        detail_tasks: dict[str, asyncio.Task[FoodSearchResult]] | None = None,
    ) -> list[Ingredient]:
        if len(ingredients) <= 1:
            return [
                await self._normalize_recipe_ingredient(client, ingredient, detail_tasks=detail_tasks)
                for ingredient in ingredients
            ]
        await client.ensure_logged_in()
        semaphore = asyncio.Semaphore(INGREDIENT_NORMALIZE_CONCURRENCY)

        async def normalize_one(ingredient: Ingredient) -> Ingredient:
            async with semaphore:
                return await self._normalize_recipe_ingredient(client, ingredient, detail_tasks=detail_tasks)

        return list(await asyncio.gather(*(normalize_one(ingredient) for ingredient in ingredients)))

    async def _normalize_recipe_ingredient(
        self,
        client: FatSecretClient,
        ingredient: Ingredient,
        *,
        detail_tasks: dict[str, asyncio.Task[FoodSearchResult]] | None = None,
    ) -> Ingredient:
        grams = _ingredient_grams_or_none(ingredient)
        metadata: FoodSearchResult | None = None
        if ingredient.food_id and not _ingredient_current_portion_sends_grams(ingredient, grams):
            try:
                detail_input = FoodSearchResult(
                    food_id=ingredient.food_id,
                    title=ingredient.title,
                    default_portion_id=ingredient.portion_id or "0",
                    default_portion_description=ingredient.portion_description,
                )
                if detail_tasks is None:
                    metadata = await client.resolve_food_detail(detail_input)
                else:
                    task = detail_tasks.get(ingredient.food_id)
                    if task is None:
                        task = asyncio.create_task(client.resolve_food_detail(detail_input))
                        detail_tasks[ingredient.food_id] = task
                    metadata = await task
            except Exception:  # noqa: BLE001 - keep the recipe usable if one food detail lookup fails.
                logger.debug("ingredient gram normalization detail lookup failed for %s", ingredient.title, exc_info=True)

        if grams is None and metadata is not None:
            portion_grams = _food_result_portion_grams(metadata)
            if portion_grams is not None:
                grams = ingredient.amount * portion_grams

        if grams is None:
            return Ingredient(
                id=ingredient.id,
                recipe_id=ingredient.recipe_id,
                food_id=ingredient.food_id,
                title=ingredient.title,
                portion_id=ingredient.portion_id,
                amount=ingredient.amount,
                portion_description=ingredient.portion_description,
                remote_ingredient_id=ingredient.remote_ingredient_id,
                grams=None,
            )

        if metadata is not None:
            return _ingredient_from_food_result(
                metadata,
                grams,
                id=ingredient.id,
                recipe_id=ingredient.recipe_id,
                food_id=metadata.food_id or ingredient.food_id,
                title=metadata.title or ingredient.title,
                remote_ingredient_id=ingredient.remote_ingredient_id,
            )

        portion_id = ingredient.portion_id or "0"
        if portion_id != "0" and is_explicit_weight_portion(ingredient.portion_description):
            return Ingredient(
                id=ingredient.id,
                recipe_id=ingredient.recipe_id,
                food_id=ingredient.food_id,
                title=ingredient.title,
                portion_id=portion_id,
                amount=grams,
                portion_description="г",
                remote_ingredient_id=ingredient.remote_ingredient_id,
                grams=grams,
            )
        return Ingredient(
            id=ingredient.id,
            recipe_id=ingredient.recipe_id,
            food_id=ingredient.food_id,
            title=ingredient.title,
            portion_id="0",
            amount=_gram_portion_amount(grams),
            portion_description="100г",
            remote_ingredient_id=ingredient.remote_ingredient_id,
            grams=grams,
        )

    async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
        """Load and merge current cookbook recipe summaries from all FatSecret accounts in a group."""
        clients = self._build_clients(group_id)
        merged: dict[str, Recipe] = {}
        summaries_by_account: dict[str, list] = {}
        live_remote_ids_by_account = {account_key: set() for account_key in clients}
        try:
            for account_key, client in clients.items():
                summaries = await client.cookbook()
                summaries_by_account[account_key] = summaries
                for summary in summaries:
                    live_remote_ids_by_account[account_key].add(summary.remote_id)
                    normalized = normalize_title(summary.title)
                    if not normalized:
                        continue
                    recipe = merged.get(normalized)
                    if recipe is None:
                        recipe = Recipe(
                            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fatsecret-bot:recipe:{group_id}:{normalized}")),
                            title=summary.title,
                            description=summary.description,
                            group_id=group_id,
                        )
                        merged[normalized] = recipe
                    recipe.remote_ids_by_account.setdefault(account_key, []).append(summary.remote_id)
                    recipe.remote_ids.setdefault(account_key, summary.remote_id)
        finally:
            await self._close_clients(clients)
        for account_key, summaries in summaries_by_account.items():
            for summary in summaries:
                self.storage.upsert_remote_recipe_summary(
                    account_key,
                    summary.remote_id,
                    summary.title,
                )
            self.storage.reconcile_remote_recipe_snapshots(
                account_key,
                live_remote_ids_by_account[account_key],
            )
        self.storage.reconcile_group_remote_recipes(group_id, live_remote_ids_by_account)
        return [merged[key] for key in sorted(merged)]

    async def hydrate_live_recipe_variants(self, recipe_ref: Recipe) -> list[RemoteRecipeVariant]:
        """Load and persist every account-specific live version represented by a merged recipe card."""
        clients = self._build_clients(recipe_ref.group_id)
        variants: list[RemoteRecipeVariant] = []
        try:
            for account_key in sorted(set(recipe_ref.remote_ids) | set(recipe_ref.remote_ids_by_account)):
                client = clients.get(account_key)
                if client is None:
                    continue
                for remote_id in _remote_ids_for_account(recipe_ref, account_key):
                    snapshot = await self._load_recipe_source_snapshot(client, remote_id, recipe_ref.id)
                    display = snapshot.display
                    display.title = display.title or recipe_ref.title
                    display.group_id = recipe_ref.group_id
                    display.remote_ids = {account_key: remote_id}
                    display.remote_ids_by_account = {account_key: [remote_id]}
                    strict_fingerprint = recipe_fingerprint(snapshot.transport)
                    self.storage.upsert_remote_recipe_snapshot(
                        account_key,
                        remote_id,
                        snapshot.transport,
                        strict_fingerprint,
                    )
                    variants.append(
                        RemoteRecipeVariant(
                            account_key=account_key,
                            remote_recipe_id=remote_id,
                            recipe=display,
                            fingerprint=recipe_content_fingerprint(snapshot.transport),
                        )
                    )
        finally:
            await self._close_clients(clients)
        return variants

    async def hydrate_live_recipe(self, recipe_ref: Recipe) -> Recipe | None:
        """Load current recipe details from FatSecret for an in-memory recipe reference."""
        variants = await self.hydrate_live_recipe_variants(recipe_ref)
        if not variants:
            return None
        recipe = variants[0].recipe
        recipe.remote_ids = dict(recipe_ref.remote_ids)
        recipe.remote_ids_by_account = {
            account_key: list(remote_ids)
            for account_key, remote_ids in recipe_ref.remote_ids_by_account.items()
        }
        return recipe

    async def resolve_recipe_list_items(self, group_id: str, items: list[RecipeListItem]) -> RecipeListDraft:
        """Resolve free-text ingredient lines using daily FatSecret usage cache and live search."""
        await self.ensure_food_usage_cache(group_id)
        resolved: list[ResolvedRecipeListItem] = []
        unresolved: list[str] = []
        for item in items:
            candidates = await self.recipe_list_candidates(group_id, item.query, item.grams, limit=1)
            if not candidates:
                unresolved.append(item)
                continue
            resolved.append(candidates[0])
        return RecipeListDraft(items=resolved, unresolved=unresolved)

    async def _local_food_metadata(
        self,
        client: FatSecretClient,
        ingredient: Ingredient,
        query: str,
    ) -> FoodSearchResult | None:
        direct_metadata: FoodSearchResult | None = None
        if ingredient.food_id:
            try:
                direct_metadata = await client.resolve_food_detail(
                    FoodSearchResult(
                        food_id=ingredient.food_id,
                        title=ingredient.title,
                        default_portion_id=ingredient.portion_id or "0",
                        default_portion_description=ingredient.portion_description,
                    )
                )
            except Exception:  # noqa: BLE001 - fall back to search metadata if direct lookup fails.
                logger.debug("local food direct metadata lookup failed for %s", ingredient.title, exc_info=True)
        if direct_metadata is not None and _food_result_has_macros(direct_metadata):
            return direct_metadata
        title_matches: list[FoodSearchResult] = []
        search_queries = [ingredient.title, query]
        if direct_metadata is not None and direct_metadata.brand:
            brand = direct_metadata.brand.strip()
            brand_words = brand.replace("-", " ")
            search_queries.extend(
                [
                    f"{brand} {ingredient.title}",
                    f"{ingredient.title} {brand}",
                    f"{brand_words} {ingredient.title}",
                    f"{ingredient.title} {brand_words}",
                ]
            )
        seen_queries: set[str] = set()
        for search_query in search_queries:
            if not search_query.strip():
                continue
            normalized_search_query = normalize_title(search_query)
            if normalized_search_query in seen_queries:
                continue
            seen_queries.add(normalized_search_query)
            try:
                results = _dedupe_food_results([*await client.search_recipes(search_query, page=0)])
                if not results:
                    results = _dedupe_food_results([*await client.autocomplete_food(search_query)])
            except Exception:  # noqa: BLE001 - keep local candidate usable on lookup failure.
                logger.debug("local food metadata lookup failed for %s", search_query, exc_info=True)
                continue
            for result in results:
                if result.food_id != ingredient.food_id:
                    if (
                        _matches_requested_food(ingredient.title, result.title, _food_search_text(result))
                        and not _title_has_extra_meaningful_tokens(ingredient.title, result.title)
                        and _matches_direct_food_metadata(result, direct_metadata)
                    ):
                        title_matches.append(result)
                    continue
                try:
                    return result if _food_result_has_detail(result) else await client.resolve_food_detail(result)
                except Exception:  # noqa: BLE001 - search metadata is still better than local-only data.
                    logger.debug("local food detail lookup failed for %s", result.title, exc_info=True)
                    return result
        title_matches = _dedupe_food_results(title_matches)
        title_matches.sort(key=lambda item: _food_result_rank(ingredient.title, item))
        for result in title_matches:
            try:
                resolved = result if _food_result_has_detail(result) else await client.resolve_food_detail(result)
                if _matches_direct_food_metadata(resolved, direct_metadata):
                    return resolved
            except Exception:  # noqa: BLE001 - search metadata is still better than local-only data.
                logger.debug("local food title metadata lookup failed for %s", result.title, exc_info=True)
                return result if _matches_direct_food_metadata(result, direct_metadata) else direct_metadata
        return direct_metadata

    async def _cached_food_usage_candidates(
        self,
        group_id: str,
        query: str,
        grams: Decimal,
        client: FatSecretClient | None,
    ) -> list[ResolvedRecipeListItem]:
        candidates: list[ResolvedRecipeListItem] = []
        for usage in self.storage.list_food_usage_cache(group_id):
            if not _matches_requested_food(query, usage.title):
                continue
            usage_ingredient = Ingredient(
                id=str(uuid.uuid4()),
                recipe_id="",
                food_id=usage.food_id,
                title=usage.title,
                portion_id=usage.portion_id or "0",
                amount=Decimal("0"),
                portion_description=usage.portion_description,
            )
            metadata: FoodSearchResult | None = None
            if client is not None:
                metadata = await self._local_food_metadata(client, usage_ingredient, query)
            protein = metadata.protein_per_portion if metadata is not None else None
            fat = metadata.fat_per_portion if metadata is not None else None
            carbohydrate = metadata.carbohydrate_per_portion if metadata is not None else None
            portion_metadata = metadata if metadata is not None and metadata.food_id == usage.food_id else None
            candidates.append(
                ResolvedRecipeListItem(
                    requested_query=query,
                    grams=grams,
                    ingredient=_ingredient_from_food_result(
                        portion_metadata or FoodSearchResult(food_id=usage.food_id, title=usage.title),
                        grams,
                        food_id=usage.food_id,
                        title=usage.title,
                    ),
                    source="часто использовался",
                    brand=metadata.brand if metadata is not None else "",
                    usage_count=usage.use_count,
                    energy_per_100g=(
                        _correct_energy(metadata.energy_per_portion, protein, fat, carbohydrate)
                        if metadata is not None
                        else None
                    ),
                    protein_per_100g=protein,
                    fat_per_100g=fat,
                    carbohydrate_per_100g=carbohydrate,
                )
            )
        return candidates

    async def recipe_list_candidates(
        self,
        group_id: str,
        query: str,
        grams: Decimal,
        limit: int = 6,
        offset: int = 0,
    ) -> list[ResolvedRecipeListItem]:
        """Return replacement candidates for one free-text ingredient line."""
        limit = max(1, limit)
        offset = max(0, offset)
        local_candidates: list[ResolvedRecipeListItem] = []
        clients: dict[str, FatSecretClient] | None = None

        def get_first_client() -> FatSecretClient:
            nonlocal clients
            if clients is None:
                clients = self._build_clients(group_id)
            return next(iter(clients.values()))

        try:
            await self.ensure_food_usage_cache(group_id)
            first_client_for_cache: FatSecretClient | None = None
            try:
                first_client_for_cache = get_first_client()
            except FatSecretError:
                first_client_for_cache = None
            local_candidates = await self._cached_food_usage_candidates(
                group_id,
                query,
                grams,
                first_client_for_cache,
            )

            try:
                first_client = get_first_client()
            except FatSecretError:
                if local_candidates:
                    local_candidates.sort(key=lambda item: _resolved_candidate_rank(query, item))
                    return local_candidates[offset : offset + limit]
                raise
            remote_limit = offset + limit + 10
            raw_target_count = remote_limit
            remote_candidates: list[FoodSearchResult] = []
            variants = _query_variants(query)

            search_pages = max(1, (raw_target_count // 10) + 1)
            for page in range(search_pages):
                remote_candidates.extend(await first_client.search_recipes(query, page=page))

            if len(_dedupe_food_results(remote_candidates)) < raw_target_count:
                for variant in variants[1:]:
                    remote_candidates.extend(await first_client.search_recipes(variant, page=0))
                    if len(_dedupe_food_results(remote_candidates)) >= raw_target_count:
                        break

            if not _dedupe_food_results(remote_candidates):
                for variant in variants:
                    remote_candidates.extend(await first_client.autocomplete_food(variant))

            remote_candidates = [
                item
                for item in _dedupe_food_results(remote_candidates)
                if _matches_requested_food(query, item.title, _food_search_text(item))
            ]
            remote_candidates.sort(key=lambda item: _food_result_rank(query, item))
            remote_candidates = remote_candidates[: remote_limit + 5]

            remote_resolved: list[ResolvedRecipeListItem] = []
            for remote in remote_candidates:
                if len(remote_resolved) >= remote_limit:
                    break
                try:
                    found = remote if _food_result_has_detail(remote) else await first_client.resolve_food_detail(remote)
                except Exception:  # noqa: BLE001 - keep alternative candidates usable.
                    logger.debug("recipe list candidate resolve failed for %s", remote.title, exc_info=True)
                    continue
                if not _matches_requested_food(query, found.title, _food_search_text(found)):
                    continue
                protein = found.protein_per_portion
                fat = found.fat_per_portion
                carbohydrate = found.carbohydrate_per_portion
                remote_resolved.append(
                    ResolvedRecipeListItem(
                        requested_query=query,
                        grams=grams,
                        ingredient=_ingredient_from_food_result(found, grams),
                        source="FatSecret",
                        brand=found.brand,
                        energy_per_100g=_correct_energy(found.energy_per_portion, protein, fat, carbohydrate),
                        protein_per_100g=protein,
                        fat_per_100g=fat,
                        carbohydrate_per_100g=carbohydrate,
                    )
                )
            candidates = [*local_candidates, *remote_resolved]
            candidates.sort(key=lambda item: _resolved_candidate_rank(query, item))
            deduped_candidates: list[ResolvedRecipeListItem] = []
            seen_candidates: dict[tuple[str, str], int] = {}
            for candidate in candidates:
                key = (candidate.ingredient.food_id, normalize_title(candidate.ingredient.title))
                existing_index = seen_candidates.get(key)
                if existing_index is not None:
                    existing = deduped_candidates[existing_index]
                    if (
                        _macro_field_count(candidate),
                        bool(candidate.brand),
                    ) > (
                        _macro_field_count(existing),
                        bool(existing.brand),
                    ):
                        deduped_candidates[existing_index] = candidate
                    continue
                seen_candidates[key] = len(deduped_candidates)
                deduped_candidates.append(candidate)
            return deduped_candidates[offset : offset + limit]
        finally:
            if clients is not None:
                await self._close_clients(clients)

    async def _resolve_food_from_remote(self, client: FatSecretClient, query: str) -> FoodSearchResult | None:
        candidates = await client.search_recipes(query)
        if not candidates:
            candidates = await client.autocomplete_food(query)
        if not candidates:
            return None
        candidates.sort(key=lambda item: _food_result_rank(query, item))
        return candidates[0] if _food_result_has_detail(candidates[0]) else await client.resolve_food_detail(candidates[0])

    async def _legacy_addable_ingredient(
        self,
        client: FatSecretClient,
        ingredient: Ingredient,
        requested_query: str | None = None,
        *,
        same_food_id_only: bool = False,
    ) -> Ingredient | None:
        search_addable = getattr(client, "search_addable_foods", None)
        if search_addable is None:
            logger.warning(
                "Legacy ingredient fallback unavailable account=%s food_id=%s title=%r",
                client.account.label,
                ingredient.food_id,
                ingredient.title,
            )
            return None
        logger.info(
            "Legacy ingredient fallback search started account=%s original_food_id=%s title=%r "
            "portion_id=%s amount=%s requested_query=%r",
            client.account.label,
            ingredient.food_id,
            ingredient.title,
            ingredient.portion_id or "0",
            ingredient.amount,
            requested_query,
        )
        candidates: list[FoodSearchResult] = []
        seen_queries: set[str] = set()
        search_texts = [ingredient.title]
        if requested_query:
            search_texts.append(requested_query)
        for search_text in search_texts:
            for query in _query_variants(search_text):
                normalized_query = normalize_title(query)
                if normalized_query in seen_queries:
                    continue
                seen_queries.add(normalized_query)
                try:
                    found_candidates = await search_addable(query, page=0)
                    candidates.extend(found_candidates)
                    logger.debug(
                        "Legacy ingredient fallback query account=%s query=%r candidates=%d candidate_ids=%s",
                        client.account.label,
                        query,
                        len(found_candidates),
                        [item.food_id for item in found_candidates],
                    )
                except Exception:  # noqa: BLE001 - add fallback should not hide the original ingredient failure.
                    logger.warning(
                        "Legacy ingredient fallback query failed account=%s query=%r",
                        client.account.label,
                        query,
                        exc_info=True,
                    )
        match_query = requested_query or ingredient.title
        candidates = [
            item
            for item in _dedupe_food_results(candidates)
            if (
                item.food_id
                and (not same_food_id_only or item.food_id == ingredient.food_id)
                and _matches_requested_food(match_query, item.title, _food_search_text(item))
            )
        ]
        candidates.sort(key=lambda item: _food_result_rank(match_query, item))
        logger.debug(
            "Legacy ingredient fallback ranked account=%s original_food_id=%s candidates=%s",
            client.account.label,
            ingredient.food_id,
            [(item.food_id, item.title, item.default_portion_id) for item in candidates],
        )
        for candidate in candidates:
            try:
                found = candidate if _food_result_has_detail(candidate) else await client.resolve_food_detail(candidate)
            except Exception:  # noqa: BLE001 - keep add fallback usable if detail lookup fails.
                logger.debug("legacy addable detail lookup failed for %s", candidate.title, exc_info=True)
                found = candidate
            converted = _ingredient_with_search_result(ingredient, found)
            if (
                candidate.food_id != ingredient.food_id
                or converted.portion_id != ingredient.portion_id
                or converted.portion_description != ingredient.portion_description
                or converted.amount != ingredient.amount
            ):
                logger.info(
                    "Legacy ingredient fallback selected account=%s original_food_id=%s fallback_food_id=%s "
                    "title=%r portion_id=%s amount=%s portion=%r grams=%s",
                    client.account.label,
                    ingredient.food_id,
                    converted.food_id,
                    converted.title,
                    converted.portion_id or "0",
                    converted.amount,
                    converted.portion_description,
                    converted.grams,
                )
                return converted
        logger.warning(
            "Legacy ingredient fallback found no usable candidate account=%s original_food_id=%s title=%r candidates=%d",
            client.account.label,
            ingredient.food_id,
            ingredient.title,
            len(candidates),
        )
        return None

    async def _add_ingredient_with_fallback(
        self,
        client: FatSecretClient,
        remote_id: str,
        ingredient: Ingredient,
        requested_query: str | None = None,
        action_error_fallback: Callable[[], Awaitable[Ingredient | None]] | None = None,
        prefer_original: bool = False,
        allow_legacy_fallback: bool = True,
    ) -> Ingredient | None:
        logger.info(
            "Ingredient fallback flow started account=%s remote_recipe_id=%s food_id=%s iid=%s title=%r "
            "portion_id=%s amount=%s portion=%r grams=%s prefer_original=%s",
            client.account.label,
            remote_id,
            ingredient.food_id,
            ingredient.remote_ingredient_id or "0",
            ingredient.title,
            ingredient.portion_id or "0",
            ingredient.amount,
            ingredient.portion_description,
            ingredient.grams,
            prefer_original,
        )
        grams = _ingredient_grams_or_none(ingredient)
        prepared: Ingredient | None = None
        if not prefer_original and not _ingredient_current_portion_sends_grams(ingredient, grams):
            # Portion preparation must not silently replace the food selected by the user.
            # A different food ID is eligible only after FatSecret rejects the exact selection.
            prepared = await self._legacy_addable_ingredient(
                client,
                ingredient,
                requested_query,
                same_food_id_only=True,
            )
        first_try = prepared or ingredient
        logger.info(
            "Ingredient first attempt selected account=%s remote_recipe_id=%s original_food_id=%s attempt_food_id=%s "
            "iid=%s portion_id=%s amount=%s portion=%r grams=%s prepared=%s",
            client.account.label,
            remote_id,
            ingredient.food_id,
            first_try.food_id,
            first_try.remote_ingredient_id or "0",
            first_try.portion_id or "0",
            first_try.amount,
            first_try.portion_description,
            first_try.grams,
            prepared is not None,
        )
        action_error: FatSecretActionError | None = None
        try:
            if await client.add_ingredient(remote_id, first_try):
                logger.info(
                    "Ingredient first attempt accepted account=%s remote_recipe_id=%s food_id=%s",
                    client.account.label,
                    remote_id,
                    first_try.food_id,
                )
                return first_try
            logger.warning(
                "Ingredient first attempt returned false account=%s remote_recipe_id=%s food_id=%s",
                client.account.label,
                remote_id,
                first_try.food_id,
            )
        except FatSecretActionError as exc:
            is_backend_rejection = (
                exc.status_code == 302
                and exc.action == "ingredientsave"
                and exc.location.casefold().startswith("/errorloguserfeedback.ashx")
            )
            if not is_backend_rejection:
                raise
            action_error = exc
            logger.warning(
                "Ingredient backend rejection account=%s remote_recipe_id=%s food_id=%s status=%d Location=%s replayed=%s",
                client.account.label,
                remote_id,
                first_try.food_id,
                exc.status_code,
                exc.location,
                exc.replayed,
            )
            if action_error_fallback is not None:
                logger.info(
                    "Ingredient ownership fallback started account=%s remote_recipe_id=%s source_food_id=%s",
                    client.account.label,
                    remote_id,
                    ingredient.food_id,
                )
                exact_fallback = await action_error_fallback()
                if exact_fallback is not None:
                    logger.info(
                        "Ingredient ownership fallback selected account=%s remote_recipe_id=%s source_food_id=%s "
                        "target_food_id=%s portion_id=%s amount=%s",
                        client.account.label,
                        remote_id,
                        ingredient.food_id,
                        exact_fallback.food_id,
                        exact_fallback.portion_id or "0",
                        exact_fallback.amount,
                    )
                    if await client.add_ingredient(remote_id, exact_fallback):
                        logger.info(
                            "Ingredient ownership fallback accepted account=%s remote_recipe_id=%s target_food_id=%s",
                            client.account.label,
                            remote_id,
                            exact_fallback.food_id,
                        )
                        return exact_fallback
                    logger.warning(
                        "Ingredient ownership fallback returned false account=%s remote_recipe_id=%s target_food_id=%s",
                        client.account.label,
                        remote_id,
                        exact_fallback.food_id,
                    )
                    return None
                logger.info(
                    "Ingredient ownership fallback not applicable account=%s remote_recipe_id=%s source_food_id=%s",
                    client.account.label,
                    remote_id,
                    ingredient.food_id,
                )
        if not allow_legacy_fallback:
            logger.error(
                "Exact ingredient copy rejected without substitution account=%s remote_recipe_id=%s food_id=%s",
                client.account.label,
                remote_id,
                ingredient.food_id,
            )
            if action_error is not None:
                raise action_error
            return None
        fallback = None if prepared is not None else await self._legacy_addable_ingredient(client, ingredient, requested_query)
        if fallback is None:
            logger.error(
                "Ingredient fallback exhausted account=%s remote_recipe_id=%s food_id=%s had_action_error=%s",
                client.account.label,
                remote_id,
                ingredient.food_id,
                action_error is not None,
            )
            if action_error is not None:
                raise action_error
            return None
        logger.info(
            "Ingredient legacy attempt started account=%s remote_recipe_id=%s original_food_id=%s fallback_food_id=%s "
            "portion_id=%s amount=%s portion=%r grams=%s",
            client.account.label,
            remote_id,
            ingredient.food_id,
            fallback.food_id,
            fallback.portion_id or "0",
            fallback.amount,
            fallback.portion_description,
            fallback.grams,
        )
        if await client.add_ingredient(remote_id, fallback):
            logger.info(
                "Ingredient legacy attempt accepted account=%s remote_recipe_id=%s fallback_food_id=%s",
                client.account.label,
                remote_id,
                fallback.food_id,
            )
            return fallback
        logger.warning(
            "Ingredient legacy attempt returned false account=%s remote_recipe_id=%s fallback_food_id=%s",
            client.account.label,
            remote_id,
            fallback.food_id,
        )
        if action_error is not None:
            raise action_error
        return None

    def _mapped_custom_food_ingredient(
        self,
        source_account_key: str | None,
        target_account_key: str | None,
        ingredient: Ingredient,
    ) -> Ingredient:
        if not source_account_key or not target_account_key:
            return ingredient
        mapped_food_id = self.storage.custom_food_mapping(
            source_account_key,
            ingredient.food_id,
            target_account_key,
        )
        return _ingredient_with_food_id(ingredient, mapped_food_id) if mapped_food_id else ingredient

    async def _clone_custom_food_ingredient(
        self,
        source_client: FatSecretClient,
        target_client: FatSecretClient,
        source_account_key: str,
        target_account_key: str,
        source_ingredient: Ingredient,
        target_ingredient: Ingredient,
    ) -> Ingredient | None:
        logger.info(
            "Custom food ownership inspection started source_account=%s target_account=%s source_food_id=%s title=%r",
            source_client.account.label,
            target_client.account.label,
            source_ingredient.food_id,
            source_ingredient.title,
        )
        try:
            definition = await source_client.get_custom_food_definition(source_ingredient.food_id)
        except FatSecretNotCustomFoodError:
            logger.info(
                "Custom food ownership inspection classified public source_account=%s target_account=%s "
                "source_food_id=%s",
                source_client.account.label,
                target_client.account.label,
                source_ingredient.food_id,
            )
            return None
        logger.info(
            "Custom food ownership confirmed source_account=%s target_account=%s source_food_id=%s title=%r",
            source_client.account.label,
            target_client.account.label,
            source_ingredient.food_id,
            definition.title,
        )
        target_food_id = await target_client.create_custom_food(definition)
        self.storage.set_custom_food_mapping(
            source_account_key,
            source_ingredient.food_id,
            target_account_key,
            target_food_id,
            _custom_food_content_hash(definition),
        )
        logger.info(
            "Custom food mapping stored source_account=%s target_account=%s source_food_id=%s target_food_id=%s",
            source_client.account.label,
            target_client.account.label,
            source_ingredient.food_id,
            target_food_id,
        )
        return _ingredient_with_food_id(target_ingredient, target_food_id)

    async def _prepare_target_recipe(
        self,
        source_client: FatSecretClient,
        target_client: FatSecretClient,
        source_account_key: str,
        target_account_key: str,
        recipe: Recipe,
        cache: dict[str, str | None],
    ) -> Recipe:
        """Resolve every personal food before the first target ingredient mutation."""
        target_recipe = _copy_recipe_from_remote(recipe.id, recipe)
        target_recipe.group_id = recipe.group_id
        target_recipe.ingredients = []
        for ingredient in recipe.ingredients:
            if ingredient.food_id in cache:
                mapped_id = cache[ingredient.food_id]
                target_recipe.ingredients.append(
                    _ingredient_with_food_id(ingredient, mapped_id) if mapped_id else ingredient
                )
                continue
            try:
                definition = await source_client.get_custom_food_definition(ingredient.food_id)
            except FatSecretNotCustomFoodError:
                cache[ingredient.food_id] = None
                target_recipe.ingredients.append(ingredient)
                continue

            mapped_id = self.storage.custom_food_mapping(
                source_account_key,
                ingredient.food_id,
                target_account_key,
            )
            if mapped_id is not None:
                try:
                    await target_client.get_custom_food_definition(mapped_id)
                except FatSecretNotCustomFoodError:
                    logger.warning(
                        "Custom food mapping stale source_account=%s target_account=%s source_food_id=%s "
                        "target_food_id=%s",
                        source_client.account.label,
                        target_client.account.label,
                        ingredient.food_id,
                        mapped_id,
                    )
                    self.storage.delete_custom_food_mapping(
                        source_account_key,
                        ingredient.food_id,
                        target_account_key,
                    )
                    mapped_id = None
            if mapped_id is None:
                mapped_id = await target_client.create_custom_food(definition)
                try:
                    await target_client.get_custom_food_definition(mapped_id)
                except FatSecretNotCustomFoodError as exc:
                    raise FatSecretError(
                        f"{target_client.account.label}: созданный личный продукт «{definition.title}» "
                        "не виден целевому аккаунту"
                    ) from exc
                content_hash = _custom_food_content_hash(definition)
                self.storage.set_custom_food_mapping(
                    source_account_key,
                    ingredient.food_id,
                    target_account_key,
                    mapped_id,
                    content_hash,
                )
                self.storage.set_custom_food_mapping(
                    target_account_key,
                    mapped_id,
                    source_account_key,
                    ingredient.food_id,
                    content_hash,
                )
            cache[ingredient.food_id] = mapped_id
            target_recipe.ingredients.append(_ingredient_with_food_id(ingredient, mapped_id))
        return target_recipe

    async def _verify_remote_recipe(
        self,
        client: FatSecretClient,
        account_key: str,
        remote_id: str,
        expected: Recipe,
    ) -> Recipe:
        """Read back one mutation and reject only missing or unexpected ingredients."""
        actual = await client.get_recipe(remote_id)
        actual_fingerprint = recipe_fingerprint(actual)
        differences = _recipe_ingredient_membership_differences(expected, actual)
        if differences:
            raise FatSecretError(
                f"{client.account.label}: FatSecret сохранил неверный набор продуктов: "
                + "; ".join(differences)
            )
        self.storage.upsert_remote_recipe_snapshot(
            account_key,
            remote_id,
            actual,
            actual_fingerprint,
        )
        return actual

    async def _create_recipe_with_readback(
        self,
        client: FatSecretClient,
        recipe: Recipe,
    ) -> str:
        """Recover a timed-out create by looking up its unique title before any replay."""
        try:
            return await client.create_recipe(recipe)
        except Exception as exc:
            if not _is_ambiguous_mutation_error(exc):
                raise
            logger.warning(
                "Recipe create ambiguous; reading cookbook account=%s title=%r",
                client.account.label,
                recipe.title,
                exc_info=True,
            )
            matches = [
                item.remote_id
                for item in await client.cookbook()
                if normalize_title(item.title) == normalize_title(recipe.title)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise
            raise FatSecretError(
                f"{client.account.label}: после timeout создания найдено несколько рецептов «{recipe.title}»; "
                "операция остановлена без повтора"
            ) from exc

    async def _sync_ingredients_with_readback(
        self,
        client: FatSecretClient,
        recipe: Recipe,
        remote_id: str,
    ) -> IngredientSyncStats:
        """Resume an ambiguous ingredient mutation only after `_sync_ingredients` reloads target detail."""
        try:
            return await self._sync_ingredients(client, recipe, remote_id)
        except Exception as exc:
            if not _is_ambiguous_mutation_error(exc):
                raise
            logger.warning(
                "Ingredient mutation ambiguous; rebuilding diff account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
                exc_info=True,
            )
            return await self._sync_ingredients(client, recipe, remote_id)

    async def _save_recipe_meta_with_readback(
        self,
        client: FatSecretClient,
        recipe: Recipe,
        remote_id: str,
    ) -> None:
        """Resolve a timed-out metadata save by detail readback before one conditional retry."""
        try:
            if not await client.save_recipe_meta(recipe, remote_id):
                raise FatSecretError(f"{client.account.label}: recipe metadata save returned false")
            return
        except Exception as exc:
            if not _is_ambiguous_mutation_error(exc):
                raise
            logger.warning(
                "Recipe metadata mutation ambiguous; reading detail account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
                exc_info=True,
            )
            actual = await client.get_recipe(remote_id)
            differences = recipe_fingerprint_diff(recipe_fingerprint(recipe), recipe_fingerprint(actual))
            if not differences:
                return
            if not await client.save_recipe_meta(recipe, remote_id):
                raise FatSecretError(f"{client.account.label}: recipe metadata retry returned false")

    async def create_recipe_from_list(
        self,
        group_id: str,
        title: str,
        items: list[ResolvedRecipeListItem],
        updated_by: int,
        *,
        portions: Decimal = Decimal("1"),
        steps: list[str] | None = None,
        replace_existing_recipe_id: str | None = None,
        replace_existing_recipe_ref: Recipe | None = None,
    ) -> RecipeCreateResult:
        """Create or replace a recipe through a durable, resumable multi-account journal."""
        final_title = title.strip()
        if not final_title:
            raise FatSecretError("Название рецепта не должно быть пустым.")
        replaced_recipe: Recipe | None = None
        replace_existing_local = False
        create_title = final_title
        if replace_existing_recipe_id is not None and replace_existing_recipe_ref is not None:
            raise FatSecretError("Передан конфликтующий контекст замены рецепта.")
        if replace_existing_recipe_id is not None:
            replaced_recipe = self.storage.get_recipe(replace_existing_recipe_id)
            if replaced_recipe is None:
                raise FatSecretError("Рецепт для замены больше не найден. Обнови список и попробуй снова.")
            replace_existing_local = True
        elif replace_existing_recipe_ref is not None:
            replaced_recipe = replace_existing_recipe_ref
        if replaced_recipe is not None:
            if replaced_recipe.group_id != group_id:
                raise FatSecretError("Рецепт для замены относится к другой группе.")
            if not replaced_recipe.remote_ids:
                raise FatSecretError("У рецепта для замены нет привязок к FatSecret.")
            create_title = self.storage.next_available_recipe_title(group_id, final_title, include_base=False)
        clean_steps = list(steps or [])
        payload_fingerprint = _recipe_list_request_fingerprint(final_title, portions, clean_steps, items)
        run = self.storage.matching_recipe_list_run(group_id, final_title, payload_fingerprint)
        if run is not None and run["status"] == "completed":
            return self._recipe_list_result_from_run(run)
        if run is None and replaced_recipe is None and self.storage.find_recipe_by_title(group_id, final_title) is not None:
            raise FatSecretError("Рецепт с таким именем уже есть. Выбери обновление существующего или измени имя.")

        clients = self._build_clients(group_id)
        try:
            if run is None:
                candidate_recipe_id = str(uuid.uuid4())
                candidate_ingredients: list[Ingredient] = []
                custom_food_ids: dict[str, dict[str, str]] = {}
                for item in items:
                    ingredient = Ingredient(
                        id=str(uuid.uuid4()),
                        recipe_id=candidate_recipe_id,
                        food_id=item.ingredient.food_id,
                        title=item.ingredient.title,
                        portion_id=item.ingredient.portion_id or "0",
                        amount=item.ingredient.amount,
                        portion_description=item.ingredient.portion_description or "г",
                        grams=item.ingredient.grams,
                    )
                    candidate_ingredients.append(ingredient)
                    if item.custom_food_ids:
                        custom_food_ids[ingredient.id] = dict(item.custom_food_ids)
                candidate = Recipe(
                    id=candidate_recipe_id,
                    title=create_title,
                    description=_sync_description(timezone=self.timezone),
                    portions=portions,
                    prep_time=0,
                    cook_time=0,
                    steps=clean_steps,
                    group_id=group_id,
                    ingredients=candidate_ingredients,
                )
                canonical_recipe_id = replaced_recipe.id if replace_existing_local and replaced_recipe is not None else None
                if canonical_recipe_id is None and replaced_recipe is not None:
                    cached_same_title = self.storage.find_recipe_by_title(group_id, final_title)
                    canonical_recipe_id = cached_same_title.id if cached_same_title is not None else None
                old_remote_ids = {
                    account_key: (
                        list(replaced_recipe.remote_ids_by_account.get(account_key) or [])
                        or ([replaced_recipe.remote_ids[account_key]] if account_key in replaced_recipe.remote_ids else [])
                        if replaced_recipe is not None
                        else []
                    )
                    for account_key in clients
                }
                run_id = self.storage.create_recipe_list_run(
                    group_id=group_id,
                    initiated_by=updated_by,
                    requested_title=final_title,
                    temporary_title=create_title,
                    canonical_recipe_id=canonical_recipe_id,
                    replaced_recipe_id=replaced_recipe.id if replaced_recipe is not None else None,
                    candidate_recipe_id=candidate_recipe_id,
                    payload_json=_recipe_list_payload_json(
                        candidate,
                        [item.requested_query for item in items],
                        custom_food_ids,
                    ),
                    payload_fingerprint=payload_fingerprint,
                    old_remote_ids=old_remote_ids,
                )
                run = self.storage.recipe_list_run(run_id)
                if run is None:
                    raise FatSecretError("Не удалось сохранить журнал создания рецепта.")
            account_rows = {str(account["account_key"]): account for account in run["accounts"]}
            if set(account_rows) != set(clients):
                message = "Набор FatSecret аккаунтов изменился; операция сохранена для безопасного продолжения."
                self.storage.update_recipe_list_run(str(run["id"]), "recovery_pending", error=message)
                raise FatSecretError(message)

            recipe, requested_queries, custom_food_ids = _recipe_list_payload(str(run["payload_json"]))
            if run["status"] == "remote_complete":
                return self._finalize_recipe_list_run(run, recipe)

            phase_one_error: tuple[str, Exception] | None = None
            for account_key, client in clients.items():
                account = account_rows[account_key]
                if str(account["status"]) in {"verified", "old_deleted", "renamed", "completed"}:
                    continue
                try:
                    await self._prepare_recipe_list_account(
                        str(run["id"]),
                        account,
                        account_key,
                        client,
                        recipe,
                        requested_queries,
                        custom_food_ids,
                    )
                except Exception as exc:  # noqa: BLE001 - rollback is coordinated after all journal writes.
                    phase_one_error = (account_key, exc)
                    self.storage.update_recipe_list_run_account(
                        str(run["id"]),
                        account_key,
                        str(account["status"]),
                        new_remote_id=(str(account["new_remote_id"]) if account.get("new_remote_id") else None),
                        error=user_safe_error_message(exc),
                    )
                    break
            if phase_one_error is not None:
                await self._rollback_recipe_list_run(run, clients, account_rows, phase_one_error)

            run = self.storage.recipe_list_run(str(run["id"]))
            if run is None:
                raise FatSecretError("Журнал создания рецепта потерян перед заменой.")
            account_rows = {str(account["account_key"]): account for account in run["accounts"]}
            active_account_key: str | None = None
            try:
                for account_key, client in clients.items():
                    active_account_key = account_key
                    account = account_rows[account_key]
                    if str(account["status"]) in {"old_deleted", "renamed", "completed"}:
                        continue
                    remote_id = str(account["new_remote_id"])
                    live_ids = {summary.remote_id for summary in await client.cookbook()}
                    for old_remote_id in account["old_remote_ids"]:
                        if old_remote_id == remote_id or old_remote_id not in live_ids:
                            continue
                        if not await self._delete_remote_recipe_confirmed(client, old_remote_id):
                            raise FatSecretError(f"{client.account.label}: не удалось удалить старый рецепт {old_remote_id}")
                    remaining_ids = {summary.remote_id for summary in await client.cookbook()}
                    undeleted = [
                        old_remote_id
                        for old_remote_id in account["old_remote_ids"]
                        if old_remote_id != remote_id and old_remote_id in remaining_ids
                    ]
                    if undeleted:
                        raise FatSecretError(
                            f"{client.account.label}: старые рецепты остались после удаления: {', '.join(undeleted)}"
                        )
                    self.storage.update_recipe_list_run_account(
                        str(run["id"]),
                        account_key,
                        "old_deleted",
                        new_remote_id=remote_id,
                    )
                    account["status"] = "old_deleted"

                recipe.title = str(run["requested_title"])
                self.storage.update_recipe_list_run_payload(
                    str(run["id"]),
                    _recipe_list_payload_json(recipe, requested_queries, custom_food_ids),
                )
                for account_key, client in clients.items():
                    active_account_key = account_key
                    account = account_rows[account_key]
                    if str(account["status"]) in {"renamed", "completed"}:
                        continue
                    remote_id = str(account["new_remote_id"])
                    account_recipe = _recipe_for_account(recipe, account_key, custom_food_ids)
                    await self._save_recipe_meta_with_readback(client, account_recipe, remote_id)
                    await self._verify_remote_recipe(client, account_key, remote_id, account_recipe)
                    self.storage.update_recipe_list_run_account(
                        str(run["id"]),
                        account_key,
                        "renamed",
                        new_remote_id=remote_id,
                    )
                    account["status"] = "renamed"
            except Exception as exc:  # noqa: BLE001 - old recipes may already be gone, so preserve new identities.
                message = user_safe_error_message(exc)
                if active_account_key is not None:
                    active_account = account_rows[active_account_key]
                    self.storage.update_recipe_list_run_account(
                        str(run["id"]),
                        active_account_key,
                        str(active_account["status"]),
                        new_remote_id=(
                            str(active_account["new_remote_id"])
                            if active_account.get("new_remote_id")
                            else None
                        ),
                        error=message,
                    )
                self.storage.update_recipe_list_run(str(run["id"]), "recovery_pending", error=message)
                raise FatSecretError(
                    "FatSecret уже сохранил часть замены. Новые remote ID и этап операции сохранены; "
                    f"повтори создание для безопасного продолжения. {message}"
                ) from exc

            self.storage.update_recipe_list_run(str(run["id"]), "remote_complete")
            completed_run = self.storage.recipe_list_run(str(run["id"]))
            if completed_run is None:
                raise FatSecretError("Журнал создания рецепта потерян перед локальной фиксацией.")
            recipe, _, _ = _recipe_list_payload(str(completed_run["payload_json"]))
            return self._finalize_recipe_list_run(completed_run, recipe)
        finally:
            await self._close_clients(clients)

    def _recipe_list_result_from_run(self, run: dict[str, object]) -> RecipeCreateResult:
        """Build the stable public result for a completed recipe-list journal entry."""
        accounts = list(run.get("accounts") or [])
        results = [
            AccountSyncResult(
                str(account["account_key"]),
                str(account["new_remote_id"]),
                True,
                "создан",
            )
            for account in accounts
        ]
        replaced_recipe_id = str(run["replaced_recipe_id"]) if run.get("replaced_recipe_id") else None
        replacement_results = (
            [
                AccountSyncResult(
                    str(account["account_key"]),
                    str(account["new_remote_id"]),
                    True,
                    "старый рецепт удален",
                )
                for account in accounts
            ]
            if replaced_recipe_id is not None
            else []
        )
        temporary_title = str(run["temporary_title"])
        final_title = str(run["requested_title"])
        rename_results = (
            [
                AccountSyncResult(
                    str(account["account_key"]),
                    str(account["new_remote_id"]),
                    True,
                    "переименован",
                )
                for account in accounts
            ]
            if temporary_title != final_title
            else []
        )
        return RecipeCreateResult(
            recipe_id=str(run["canonical_recipe_id"] or run["candidate_recipe_id"]),
            results=results,
            title=final_title,
            temporary_title=temporary_title if temporary_title != final_title else None,
            replaced_recipe_id=replaced_recipe_id,
            replacement_results=replacement_results,
            rename_results=rename_results,
        )

    async def _prepare_recipe_list_account(
        self,
        run_id: str,
        account: dict[str, object],
        account_key: str,
        client: FatSecretClient,
        recipe: Recipe,
        requested_queries: list[str],
        custom_food_ids: dict[str, dict[str, str]],
    ) -> None:
        """Create, fill, and exactly verify one temporary remote candidate."""
        account_recipe = _recipe_for_account(recipe, account_key, custom_food_ids)
        remote_id = str(account["new_remote_id"]) if account.get("new_remote_id") else ""
        if not remote_id:
            title_matches = [
                summary.remote_id
                for summary in await client.cookbook()
                if normalize_title(summary.title) == normalize_title(account_recipe.title)
            ]
            matching_ids: list[str] = []
            for matching_id in title_matches:
                existing = await client.get_recipe(matching_id)
                if (
                    normalize_title(existing.title) == normalize_title(account_recipe.title)
                    and existing.description == account_recipe.description
                    and existing.portions == account_recipe.portions
                    and existing.prep_time == account_recipe.prep_time
                    and existing.cook_time == account_recipe.cook_time
                ):
                    matching_ids.append(matching_id)
            if len(matching_ids) > 1:
                raise FatSecretError(
                    f"{client.account.label}: найдено несколько временных рецептов «{account_recipe.title}»; "
                    "операция остановлена без нового создания"
                )
            if title_matches and not matching_ids:
                raise FatSecretError(
                    f"{client.account.label}: рецепт «{account_recipe.title}» уже существует, но не принадлежит "
                    "этой незавершённой операции"
                )
            remote_id = (
                matching_ids[0]
                if matching_ids
                else await self._create_recipe_with_readback(client, account_recipe)
            )
            account["new_remote_id"] = remote_id
            account["status"] = "created"
            self.storage.update_recipe_list_run_account(
                run_id,
                account_key,
                "created",
                new_remote_id=remote_id,
            )

        current = await client.get_recipe(remote_id)
        if recipe_fingerprint(current).digest == recipe_fingerprint(account_recipe).digest:
            self.storage.upsert_remote_recipe_snapshot(
                account_key,
                remote_id,
                current,
                recipe_fingerprint(current),
            )
            self.storage.update_recipe_list_run_account(
                run_id,
                account_key,
                "verified",
                new_remote_id=remote_id,
            )
            account["status"] = "verified"
            return

        for ingredient in current.ingredients:
            if not ingredient.remote_ingredient_id:
                raise FatSecretError(
                    f"{client.account.label}: временный рецепт содержит ингредиент без remote ID; "
                    "безопасный повтор остановлен"
                )
            if not await client.delete_ingredient(remote_id, ingredient.remote_ingredient_id):
                raise FatSecretError(
                    f"{client.account.label}: не удалось очистить временный ингредиент «{ingredient.title}»"
                )

        payload_changed = False
        for index, ingredient in enumerate(list(account_recipe.ingredients)):
            requested_query = requested_queries[index] if index < len(requested_queries) else ingredient.title
            canonical_ingredient = recipe.ingredients[index]
            is_custom_food = canonical_ingredient.id in custom_food_ids
            accepted = await self._add_ingredient_with_fallback(
                client,
                remote_id,
                ingredient,
                requested_query,
                allow_legacy_fallback=not is_custom_food,
            )
            if accepted is None:
                raise FatSecretError(f"{client.account.label}: FatSecret не принял ингредиент «{ingredient.title}».")
            if _ingredient_needs_update(ingredient, accepted):
                updated_ingredient = Ingredient(
                    id=ingredient.id,
                    recipe_id=account_recipe.id,
                    food_id=accepted.food_id,
                    title=accepted.title,
                    portion_id=accepted.portion_id or "0",
                    amount=accepted.amount,
                    portion_description=accepted.portion_description,
                    grams=accepted.grams,
                )
                account_recipe.ingredients[index] = updated_ingredient
                if not is_custom_food:
                    recipe.ingredients[index] = updated_ingredient
                    payload_changed = True
        if payload_changed:
            self.storage.update_recipe_list_run_payload(
                run_id,
                _recipe_list_payload_json(recipe, requested_queries, custom_food_ids),
            )
        await self._save_recipe_meta_with_readback(client, account_recipe, remote_id)
        await self._verify_remote_recipe(client, account_key, remote_id, account_recipe)
        self.storage.update_recipe_list_run_account(
            run_id,
            account_key,
            "verified",
            new_remote_id=remote_id,
        )
        account["status"] = "verified"

    async def _rollback_recipe_list_run(
        self,
        run: dict[str, object],
        clients: dict[str, FatSecretClient],
        account_rows: dict[str, dict[str, object]],
        phase_error: tuple[str, Exception],
    ) -> None:
        """Roll back every new candidate while no old recipe has been deleted."""
        failed_account_key, failure = phase_error
        messages = [user_safe_error_message(failure)]
        cleanup_ok = True
        for account_key, account in account_rows.items():
            remote_id = str(account["new_remote_id"]) if account.get("new_remote_id") else ""
            if not remote_id:
                self.storage.update_recipe_list_run_account(
                    str(run["id"]),
                    account_key,
                    "failed",
                    error=(user_safe_error_message(failure) if account_key == failed_account_key else None),
                )
                continue
            deleted, rollback_message = await self._rollback_created_recipe_with_status(
                clients[account_key],
                remote_id,
            )
            cleanup_ok = cleanup_ok and deleted
            messages.append(rollback_message)
            self.storage.update_recipe_list_run_account(
                str(run["id"]),
                account_key,
                "rolled_back" if deleted else "failed",
                new_remote_id=remote_id,
                error=None if deleted else rollback_message,
            )
        self.storage.update_recipe_list_run(
            str(run["id"]),
            "rolled_back" if cleanup_ok else "failed",
            error=user_safe_error_message(failure),
        )
        details = "; ".join(message for message in messages if message)
        raise FatSecretError(
            "FatSecret не создал рецепт во всех подключенных аккаунтах. "
            f"Локальный черновик удален. {details}"
        ) from failure

    def _finalize_recipe_list_run(
        self,
        run: dict[str, object],
        recipe: Recipe,
    ) -> RecipeCreateResult:
        """Finalize verified remote identities locally or leave a retryable recovery marker."""
        remote_ids = {
            str(account["account_key"]): str(account["new_remote_id"])
            for account in list(run.get("accounts") or [])
            if account.get("new_remote_id")
        }
        try:
            self.storage.finalize_recipe_list_run(str(run["id"]), recipe, remote_ids)
        except Exception as exc:
            message = user_safe_error_message(exc)
            self.storage.update_recipe_list_run(str(run["id"]), "recovery_pending", error=message)
            raise FatSecretError(
                "Рецепт уже подтвержден во всех FatSecret аккаунтах, но локальная фиксация не завершилась. "
                f"Повтори создание: remote ID сохранены и повторно рецепт не создастся. {message}"
            ) from exc
        completed = self.storage.recipe_list_run(str(run["id"]))
        if completed is None:
            raise FatSecretError("Локальная фиксация завершилась, но журнал операции не найден.")
        return self._recipe_list_result_from_run(completed)

    async def _rollback_created_recipe(self, client: FatSecretClient, remote_id: str | None) -> str:
        _, message = await self._rollback_created_recipe_with_status(client, remote_id)
        return message

    async def _rollback_created_recipe_with_status(
        self,
        client: FatSecretClient,
        remote_id: str | None,
    ) -> tuple[bool, str]:
        if not remote_id:
            return False, ""
        logger.warning(
            "Recipe rollback started account=%s remote_recipe_id=%s",
            client.account.label,
            remote_id,
        )
        try:
            ok = await self._delete_remote_recipe_confirmed(client, remote_id)
        except Exception as exc:  # noqa: BLE001 - preserve original creation error and report cleanup failure.
            logger.exception(
                "Recipe rollback failed with exception account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
            )
            return (
                False,
                f"{client.account.label}: созданный рецепт {remote_id} не удалось удалить после ошибки: "
                f"{user_safe_error_message(exc)}",
            )
        if ok:
            logger.info(
                "Recipe rollback completed account=%s remote_recipe_id=%s deleted=true",
                client.account.label,
                remote_id,
            )
            return True, f"{client.account.label}: созданный рецепт {remote_id} удален после ошибки."
        logger.error(
            "Recipe rollback completed account=%s remote_recipe_id=%s deleted=false",
            client.account.label,
            remote_id,
        )
        return False, f"{client.account.label}: созданный рецепт {remote_id} не удалось удалить после ошибки."

    async def _delete_remote_recipe_confirmed(
        self,
        client: FatSecretClient,
        remote_id: str,
    ) -> bool:
        """Delete once, resolve ambiguous failures by cookbook readback, and retry only if still present."""
        first_error: Exception | None = None
        try:
            deleted = await client.delete_recipe(remote_id)
            if deleted:
                return True
            first_error = FatSecretError(f"{client.account.label}: recipe delete returned false")
        except Exception as exc:  # noqa: BLE001 - ambiguity is resolved by authoritative readback below.
            first_error = exc
            logger.exception(
                "Recipe delete mutation ambiguous account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
            )

        try:
            still_present = any(item.remote_id == remote_id for item in await client.cookbook())
        except Exception as read_error:  # noqa: BLE001
            logger.exception(
                "Recipe delete readback unavailable account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
            )
            raise FatSecretError(
                f"{client.account.label}: состояние удаления {remote_id} не удалось подтвердить; "
                "локальная привязка сохранена"
            ) from read_error
        if not still_present:
            return True
        if first_error is not None and not _is_ambiguous_mutation_error(first_error):
            raise first_error

        try:
            retried = await client.delete_recipe(remote_id)
            if not retried:
                raise FatSecretError(f"{client.account.label}: повторное удаление вернуло false")
        except Exception as retry_error:  # noqa: BLE001
            logger.exception(
                "Recipe delete retry failed account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
            )
            first_error = retry_error
        try:
            still_present = any(item.remote_id == remote_id for item in await client.cookbook())
        except Exception as read_error:  # noqa: BLE001
            logger.exception(
                "Recipe delete final readback unavailable account=%s remote_recipe_id=%s",
                client.account.label,
                remote_id,
            )
            raise FatSecretError(
                f"{client.account.label}: состояние удаления {remote_id} после повтора неопределённо; "
                "локальная привязка сохранена"
            ) from read_error
        if not still_present:
            return True
        if first_error is not None:
            raise first_error
        raise FatSecretError(f"{client.account.label}: рецепт {remote_id} остался после удаления и проверки")

    async def hydrate_recipe_from_remote(self, recipe_id: str) -> Recipe | None:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            return None
        if recipe.ingredients:
            return recipe

        clients = self._build_clients(recipe.group_id)
        try:
            for account_key, remote_id in recipe.remote_ids.items():
                client = clients.get(account_key)
                if client is None:
                    continue
                remote = await client.get_recipe(remote_id)
                remote.ingredients = await self._normalize_recipe_ingredients(client, remote.ingredients)
                remote.ingredients = _copy_remote_ingredients(recipe_id, remote.ingredients)
                self.storage.update_recipe_from_remote(
                    recipe_id=recipe_id,
                    title=remote.title or recipe.title,
                    description=remote.description,
                    portions=remote.portions,
                    prep_time=remote.prep_time,
                    cook_time=remote.cook_time,
                    steps=remote.steps,
                )
                self.storage.replace_ingredients(recipe_id, remote.ingredients)
                return self.storage.get_recipe(recipe_id)
        finally:
            await self._close_clients(clients)
        return recipe

    async def sync_recipe(self, recipe_id: str) -> list[AccountSyncResult]:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")
        if not recipe.remote_ids:
            raise FatSecretError("У рецепта нет привязки к FatSecret. Нажми «Обновить» и попробуй снова.")
        return await self.sync_recipe_from_source(recipe_id, next(iter(recipe.remote_ids)))

    async def sync_recipe_from_source(self, recipe_id: str, source_account_key: str) -> list[AccountSyncResult]:
        """Read a recipe from one FatSecret account and propagate it to every connected account."""
        sync_run = uuid.uuid4().hex[:12]
        logger.info(
            "Recipe sync started run=%s local_recipe_id=%s source_account_key=%s mode=stored",
            sync_run,
            recipe_id,
            source_account_key,
        )
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")

        source_remote_id = recipe.remote_ids.get(source_account_key)
        if source_remote_id is None:
            raise FatSecretError("Выбранный аккаунт не содержит этот рецепт. Обнови список рецептов.")

        results: list[AccountSyncResult] = []
        clients = self._build_clients(recipe.group_id)
        try:
            source_client = clients.get(source_account_key)
            if source_client is None:
                raise FatSecretError("Аккаунт-источник больше не подключен.")

            source_snapshot = await self._load_recipe_source_snapshot(source_client, source_remote_id, recipe.id)
            source_recipe = source_snapshot.transport
            display_recipe = source_snapshot.display
            source_fingerprint = recipe_fingerprint(source_recipe)
            self.storage.upsert_remote_recipe_snapshot(
                source_account_key,
                source_remote_id,
                source_recipe,
                source_fingerprint,
            )
            logger.info(
                "Recipe sync source loaded run=%s local_recipe_id=%s source_account=%s source_remote_id=%s "
                "title=%r raw_ingredients=%d display_ingredients=%d",
                sync_run,
                recipe_id,
                source_client.account.label,
                source_remote_id,
                source_recipe.title,
                len(source_recipe.ingredients),
                len(display_recipe.ingredients),
            )
            source_recipe.title = source_recipe.title or recipe.title
            display_recipe.title = source_recipe.title
            self.storage.update_recipe_from_remote(
                recipe_id=recipe.id,
                title=display_recipe.title,
                description=display_recipe.description,
                portions=display_recipe.portions,
                prep_time=display_recipe.prep_time,
                cook_time=display_recipe.cook_time,
                steps=display_recipe.steps,
            )
            self.storage.replace_ingredients(recipe.id, display_recipe.ingredients)
            recipe = self.storage.get_recipe(recipe.id) or display_recipe
            recipe.steps = list(source_recipe.steps)
            recipe.ingredients = source_recipe.ingredients

            for account_key, client in clients.items():
                remote_id = recipe.remote_ids.get(account_key)
                created_target = False
                try:
                    logger.info(
                        "Recipe sync account started run=%s local_recipe_id=%s account=%s account_key=%s "
                        "remote_recipe_id=%s is_source=%s",
                        sync_run,
                        recipe.id,
                        client.account.label,
                        account_key,
                        remote_id or "-",
                        account_key == source_account_key,
                    )
                    if account_key == source_account_key:
                        self.storage.mark_synced(recipe.id, account_key, source_remote_id, recipe.version)
                        results.append(AccountSyncResult(account_key, source_remote_id, True, "источник"))
                        logger.info(
                            "Recipe sync account completed run=%s account=%s remote_recipe_id=%s result=source_updated",
                            sync_run,
                            client.account.label,
                            source_remote_id,
                        )
                        continue
                    created_target = remote_id is None
                    target_recipe = await self._prepare_target_recipe(
                        source_client,
                        client,
                        source_account_key,
                        account_key,
                        recipe,
                        {},
                    )
                    remote_id, stats, _ = await self._synchronize_target_recipe(
                        client,
                        account_key,
                        target_recipe,
                        remote_id,
                        persist_mapping=True,
                    )
                    _remember_remote_recipe_id(recipe, account_key, remote_id)
                    self.storage.mark_synced(recipe.id, account_key, remote_id, recipe.version)
                    results.append(AccountSyncResult(account_key, remote_id, True, stats.message()))
                    logger.info(
                        "Recipe sync account completed run=%s account=%s remote_recipe_id=%s created_target=%s "
                        "added=%d updated=%d unchanged=%d deleted=%d",
                        sync_run,
                        client.account.label,
                        remote_id,
                        created_target,
                        stats.added,
                        stats.updated,
                        stats.unchanged,
                        stats.deleted,
                    )
                except Exception as exc:  # noqa: BLE001 - keep per-account sync isolated.
                    if isinstance(exc, _TargetSyncFailure):
                        remote_id = exc.remote_id
                        created_target = False
                        if not exc.rolled_back:
                            _remember_remote_recipe_id(recipe, account_key, remote_id)
                            self.storage.set_remote_recipe_id(
                                recipe.id,
                                account_key,
                                remote_id,
                                last_synced_version=0,
                            )
                    logger.exception(
                        "Recipe sync account failed run=%s local_recipe_id=%s account=%s account_key=%s "
                        "remote_recipe_id=%s created_target=%s",
                        sync_run,
                        recipe.id,
                        client.account.label,
                        account_key,
                        remote_id or "-",
                        created_target,
                    )
                    message = user_safe_error_message(exc)
                    if created_target and remote_id:
                        rolled_back, rollback_message = await self._rollback_created_recipe_with_status(client, remote_id)
                        if rollback_message:
                            message = f"{message} {rollback_message}"
                        if rolled_back:
                            _forget_remote_recipe_id(recipe, account_key, remote_id)
                        else:
                            self.storage.set_remote_recipe_id(recipe.id, account_key, remote_id, last_synced_version=0)
                    self.storage.record_sync(recipe.id, account_key, "error", message)
                    results.append(AccountSyncResult(account_key, remote_id, False, message))
        finally:
            await self._close_clients(clients)
            logger.info(
                "Recipe sync finished run=%s local_recipe_id=%s results=%s",
                sync_run,
                recipe_id,
                [(result.account_key, result.remote_recipe_id, result.ok) for result in results],
            )
        return results

    async def sync_live_recipe_from_source(
        self,
        recipe_ref: Recipe,
        source_account_key: str,
    ) -> tuple[Recipe, list[AccountSyncResult]]:
        """Read a live recipe from one FatSecret account and propagate it without persisting local recipe rows."""
        sync_run = uuid.uuid4().hex[:12]
        logger.info(
            "Recipe sync started run=%s local_recipe_id=%s source_account_key=%s mode=live",
            sync_run,
            recipe_ref.id,
            source_account_key,
        )
        source_remote_id = recipe_ref.remote_ids.get(source_account_key)
        if source_remote_id is None:
            raise FatSecretError("Выбранный аккаунт не содержит этот рецепт. Обнови список рецептов.")

        results: list[AccountSyncResult] = []
        clients = self._build_clients(recipe_ref.group_id)
        try:
            source_client = clients.get(source_account_key)
            if source_client is None:
                raise FatSecretError("Аккаунт-источник больше не подключен.")

            source_snapshot = await self._load_recipe_source_snapshot(source_client, source_remote_id, recipe_ref.id)
            transport_recipe = source_snapshot.transport
            recipe = source_snapshot.display
            self.storage.upsert_remote_recipe_snapshot(
                source_account_key,
                source_remote_id,
                transport_recipe,
                recipe_fingerprint(transport_recipe),
            )
            logger.info(
                "Recipe sync source loaded run=%s local_recipe_id=%s source_account=%s source_remote_id=%s "
                "title=%r raw_ingredients=%d display_ingredients=%d",
                sync_run,
                recipe_ref.id,
                source_client.account.label,
                source_remote_id,
                transport_recipe.title,
                len(transport_recipe.ingredients),
                len(recipe.ingredients),
            )
            recipe.title = recipe.title or recipe_ref.title
            recipe.group_id = recipe_ref.group_id
            remote_ids = dict(recipe_ref.remote_ids)
            remote_ids_by_account = {
                account_key: list(remote_ids)
                for account_key, remote_ids in recipe_ref.remote_ids_by_account.items()
            }
            recipe.remote_ids = remote_ids
            recipe.remote_ids_by_account = remote_ids_by_account
            transport_recipe.title = recipe.title
            transport_recipe.description = recipe.description
            transport_recipe.group_id = recipe.group_id
            transport_recipe.remote_ids = remote_ids
            transport_recipe.remote_ids_by_account = remote_ids_by_account

            for account_key, client in clients.items():
                remote_id = recipe.remote_ids.get(account_key)
                created_target = False
                try:
                    logger.info(
                        "Recipe sync account started run=%s local_recipe_id=%s account=%s account_key=%s "
                        "remote_recipe_id=%s is_source=%s",
                        sync_run,
                        recipe.id,
                        client.account.label,
                        account_key,
                        remote_id or "-",
                        account_key == source_account_key,
                    )
                    if account_key == source_account_key:
                        results.append(AccountSyncResult(account_key, source_remote_id, True, "источник"))
                        logger.info(
                            "Recipe sync account completed run=%s account=%s remote_recipe_id=%s result=source_updated",
                            sync_run,
                            client.account.label,
                            source_remote_id,
                        )
                        continue
                    created_target = remote_id is None
                    target_recipe = await self._prepare_target_recipe(
                        source_client,
                        client,
                        source_account_key,
                        account_key,
                        transport_recipe,
                        {},
                    )
                    old_remote_id = remote_id
                    remote_id, stats, _ = await self._synchronize_target_recipe(
                        client,
                        account_key,
                        target_recipe,
                        remote_id,
                        persist_mapping=False,
                    )
                    if old_remote_id and old_remote_id != remote_id:
                        _forget_remote_recipe_id(transport_recipe, account_key, old_remote_id)
                        _forget_remote_recipe_id(recipe, account_key, old_remote_id)
                    _remember_remote_recipe_id(transport_recipe, account_key, remote_id)
                    _remember_remote_recipe_id(recipe, account_key, remote_id)
                    results.append(AccountSyncResult(account_key, remote_id, True, stats.message()))
                    logger.info(
                        "Recipe sync account completed run=%s account=%s remote_recipe_id=%s created_target=%s "
                        "added=%d updated=%d unchanged=%d deleted=%d",
                        sync_run,
                        client.account.label,
                        remote_id,
                        created_target,
                        stats.added,
                        stats.updated,
                        stats.unchanged,
                        stats.deleted,
                    )
                except Exception as exc:  # noqa: BLE001 - keep per-account sync isolated.
                    if isinstance(exc, _TargetSyncFailure):
                        remote_id = exc.remote_id
                        created_target = False
                        if not exc.rolled_back:
                            _remember_remote_recipe_id(transport_recipe, account_key, remote_id)
                            _remember_remote_recipe_id(recipe, account_key, remote_id)
                    logger.exception(
                        "Recipe sync account failed run=%s local_recipe_id=%s account=%s account_key=%s "
                        "remote_recipe_id=%s created_target=%s",
                        sync_run,
                        recipe.id,
                        client.account.label,
                        account_key,
                        remote_id or "-",
                        created_target,
                    )
                    message = user_safe_error_message(exc)
                    if created_target and remote_id:
                        rolled_back, rollback_message = await self._rollback_created_recipe_with_status(client, remote_id)
                        if rollback_message:
                            message = f"{message} {rollback_message}"
                        if rolled_back:
                            _forget_remote_recipe_id(transport_recipe, account_key, remote_id)
                    results.append(AccountSyncResult(account_key, remote_id, False, message))
        finally:
            await self._close_clients(clients)
            logger.info(
                "Recipe sync finished run=%s local_recipe_id=%s results=%s",
                sync_run,
                recipe_ref.id,
                [(result.account_key, result.remote_recipe_id, result.ok) for result in results],
            )
        return recipe, results

    async def delete_recipe_everywhere(self, recipe_id: str) -> list[AccountSyncResult]:
        """Delete one recipe from every FatSecret account where it is mapped."""
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")
        clients = self._build_clients(recipe.group_id if recipe else None)
        try:
            return await self._delete_recipe_with_clients(recipe_id, clients)
        finally:
            await self._close_clients(clients)

    async def delete_recipes_everywhere(self, recipe_ids: list[str]) -> dict[str, list[AccountSyncResult]]:
        """Delete several recipes from all mapped FatSecret accounts."""
        recipe = self.storage.get_recipe(recipe_ids[0]) if recipe_ids else None
        clients = self._build_clients(recipe.group_id if recipe else None)
        results: dict[str, list[AccountSyncResult]] = {}
        try:
            for recipe_id in recipe_ids:
                try:
                    results[recipe_id] = await self._delete_recipe_with_clients(recipe_id, clients)
                except Exception as exc:  # noqa: BLE001 - keep batch deletion moving.
                    results[recipe_id] = [
                        AccountSyncResult("local", None, False, user_safe_error_message(exc))
                    ]
        finally:
            await self._close_clients(clients)
        return results

    async def delete_live_recipe_everywhere(self, recipe_ref: Recipe) -> list[AccountSyncResult]:
        """Delete one in-memory recipe reference from every mapped FatSecret account."""
        clients = self._build_clients(recipe_ref.group_id)
        try:
            return await self._delete_live_recipe_ref_with_clients(recipe_ref, clients)
        finally:
            await self._close_clients(clients)

    async def delete_live_recipes_everywhere(self, recipe_refs: list[Recipe]) -> dict[str, list[AccountSyncResult]]:
        """Delete several in-memory recipe references from FatSecret."""
        results: dict[str, list[AccountSyncResult]] = {}
        for recipe in recipe_refs:
            try:
                results[recipe.id] = await self.delete_live_recipe_everywhere(recipe)
            except Exception as exc:  # noqa: BLE001 - keep batch deletion moving.
                results[recipe.id] = [AccountSyncResult("local", None, False, user_safe_error_message(exc))]
        return results

    async def _delete_recipe_with_clients(
        self,
        recipe_id: str,
        clients: dict[str, FatSecretClient],
    ) -> list[AccountSyncResult]:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")
        if not recipe.remote_ids:
            self.storage.delete_recipe(recipe.id)
            return [AccountSyncResult("local", None, True, "нет привязок к FatSecret; удалил локально")]

        results: list[AccountSyncResult] = []
        deleted_account_keys: list[str] = []
        for account_key, remote_id in list(recipe.remote_ids.items()):
            client = clients.get(account_key)
            if client is None:
                message = "FatSecret аккаунт больше не подключен"
                self.storage.record_sync(recipe.id, account_key, "error", message)
                results.append(AccountSyncResult(account_key, remote_id, False, message))
                continue
            try:
                ok = await self._delete_remote_recipe_confirmed(client, remote_id)
                if not ok:
                    raise FatSecretError(f"{client.account.label}: recipe delete returned false")
                self.storage.record_sync(recipe.id, account_key, "ok", f"deleted remote recipe {remote_id}")
                deleted_account_keys.append(account_key)
                results.append(AccountSyncResult(account_key, remote_id, True, "удален в FatSecret"))
            except Exception as exc:  # noqa: BLE001 - keep per-account deletion isolated.
                logger.exception(
                    "Recipe delete failed account=%s remote_recipe_id=%s",
                    client.account.label,
                    remote_id,
                )
                self.storage.record_sync(recipe.id, account_key, "error", str(exc))
                results.append(AccountSyncResult(account_key, remote_id, False, user_safe_error_message(exc)))

        for account_key in deleted_account_keys:
            self.storage.delete_remote_recipe_id(recipe.id, account_key)
        if deleted_account_keys and not self.storage.remote_ids(recipe.id):
            self.storage.delete_recipe(recipe.id)
        return results

    async def _delete_live_recipe_ref_with_clients(
        self,
        recipe_ref: Recipe,
        clients: dict[str, FatSecretClient],
    ) -> list[AccountSyncResult]:
        results: list[AccountSyncResult] = []
        account_keys = set(recipe_ref.remote_ids) | set(recipe_ref.remote_ids_by_account)
        for account_key in sorted(account_keys):
            client = clients.get(account_key)
            for remote_id in _remote_ids_for_account(recipe_ref, account_key):
                if client is None:
                    results.append(AccountSyncResult(account_key, remote_id, False, "FatSecret аккаунт больше не подключен"))
                    continue
                try:
                    ok = await self._delete_remote_recipe_confirmed(client, remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe delete returned false")
                    self.storage.remove_remote_recipe_mapping(account_key, remote_id)
                    results.append(AccountSyncResult(account_key, remote_id, True, "удален в FatSecret"))
                except Exception as exc:  # noqa: BLE001 - keep per-account deletion isolated.
                    logger.exception(
                        "Live recipe delete failed account=%s remote_recipe_id=%s",
                        client.account.label,
                        remote_id,
                    )
                    results.append(AccountSyncResult(account_key, remote_id, False, user_safe_error_message(exc)))
        return results

    async def _ensure_remote_recipe(
        self,
        client: FatSecretClient,
        recipe: Recipe,
        remote_id: str | None,
    ) -> str:
        if remote_id:
            logger.info(
                "Recipe target reuse account=%s remote_recipe_id=%s title=%r",
                client.account.label,
                remote_id,
                recipe.title,
            )
            return remote_id
        logger.info("Recipe target create required account=%s title=%r", client.account.label, recipe.title)
        created_remote_id = await client.create_recipe(recipe)
        logger.info(
            "Recipe target created account=%s remote_recipe_id=%s title=%r",
            client.account.label,
            created_remote_id,
            recipe.title,
        )
        return created_remote_id

    async def _synchronize_target_recipe(
        self,
        client: FatSecretClient,
        account_key: str,
        expected: Recipe,
        remote_id: str | None,
        *,
        persist_mapping: bool,
    ) -> tuple[str, IngredientSyncStats, bool]:
        """Create or transactionally swap a target and return only after exact readback."""
        if remote_id is None:
            created_id = await self._create_recipe_with_readback(client, expected)
            try:
                stats = await self._sync_ingredients_with_readback(client, expected, created_id)
                await self._save_recipe_meta_with_readback(client, expected, created_id)
                await self._verify_remote_recipe(client, account_key, created_id, expected)
                return created_id, stats, True
            except Exception as exc:
                rolled_back, rollback_message = await self._rollback_created_recipe_with_status(client, created_id)
                message = user_safe_error_message(exc)
                if rollback_message:
                    message = f"{message} {rollback_message}"
                raise _TargetSyncFailure(message, created_id, rolled_back) from exc

        expected_fingerprint = recipe_fingerprint(expected)
        resumable = next(
            (
                run
                for run in self.storage.incomplete_recipe_swap_runs()
                if run.get("recipe_id") == expected.id
                and run.get("account_key") == account_key
                and run.get("status") == "old_deleted"
                and run.get("new_remote_id")
            ),
            None,
        )
        if resumable is not None:
            resumed_id = str(resumable["new_remote_id"])
            await self._save_recipe_meta_with_readback(client, expected, resumed_id)
            actual = await self._verify_remote_recipe(client, account_key, resumed_id, expected)
            if persist_mapping:
                self.storage.complete_recipe_swap(
                    str(resumable["id"]),
                    expected.id,
                    account_key,
                    resumed_id,
                    expected.version,
                    actual,
                    recipe_fingerprint(actual),
                )
            else:
                self.storage.update_recipe_swap_run(str(resumable["id"]), "completed", new_remote_id=resumed_id)
            return resumed_id, IngredientSyncStats(unchanged=len(expected.ingredients)), True

        current = await client.get_recipe(remote_id)
        current_fingerprint = recipe_fingerprint(current)
        if current_fingerprint.digest == expected_fingerprint.digest:
            self.storage.upsert_remote_recipe_snapshot(
                account_key,
                remote_id,
                current,
                current_fingerprint,
            )
            return remote_id, IngredientSyncStats(unchanged=len(expected.ingredients)), False

        temporary_title = f"{expected.title} · sync {uuid.uuid4().hex[:6]}"
        temporary = _copy_recipe_from_remote(expected.id, expected)
        temporary.group_id = expected.group_id
        temporary.title = temporary_title
        run_id = self.storage.create_recipe_swap_run(
            expected.id,
            account_key,
            remote_id,
            temporary_title,
            expected.title,
        )
        new_remote_id: str | None = None
        old_deleted = False
        try:
            new_remote_id = await self._create_recipe_with_readback(client, temporary)
            self.storage.update_recipe_swap_run(run_id, "created", new_remote_id=new_remote_id)
            stats = await self._sync_ingredients_with_readback(client, temporary, new_remote_id)
            await self._save_recipe_meta_with_readback(client, temporary, new_remote_id)
            await self._verify_remote_recipe(client, account_key, new_remote_id, temporary)
            self.storage.update_recipe_swap_run(run_id, "verified", new_remote_id=new_remote_id)

            await self._delete_remote_recipe_confirmed(client, remote_id)
            old_deleted = True
            self.storage.update_recipe_swap_run(run_id, "old_deleted", new_remote_id=new_remote_id)

            await self._save_recipe_meta_with_readback(client, expected, new_remote_id)
            actual = await self._verify_remote_recipe(client, account_key, new_remote_id, expected)
            actual_fingerprint = recipe_fingerprint(actual)
            if persist_mapping:
                self.storage.complete_recipe_swap(
                    run_id,
                    expected.id,
                    account_key,
                    new_remote_id,
                    expected.version,
                    actual,
                    actual_fingerprint,
                )
            else:
                self.storage.update_recipe_swap_run(run_id, "completed", new_remote_id=new_remote_id)
            return new_remote_id, stats, True
        except Exception as exc:
            if new_remote_id and not old_deleted:
                rolled_back, _ = await self._rollback_created_recipe_with_status(client, new_remote_id)
                self.storage.update_recipe_swap_run(
                    run_id,
                    "rolled_back" if rolled_back else "failed",
                    new_remote_id=new_remote_id,
                    error=user_safe_error_message(exc),
                )
            else:
                self.storage.update_recipe_swap_run(
                    run_id,
                    "old_deleted" if old_deleted else "failed",
                    new_remote_id=new_remote_id,
                    error=user_safe_error_message(exc),
                )
            raise

    async def _add_synced_ingredient(
        self,
        client: FatSecretClient,
        remote_id: str,
        source_ingredient: Ingredient,
        target_ingredient: Ingredient,
        *,
        source_client: FatSecretClient | None,
        source_account_key: str | None,
        target_account_key: str | None,
    ) -> Ingredient | None:
        action_error_fallback: Callable[[], Awaitable[Ingredient | None]] | None = None
        if source_client is not None and source_account_key is not None and target_account_key is not None:

            async def clone_custom_food() -> Ingredient | None:
                return await self._clone_custom_food_ingredient(
                    source_client,
                    client,
                    source_account_key,
                    target_account_key,
                    source_ingredient,
                    target_ingredient,
                )

            action_error_fallback = clone_custom_food
        return await self._add_ingredient_with_fallback(
            client,
            remote_id,
            target_ingredient,
            source_ingredient.title,
            action_error_fallback=action_error_fallback,
            prefer_original=True,
            allow_legacy_fallback=False,
        )

    async def _sync_ingredients(
        self,
        client: FatSecretClient,
        recipe: Recipe,
        remote_id: str,
        *,
        source_client: FatSecretClient | None = None,
        source_account_key: str | None = None,
        target_account_key: str | None = None,
    ) -> IngredientSyncStats:
        logger.info(
            "Ingredient sync started account=%s target_remote_recipe_id=%s local_recipe_id=%s title=%r "
            "source_account_key=%s target_account_key=%s source_ingredients=%d",
            client.account.label,
            remote_id,
            recipe.id,
            recipe.title,
            source_account_key or "-",
            target_account_key or "-",
            len(recipe.ingredients),
        )
        remote = await client.get_recipe(remote_id)
        logger.info(
            "Ingredient sync target loaded account=%s target_remote_recipe_id=%s target_ingredients=%d",
            client.account.label,
            remote_id,
            len(remote.ingredients),
        )
        used_target_ids: set[str] = set()
        added = 0
        updated = 0
        unchanged = 0
        deleted = 0
        for source_ingredient in recipe.ingredients:
            ingredient = self._mapped_custom_food_ingredient(
                source_account_key,
                target_account_key,
                source_ingredient,
            )
            logger.info(
                "Ingredient sync item account=%s target_remote_recipe_id=%s source_food_id=%s mapped_food_id=%s "
                "source_iid=%s title=%r portion_id=%s amount=%s portion=%r grams=%s",
                client.account.label,
                remote_id,
                source_ingredient.food_id,
                ingredient.food_id,
                source_ingredient.remote_ingredient_id or "0",
                ingredient.title,
                ingredient.portion_id or "0",
                ingredient.amount,
                ingredient.portion_description,
                ingredient.grams,
            )
            target = _find_matching_ingredient(remote.ingredients, ingredient, used_target_ids)
            if target is None:
                logger.info(
                    "Ingredient sync decision account=%s target_remote_recipe_id=%s food_id=%s decision=add",
                    client.account.label,
                    remote_id,
                    ingredient.food_id,
                )
                target_ingredient = _ingredient_for_target_create(ingredient)
                accepted_ingredient = await self._add_synced_ingredient(
                    client,
                    remote_id,
                    source_ingredient,
                    target_ingredient,
                    source_client=source_client,
                    source_account_key=source_account_key,
                    target_account_key=target_account_key,
                )
                ok = accepted_ingredient is not None
                added += 1
            elif not _ingredient_needs_update(target, ingredient):
                used_target_ids.add(_ingredient_identity(target))
                unchanged += 1
                logger.info(
                    "Ingredient sync decision account=%s target_remote_recipe_id=%s food_id=%s target_iid=%s "
                    "decision=unchanged",
                    client.account.label,
                    remote_id,
                    ingredient.food_id,
                    _ingredient_identity(target),
                )
                continue
            else:
                used_target_ids.add(_ingredient_identity(target))
                logger.info(
                    "Ingredient sync decision account=%s target_remote_recipe_id=%s food_id=%s target_iid=%s "
                    "decision=update old_portion_id=%s old_amount=%s old_grams=%s",
                    client.account.label,
                    remote_id,
                    ingredient.food_id,
                    _ingredient_identity(target),
                    target.portion_id or "0",
                    target.amount,
                    target.grams,
                )
                target_ingredient = Ingredient(
                    id=target.id,
                    recipe_id=remote_id,
                    food_id=ingredient.food_id,
                    title=ingredient.title,
                    portion_id=ingredient.portion_id,
                    amount=ingredient.amount,
                    portion_description=ingredient.portion_description,
                    remote_ingredient_id=_ingredient_identity(target),
                    grams=ingredient.grams,
                )
                accepted_ingredient = await self._add_synced_ingredient(
                    client,
                    remote_id,
                    source_ingredient,
                    target_ingredient,
                    source_client=source_client,
                    source_account_key=source_account_key,
                    target_account_key=target_account_key,
                )
                ok = accepted_ingredient is not None
                updated += 1
            if not ok:
                logger.error(
                    "Ingredient sync item rejected account=%s target_remote_recipe_id=%s source_food_id=%s "
                    "mapped_food_id=%s title=%r",
                    client.account.label,
                    remote_id,
                    source_ingredient.food_id,
                    ingredient.food_id,
                    ingredient.title,
                )
                raise FatSecretError(f"{client.account.label}: FatSecret не принял ингредиент «{ingredient.title}».")
            logger.info(
                "Ingredient sync item accepted account=%s target_remote_recipe_id=%s source_food_id=%s "
                "accepted_food_id=%s accepted_portion_id=%s accepted_amount=%s",
                client.account.label,
                remote_id,
                source_ingredient.food_id,
                accepted_ingredient.food_id,
                accepted_ingredient.portion_id or "0",
                accepted_ingredient.amount,
            )
        extras = [target for target in remote.ingredients if _ingredient_identity(target) not in used_target_ids]
        logger.info(
            "Ingredient sync extras account=%s target_remote_recipe_id=%s count=%d iids=%s",
            client.account.label,
            remote_id,
            len(extras),
            [_ingredient_identity(target) for target in extras],
        )
        for target in extras:
            remote_ingredient_id = target.remote_ingredient_id
            if not remote_ingredient_id:
                raise FatSecretError(
                    f"{client.account.label}: у лишнего ингредиента «{target.title}» нет FatSecret iid; "
                    "точная синхронизация невозможна"
                )
            logger.info(
                "Ingredient sync extra delete account=%s target_remote_recipe_id=%s iid=%s food_id=%s title=%r",
                client.account.label,
                remote_id,
                remote_ingredient_id,
                target.food_id,
                target.title,
            )
            if not await client.delete_ingredient(remote_id, remote_ingredient_id):
                raise FatSecretError(
                    f"{client.account.label}: FatSecret не удалил лишний ингредиент «{target.title}»"
                )
            deleted += 1
        stats = IngredientSyncStats(added=added, updated=updated, unchanged=unchanged, deleted=deleted)
        logger.info(
            "Ingredient sync completed account=%s target_remote_recipe_id=%s added=%d updated=%d unchanged=%d deleted=%d",
            client.account.label,
            remote_id,
            stats.added,
            stats.updated,
            stats.unchanged,
            stats.deleted,
        )
        return stats
