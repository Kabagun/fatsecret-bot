from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fatsecret_client import FatSecretClient, FatSecretError
from .models import FatSecretAccountConfig, FatSecretDeviceConfig, FoodSearchResult, Ingredient, Recipe
from .storage import Storage, normalize_title

logger = logging.getLogger(__name__)
PORTION_UNIT_RE = re.compile(r"^\s*(\d+(?:[\.,]\d+)?)\s*(?:г|гр|g|gram|грам|мл|ml)\b", re.IGNORECASE)
SEARCH_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


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
    extras: int = 0

    def message(self) -> str:
        """Return a compact user-facing summary of ingredient propagation."""
        parts: list[str] = []
        if self.added:
            parts.append(f"добавлено ингредиентов: {self.added}")
        if self.updated:
            parts.append(f"обновлено ингредиентов: {self.updated}")
        if self.unchanged:
            parts.append(f"без изменений: {self.unchanged}")
        if self.extras:
            parts.append(f"лишних ингредиентов в целевом рецепте: {self.extras} (не удалял)")
        return "; ".join(parts) if parts else "ингредиентов нет"


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


@dataclass(frozen=True)
class RecipeListDraft:
    items: list[ResolvedRecipeListItem]
    unresolved: list[RecipeListItem]
    steps: list[str] | None = None


@dataclass(frozen=True)
class RecipeCreateResult:
    recipe_id: str
    results: list[AccountSyncResult]


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


def _food_result_rank(query: str, result: FoodSearchResult) -> tuple[int, int, int, int, int, int, int, int, int, str]:
    missing_terms, exact_title, title_missing_terms, extra_title_words, all_terms_present, all_terms_as_words, title_words, title_length, normalized_title = _rank_text(
        query,
        result.title,
        _food_search_text(result),
    )
    own_priority = 0 if result.is_own else 1
    if len(_search_tokens(query)) <= 1:
        return (
            missing_terms,
            title_missing_terms,
            own_priority,
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
        exact_title,
        own_priority,
        extra_title_words,
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
) -> tuple[int, int, int, int, int, int, int, int, int, int, str]:
    missing_terms, exact_title, title_missing_terms, extra_title_words, all_terms_present, all_terms_as_words, title_words, title_length, normalized_title = _rank_text(
        query,
        item.ingredient.title,
        _resolved_search_text(item),
    )
    source_priority = 0 if item.source == "часто использовался" else 1
    if len(_search_tokens(query)) <= 1:
        return (
            missing_terms,
            title_missing_terms,
            source_priority,
            -item.usage_count,
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
        exact_title,
        source_priority,
        -item.usage_count,
        extra_title_words,
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


def _portion_unit_size(description: str) -> Decimal | None:
    match = PORTION_UNIT_RE.search(description.replace("\xa0", " "))
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None


def _amount_for_grams(grams: Decimal, portion_description: str) -> Decimal:
    unit_size = _portion_unit_size(portion_description)
    if unit_size is None or unit_size == 0:
        return grams
    return grams / unit_size


def _gram_portion_amount(grams: Decimal) -> Decimal:
    return _amount_for_grams(grams, "100г")


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


def _ingredient_with_search_result(ingredient: Ingredient, result: FoodSearchResult) -> Ingredient:
    return Ingredient(
        id=ingredient.id,
        recipe_id=ingredient.recipe_id,
        food_id=result.food_id,
        title=result.title or ingredient.title,
        portion_id="0",
        amount=ingredient.amount,
        portion_description="100г",
        remote_ingredient_id=ingredient.remote_ingredient_id,
    )


class RecipeSyncEngine:
    def __init__(self, storage: Storage, device: FatSecretDeviceConfig, timezone: str = "Europe/Minsk") -> None:
        self.storage = storage
        self.device = device
        self.timezone = timezone

    async def close(self) -> None:
        return None

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
        )

    async def _close_clients(self, clients: dict[str, FatSecretClient]) -> None:
        for client in clients.values():
            await client.close()

    async def validate_account(self, account: FatSecretAccountConfig) -> None:
        """Verify FatSecret credentials by performing a real mobile API login."""
        client = self._build_client(account)
        try:
            await client.login()
        finally:
            await client.close()

    async def refresh_account_recipes(self, account: FatSecretAccountConfig, group_id: str | None = None) -> int:
        """Import cookbook recipes for one connected FatSecret account."""
        client = self._build_client(account)
        imported = 0
        try:
            recipes = await client.cookbook()
            for summary in recipes:
                self.storage.import_remote_recipe(account.key, summary, group_id)
                imported += 1
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
                summaries = await client.cookbook()
                for summary in summaries:
                    try:
                        recipe = await client.get_recipe(summary.remote_id)
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

    async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
        """Load and merge current cookbook recipe summaries from all FatSecret accounts in a group."""
        clients = self._build_clients(group_id)
        merged: dict[str, Recipe] = {}
        try:
            for account_key, client in clients.items():
                summaries = await client.cookbook()
                for summary in summaries:
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
                    recipe.remote_ids[account_key] = summary.remote_id
        finally:
            await self._close_clients(clients)
        return [merged[key] for key in sorted(merged)]

    async def hydrate_live_recipe(self, recipe_ref: Recipe) -> Recipe | None:
        """Load current recipe details from FatSecret for an in-memory recipe reference."""
        clients = self._build_clients(recipe_ref.group_id)
        try:
            for account_key, remote_id in recipe_ref.remote_ids.items():
                client = clients.get(account_key)
                if client is None:
                    continue
                remote = await client.get_recipe(remote_id)
                recipe = _copy_recipe_from_remote(recipe_ref.id, remote)
                recipe.title = recipe.title or recipe_ref.title
                recipe.group_id = recipe_ref.group_id
                recipe.remote_ids = dict(recipe_ref.remote_ids)
                return recipe
        finally:
            await self._close_clients(clients)
        return None

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
            candidates.append(
                ResolvedRecipeListItem(
                    requested_query=query,
                    grams=grams,
                    ingredient=Ingredient(
                        id=str(uuid.uuid4()),
                        recipe_id="",
                        food_id=usage.food_id,
                        title=usage.title,
                        portion_id="0",
                        amount=_gram_portion_amount(grams),
                        portion_description="100г",
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
                remote_key = (found.food_id, normalize_title(found.title))
                portion_description = found.default_portion_description or "г"
                protein = found.protein_per_portion
                fat = found.fat_per_portion
                carbohydrate = found.carbohydrate_per_portion
                remote_resolved.append(
                    ResolvedRecipeListItem(
                        requested_query=query,
                        grams=grams,
                        ingredient=Ingredient(
                            id=str(uuid.uuid4()),
                            recipe_id="",
                            food_id=found.food_id,
                            title=found.title,
                            portion_id="0",
                            amount=_gram_portion_amount(grams),
                            portion_description="100г",
                        ),
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
        return []

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
    ) -> Ingredient | None:
        search_addable = getattr(client, "search_addable_foods", None)
        if search_addable is None:
            return None
        candidates: list[FoodSearchResult] = []
        seen_queries: set[str] = set()
        for query in _query_variants(ingredient.title):
            normalized_query = normalize_title(query)
            if normalized_query in seen_queries:
                continue
            seen_queries.add(normalized_query)
            try:
                candidates.extend(await search_addable(query, page=0))
            except Exception:  # noqa: BLE001 - add fallback should not hide the original ingredient failure.
                logger.debug("legacy addable ingredient search failed for %s", query, exc_info=True)
        candidates = [
            item
            for item in _dedupe_food_results(candidates)
            if item.food_id and _matches_requested_food(ingredient.title, item.title, _food_search_text(item))
        ]
        candidates.sort(key=lambda item: _food_result_rank(ingredient.title, item))
        for candidate in candidates:
            if candidate.food_id != ingredient.food_id:
                return _ingredient_with_search_result(ingredient, candidate)
        return None

    async def _add_ingredient_with_fallback(
        self,
        client: FatSecretClient,
        remote_id: str,
        ingredient: Ingredient,
    ) -> Ingredient | None:
        if await client.add_ingredient(remote_id, ingredient):
            return ingredient
        fallback = await self._legacy_addable_ingredient(client, ingredient)
        if fallback is None:
            return None
        if await client.add_ingredient(remote_id, fallback):
            return fallback
        return None

    async def create_recipe_from_list(
        self,
        group_id: str,
        title: str,
        items: list[ResolvedRecipeListItem],
        updated_by: int,
        steps: list[str] | None = None,
    ) -> RecipeCreateResult:
        """Create a recipe from a validated ingredient list on every FatSecret account in a group."""
        clients = self._build_clients(group_id)
        description = _sync_description(timezone=self.timezone)
        recipe_id = self.storage.create_recipe(
            title=title,
            description=description,
            portions=Decimal("1"),
            prep_time=0,
            cook_time=0,
            updated_by=updated_by,
            group_id=group_id,
            steps=steps or [],
        )
        ingredients = [
            Ingredient(
                id=item.ingredient.id,
                recipe_id=recipe_id,
                food_id=item.ingredient.food_id,
                title=item.ingredient.title,
                portion_id=item.ingredient.portion_id or "0",
                amount=item.ingredient.amount,
                portion_description=item.ingredient.portion_description or "г",
            )
            for item in items
        ]
        self.storage.replace_ingredients(recipe_id, ingredients)
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            await self._close_clients(clients)
            raise FatSecretError("Не удалось создать локальный рецепт.")
        recipe.steps = list(steps or [])

        results: list[AccountSyncResult] = []
        successful: list[tuple[str, FatSecretClient, str]] = []
        failed: list[AccountSyncResult] = []
        try:
            for account_key, client in clients.items():
                remote_id: str | None = None
                try:
                    remote_id = await client.create_recipe(recipe)
                    for index, ingredient in enumerate(list(recipe.ingredients)):
                        accepted_ingredient = await self._add_ingredient_with_fallback(client, remote_id, ingredient)
                        if accepted_ingredient is None:
                            raise FatSecretError(f"{client.account.label}: FatSecret не принял ингредиент «{ingredient.title}».")
                        if accepted_ingredient.food_id != ingredient.food_id:
                            recipe.ingredients[index] = accepted_ingredient
                            self.storage.replace_ingredients(recipe.id, recipe.ingredients)
                    ok = await client.save_recipe_meta(recipe, remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe metadata save returned false")
                    successful.append((account_key, client, remote_id))
                    results.append(AccountSyncResult(account_key, remote_id, True, "создан"))
                except Exception as exc:  # noqa: BLE001 - keep per-account creation isolated.
                    rollback_message = await self._rollback_created_recipe(client, remote_id)
                    message = str(exc)
                    if rollback_message:
                        message = f"{message} {rollback_message}"
                    failed.append(AccountSyncResult(account_key, None, False, message))
            if failed:
                for account_key, client, remote_id in successful:
                    rollback_message = await self._rollback_created_recipe(client, remote_id)
                    failed.append(
                        AccountSyncResult(
                            account_key,
                            None,
                            False,
                            rollback_message or f"{client.account.label}: создание отменено после ошибки.",
                        )
                    )
                self.storage.delete_recipe(recipe.id)
                details = "; ".join(result.message for result in failed)
                raise FatSecretError(
                    "FatSecret не создал рецепт во всех подключенных аккаунтах. "
                    f"Локальный черновик удален. {details}"
                )
            for account_key, _client, remote_id in successful:
                self.storage.set_remote_recipe_id(recipe.id, account_key, remote_id, last_synced_version=0)
                recipe.remote_ids[account_key] = remote_id
                self.storage.mark_synced(recipe.id, account_key, remote_id, recipe.version)
        finally:
            await self._close_clients(clients)
        if not self.storage.remote_ids(recipe.id):
            self.storage.delete_recipe(recipe.id)
            details = "; ".join(result.message for result in results) or "FatSecret не вернул remote id"
            raise FatSecretError(
                "FatSecret не создал рецепт ни в одном подключенном аккаунте. "
                f"Локальный черновик удален. {details}"
            )
        return RecipeCreateResult(recipe_id=recipe_id, results=results)

    async def _rollback_created_recipe(self, client: FatSecretClient, remote_id: str | None) -> str:
        if not remote_id:
            return ""
        try:
            ok = await client.delete_recipe(remote_id)
        except Exception as exc:  # noqa: BLE001 - preserve original creation error and report cleanup failure.
            return f"{client.account.label}: созданный рецепт {remote_id} не удалось удалить после ошибки: {exc}"
        if ok:
            return f"{client.account.label}: созданный рецепт {remote_id} удален после ошибки."
        return f"{client.account.label}: созданный рецепт {remote_id} не удалось удалить после ошибки."

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

            source_remote = await source_client.get_recipe(source_remote_id)
            source_recipe = _copy_recipe_from_remote(recipe.id, source_remote)
            source_recipe.title = source_recipe.title or recipe.title
            source_recipe.description = _sync_description(timezone=self.timezone)
            source_recipe.remote_ids = dict(recipe.remote_ids)
            self.storage.update_recipe_from_remote(
                recipe_id=recipe.id,
                title=source_recipe.title,
                description=source_recipe.description,
                portions=source_recipe.portions,
                prep_time=source_recipe.prep_time,
                cook_time=source_recipe.cook_time,
                steps=source_recipe.steps,
            )
            self.storage.replace_ingredients(recipe.id, source_recipe.ingredients)
            recipe = self.storage.get_recipe(recipe.id) or source_recipe
            recipe.steps = list(source_recipe.steps)

            for account_key, client in clients.items():
                try:
                    remote_id = recipe.remote_ids.get(account_key)
                    if account_key == source_account_key:
                        ok = await client.save_recipe_meta(recipe, source_remote_id)
                        if not ok:
                            raise FatSecretError(f"{client.account.label}: source recipe metadata save returned false")
                        self.storage.mark_synced(recipe.id, account_key, source_remote_id, recipe.version)
                        results.append(AccountSyncResult(account_key, source_remote_id, True, "источник; дата обновлена"))
                        continue
                    remote_id = await self._ensure_remote_recipe(client, recipe, remote_id)
                    recipe.remote_ids[account_key] = remote_id
                    stats = await self._sync_ingredients(client, recipe, remote_id)
                    ok = await client.save_recipe_meta(recipe, remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe metadata save returned false")
                    self.storage.mark_synced(recipe.id, account_key, remote_id, recipe.version)
                    results.append(AccountSyncResult(account_key, remote_id, True, stats.message()))
                except Exception as exc:  # noqa: BLE001 - keep per-account sync isolated.
                    self.storage.record_sync(recipe.id, account_key, "error", str(exc))
                    results.append(AccountSyncResult(account_key, recipe.remote_ids.get(account_key), False, str(exc)))
        finally:
            await self._close_clients(clients)
        return results

    async def sync_live_recipe_from_source(
        self,
        recipe_ref: Recipe,
        source_account_key: str,
    ) -> tuple[Recipe, list[AccountSyncResult]]:
        """Read a live recipe from one FatSecret account and propagate it without persisting local recipe rows."""
        source_remote_id = recipe_ref.remote_ids.get(source_account_key)
        if source_remote_id is None:
            raise FatSecretError("Выбранный аккаунт не содержит этот рецепт. Обнови список рецептов.")

        results: list[AccountSyncResult] = []
        clients = self._build_clients(recipe_ref.group_id)
        try:
            source_client = clients.get(source_account_key)
            if source_client is None:
                raise FatSecretError("Аккаунт-источник больше не подключен.")

            source_remote = await source_client.get_recipe(source_remote_id)
            recipe = _copy_recipe_from_remote(recipe_ref.id, source_remote)
            recipe.title = recipe.title or recipe_ref.title
            recipe.description = _sync_description(timezone=self.timezone)
            recipe.group_id = recipe_ref.group_id
            recipe.remote_ids = dict(recipe_ref.remote_ids)

            for account_key, client in clients.items():
                try:
                    remote_id = recipe.remote_ids.get(account_key)
                    if account_key == source_account_key:
                        ok = await client.save_recipe_meta(recipe, source_remote_id)
                        if not ok:
                            raise FatSecretError(f"{client.account.label}: source recipe metadata save returned false")
                        results.append(AccountSyncResult(account_key, source_remote_id, True, "источник; дата обновлена"))
                        continue
                    remote_id = await self._ensure_remote_recipe(client, recipe, remote_id)
                    recipe.remote_ids[account_key] = remote_id
                    stats = await self._sync_ingredients(client, recipe, remote_id)
                    ok = await client.save_recipe_meta(recipe, remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe metadata save returned false")
                    results.append(AccountSyncResult(account_key, remote_id, True, stats.message()))
                except Exception as exc:  # noqa: BLE001 - keep per-account sync isolated.
                    results.append(AccountSyncResult(account_key, recipe.remote_ids.get(account_key), False, str(exc)))
        finally:
            await self._close_clients(clients)
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
                    results[recipe_id] = [AccountSyncResult("local", None, False, str(exc))]
        finally:
            await self._close_clients(clients)
        return results

    async def delete_live_recipe_everywhere(self, recipe_ref: Recipe) -> list[AccountSyncResult]:
        """Delete one in-memory recipe reference from every mapped FatSecret account."""
        clients = self._build_clients(recipe_ref.group_id)
        results: list[AccountSyncResult] = []
        try:
            for account_key, remote_id in list(recipe_ref.remote_ids.items()):
                client = clients.get(account_key)
                if client is None:
                    results.append(AccountSyncResult(account_key, remote_id, False, "FatSecret аккаунт больше не подключен"))
                    continue
                try:
                    ok = await client.delete_recipe(remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe delete returned false")
                    results.append(AccountSyncResult(account_key, remote_id, True, "удален в FatSecret"))
                except Exception as exc:  # noqa: BLE001 - keep per-account deletion isolated.
                    results.append(AccountSyncResult(account_key, remote_id, False, str(exc)))
        finally:
            await self._close_clients(clients)
        return results

    async def delete_live_recipes_everywhere(self, recipe_refs: list[Recipe]) -> dict[str, list[AccountSyncResult]]:
        """Delete several in-memory recipe references from FatSecret."""
        results: dict[str, list[AccountSyncResult]] = {}
        for recipe in recipe_refs:
            try:
                results[recipe.id] = await self.delete_live_recipe_everywhere(recipe)
            except Exception as exc:  # noqa: BLE001 - keep batch deletion moving.
                results[recipe.id] = [AccountSyncResult("local", None, False, str(exc))]
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
                ok = await client.delete_recipe(remote_id)
                if not ok:
                    raise FatSecretError(f"{client.account.label}: recipe delete returned false")
                self.storage.record_sync(recipe.id, account_key, "ok", f"deleted remote recipe {remote_id}")
                deleted_account_keys.append(account_key)
                results.append(AccountSyncResult(account_key, remote_id, True, "удален в FatSecret"))
            except Exception as exc:  # noqa: BLE001 - keep per-account deletion isolated.
                self.storage.record_sync(recipe.id, account_key, "error", str(exc))
                results.append(AccountSyncResult(account_key, remote_id, False, str(exc)))

        for account_key in deleted_account_keys:
            self.storage.delete_remote_recipe_id(recipe.id, account_key)
        if deleted_account_keys and not self.storage.remote_ids(recipe.id):
            self.storage.delete_recipe(recipe.id)
        return results

    async def _ensure_remote_recipe(
        self,
        client: FatSecretClient,
        recipe: Recipe,
        remote_id: str | None,
    ) -> str:
        if remote_id:
            return remote_id

        return await client.create_recipe(recipe)

    async def _sync_ingredients(self, client: FatSecretClient, recipe: Recipe, remote_id: str) -> IngredientSyncStats:
        remote = await client.get_recipe(remote_id)
        used_target_ids: set[str] = set()
        added = 0
        updated = 0
        unchanged = 0
        for ingredient in recipe.ingredients:
            target = _find_matching_ingredient(remote.ingredients, ingredient, used_target_ids)
            if target is None:
                ok = await client.add_ingredient(remote_id, ingredient)
                added += 1
            elif not _ingredient_needs_update(target, ingredient):
                used_target_ids.add(_ingredient_identity(target))
                unchanged += 1
                continue
            else:
                used_target_ids.add(_ingredient_identity(target))
                ok = await client.add_ingredient(
                    remote_id,
                    Ingredient(
                        id=target.id,
                        recipe_id=remote_id,
                        food_id=ingredient.food_id,
                        title=ingredient.title,
                        portion_id=ingredient.portion_id,
                        amount=ingredient.amount,
                        portion_description=ingredient.portion_description,
                        remote_ingredient_id=_ingredient_identity(target),
                    ),
                )
                updated += 1
            if not ok:
                raise FatSecretError(
                    f"{client.account.label}: FatSecret не принял ингредиент «{ingredient.title}». "
                    "Если это свой продукт, нужен capture API создания собственного продукта."
                )
        extras = sum(1 for target in remote.ingredients if _ingredient_identity(target) not in used_target_ids)
        return IngredientSyncStats(added=added, updated=updated, unchanged=unchanged, extras=extras)
