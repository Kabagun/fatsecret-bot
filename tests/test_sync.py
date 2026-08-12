from __future__ import annotations

import asyncio
import copy
import datetime as dt
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from fatsecret_bot.fatsecret_client import FatSecretActionError, FatSecretNotCustomFoodError
from fatsecret_bot.models import (
    BarcodeLookupResult,
    CustomFoodDefinition,
    FatSecretAccountConfig,
    FatSecretDeviceConfig,
    FoodSearchResult,
    Ingredient,
    Recipe,
    RecipeSummary,
)
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import (
    INGREDIENT_NORMALIZE_CONCURRENCY,
    FatSecretError,
    RecipeSyncEngine,
    ResolvedRecipeListItem,
    _custom_food_request_fingerprint,
    _sync_description,
)


class FakeFatSecretClient:
    def __init__(
        self,
        target: Recipe,
        account_key: str = "target",
        delete_ok: bool = True,
        ingredient_delete_ok: bool = True,
        details: dict[str, FoodSearchResult] | None = None,
    ) -> None:
        self.account = FatSecretAccountConfig(
            key=account_key,
            label=account_key,
            username=f"{account_key}@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.target = target
        self.recipes: dict[str, Recipe] = {target.id: target}
        self.delete_ok = delete_ok
        self.ingredient_delete_ok = ingredient_delete_ok
        self.saved_ingredients: list[Ingredient] = []
        self.deleted_ingredient_ids: list[str] = []
        self.deleted_recipe_ids: list[str] = []
        self.saved_meta: list[Recipe] = []
        self.details = details or {}

    async def get_recipe(self, remote_id: str) -> Recipe:
        return copy.deepcopy(self.recipes[remote_id])

    async def create_recipe(self, recipe: Recipe) -> str:
        remote_id = f"{self.target.id}-created-{len(self.recipes)}"
        created = copy.deepcopy(recipe)
        created.id = remote_id
        created.ingredients = []
        self.recipes[remote_id] = created
        return remote_id

    async def ensure_logged_in(self) -> None:
        return None

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        stored = copy.deepcopy(ingredient)
        stored.recipe_id = remote_recipe_id
        stored.remote_ingredient_id = stored.remote_ingredient_id or f"iid-{len(self.recipes[remote_recipe_id].ingredients) + 1}"
        existing = self.recipes[remote_recipe_id].ingredients
        if ingredient.remote_ingredient_id:
            existing[:] = [item for item in existing if item.remote_ingredient_id != ingredient.remote_ingredient_id]
        existing.append(stored)
        return True

    async def delete_ingredient(self, remote_recipe_id: str, remote_ingredient_id: str) -> bool:
        self.deleted_ingredient_ids.append(remote_ingredient_id)
        if self.ingredient_delete_ok:
            self.recipes[remote_recipe_id].ingredients = [
                item
                for item in self.recipes[remote_recipe_id].ingredients
                if item.remote_ingredient_id != remote_ingredient_id
            ]
        return self.ingredient_delete_ok

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        self.deleted_recipe_ids.append(remote_recipe_id)
        if self.delete_ok:
            self.recipes.pop(remote_recipe_id, None)
        return self.delete_ok

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        self.saved_meta.append(recipe)
        target = self.recipes[remote_id]
        target.title = recipe.title
        target.description = recipe.description
        target.portions = recipe.portions
        target.prep_time = recipe.prep_time
        target.cook_time = recipe.cook_time
        target.steps = list(recipe.steps)
        return True

    async def cookbook(self) -> list[RecipeSummary]:
        return [RecipeSummary(remote_id=remote_id, title=recipe.title) for remote_id, recipe in self.recipes.items()]

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        return self.details.get(result.food_id, result)

    async def get_custom_food_definition(self, remote_id: str) -> CustomFoodDefinition:
        raise FatSecretNotCustomFoodError(f"{self.account.label}: food {remote_id} is not a user-created product")

    async def close(self) -> None:
        return None


class FakeCreatedSyncTargetClient(FakeFatSecretClient):
    def __init__(
        self,
        remote_id: str,
        account_key: str,
        *,
        delete_ok: bool = True,
        created_remote_id: str | None = None,
    ) -> None:
        super().__init__(Recipe(id=remote_id, title="Омлет"), account_key=account_key, delete_ok=delete_ok)
        self.created_recipe: Recipe | None = None
        self.created_remote_id = created_remote_id or remote_id

    async def create_recipe(self, recipe: Recipe) -> str:
        self.created_recipe = recipe
        created = copy.deepcopy(recipe)
        created.id = self.created_remote_id
        created.ingredients = []
        self.recipes[self.created_remote_id] = created
        return self.created_remote_id

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        assert remote_recipe_id == self.target.id
        self.saved_ingredients.append(ingredient)
        return False


class FakeSlowDetailClient(FakeFatSecretClient):
    def __init__(self, target: Recipe, details: dict[str, FoodSearchResult]) -> None:
        super().__init__(target, details=details)
        self.active_detail_calls = 0
        self.max_active_detail_calls = 0

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        self.active_detail_calls += 1
        self.max_active_detail_calls = max(self.max_active_detail_calls, self.active_detail_calls)
        try:
            await asyncio.sleep(0.01)
            return self.details.get(result.food_id, result)
        finally:
            self.active_detail_calls -= 1


class FakeTimeoutAfterIngredientClient(FakeFatSecretClient):
    def __init__(self, target: Recipe, account_key: str) -> None:
        super().__init__(target, account_key=account_key)
        self.timed_out = False

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        accepted = await super().add_ingredient(remote_recipe_id, ingredient)
        if not self.timed_out:
            self.timed_out = True
            raise httpx.ReadTimeout("ambiguous ingredient save", request=httpx.Request("POST", "https://example.test"))
        return accepted


class FakeStaleDetailClient(FakeFatSecretClient):
    async def get_recipe(self, remote_id: str) -> Recipe:
        recipe = await super().get_recipe(remote_id)
        if remote_id != self.target.id:
            recipe.ingredients = []
        return recipe


class FakeAmbiguousDeleteClient(FakeFatSecretClient):
    def __init__(self, target: Recipe, account_key: str, *, disappeared: bool) -> None:
        super().__init__(target, account_key=account_key)
        self.disappeared = disappeared
        self.delete_calls = 0

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        self.delete_calls += 1
        self.deleted_recipe_ids.append(remote_recipe_id)
        if self.delete_calls == 1:
            if self.disappeared:
                self.recipes.pop(remote_recipe_id, None)
            raise httpx.ReadTimeout("ambiguous delete", request=httpx.Request("POST", "https://example.test"))
        self.recipes.pop(remote_recipe_id, None)
        return True


class FakeFinalRenameFailureClient(FakeFatSecretClient):
    def __init__(self, target: Recipe, account_key: str) -> None:
        super().__init__(target, account_key=account_key)
        self.failed_final_rename = False

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        if (
            recipe.title == "Омлет"
            and self.target.id not in self.recipes
            and not self.failed_final_rename
        ):
            self.failed_final_rename = True
            raise FatSecretError("final rename interrupted")
        return await super().save_recipe_meta(recipe, remote_id)


class FakeCustomFoodSourceClient(FakeFatSecretClient):
    def __init__(self, target: Recipe, definition: CustomFoodDefinition, account_key: str = "source") -> None:
        super().__init__(target, account_key=account_key)
        self.definition = definition
        self.custom_food_requests: list[str] = []

    async def get_custom_food_definition(self, remote_id: str) -> CustomFoodDefinition:
        self.custom_food_requests.append(remote_id)
        assert remote_id == self.definition.source_recipe_id
        return self.definition


class FakeCustomFoodTargetClient(FakeFatSecretClient):
    def __init__(
        self,
        target: Recipe,
        source_food_id: str,
        cloned_food_id: str,
        account_key: str = "target",
    ) -> None:
        super().__init__(target, account_key=account_key)
        self.source_food_id = source_food_id
        self.cloned_food_id = cloned_food_id
        self.created_custom_foods: list[CustomFoodDefinition] = []
        self.visible_definition: CustomFoodDefinition | None = None

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        assert remote_recipe_id == self.target.id
        self.saved_ingredients.append(ingredient)
        if ingredient.food_id == self.source_food_id:
            raise FatSecretActionError(
                "target: RecipeActionAndroidPage.aspx failed with HTTP 302",
                status_code=302,
                page="RecipeActionAndroidPage.aspx",
                action="ingredientsave",
                location="/ErrorLogUserFeedback.ashx",
                replayed=True,
            )
        return ingredient.food_id == self.cloned_food_id

    async def create_custom_food(self, definition: CustomFoodDefinition) -> str:
        self.created_custom_foods.append(definition)
        self.visible_definition = definition
        return self.cloned_food_id

    async def get_custom_food_definition(self, remote_id: str) -> CustomFoodDefinition:
        if remote_id == self.cloned_food_id and self.visible_definition is not None:
            return self.visible_definition
        raise FatSecretNotCustomFoodError(f"{self.account.label}: food {remote_id} is not a user-created product")


class FakeGroupCustomFoodClient(FakeFatSecretClient):
    def __init__(self, account_key: str, *, timeout_after_first_create: bool = False) -> None:
        super().__init__(Recipe(id=f"{account_key}-seed", title="Seed"), account_key=account_key)
        self.custom_foods: dict[str, CustomFoodDefinition] = {}
        self.created_custom_foods: list[CustomFoodDefinition] = []
        self.timeout_after_first_create = timeout_after_first_create
        self.barcode_food_ids: dict[str, str] = {}
        self.remap_calls: list[tuple[str, str, bool, str | None]] = []
        self.brand_catalog: list[str] = []
        self.brand_catalog_calls = 0

    async def list_custom_food_brands(self) -> list[str]:
        self.brand_catalog_calls += 1
        return list(self.brand_catalog)

    async def search_food(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        return [
            FoodSearchResult(food_id=food_id, title=definition.title, is_own=True)
            for food_id, definition in self.custom_foods.items()
            if definition.title.casefold() == query.casefold()
        ]

    async def create_custom_food(self, definition: CustomFoodDefinition) -> str:
        self.created_custom_foods.append(definition)
        remote_id = f"{self.account.key}-food-{len(self.created_custom_foods)}"
        self.custom_foods[remote_id] = replace(
            definition,
            nutrients={key: value.quantize(Decimal("0.001")) for key, value in definition.nutrients.items()},
            barcode="",
            barcode_type="",
        )
        if self.timeout_after_first_create:
            self.timeout_after_first_create = False
            raise httpx.ReadTimeout(
                "ambiguous custom-food create",
                request=httpx.Request("POST", "https://example.test"),
            )
        return remote_id

    async def get_custom_food_definition(self, remote_id: str) -> CustomFoodDefinition:
        definition = self.custom_foods.get(remote_id)
        if definition is None:
            raise FatSecretNotCustomFoodError(f"{self.account.label}: no custom food {remote_id}")
        return definition

    async def lookup_barcode(self, barcode: str) -> BarcodeLookupResult:
        return BarcodeLookupResult(barcode=barcode, food_id=self.barcode_food_ids.get(barcode))

    async def remap_barcode(
        self,
        barcode: str,
        food_id: str,
        *,
        is_new_food: bool,
        barcode_id: str | None = None,
    ) -> None:
        self.remap_calls.append((barcode, food_id, is_new_food, barcode_id))
        self.barcode_food_ids[barcode] = food_id


class FakeAmbiguousBarcodeClient(FakeGroupCustomFoodClient):
    def __init__(self, account_key: str, *, mapping_applied: bool) -> None:
        super().__init__(account_key)
        self.mapping_applied = mapping_applied

    async def remap_barcode(
        self,
        barcode: str,
        food_id: str,
        *,
        is_new_food: bool,
        barcode_id: str | None = None,
    ) -> None:
        self.remap_calls.append((barcode, food_id, is_new_food, barcode_id))
        if self.mapping_applied:
            self.barcode_food_ids[barcode] = food_id
        raise httpx.ReadTimeout(
            "ambiguous barcode remap",
            request=httpx.Request("POST", "https://example.test"),
        )


class FakeFacebookFoodTargetClient(FakeFatSecretClient):
    def __init__(self, target: Recipe, food_id: str, account_key: str = "target") -> None:
        super().__init__(target, account_key=account_key)
        self.food_id = food_id
        self.addable_queries: list[str] = []

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        assert remote_recipe_id == self.target.id
        self.saved_ingredients.append(ingredient)
        if ingredient.portion_id == "-1":
            raise FatSecretActionError(
                "target: RecipeActionAndroidPage.aspx failed with HTTP 302",
                status_code=302,
                page="RecipeActionAndroidPage.aspx",
                action="ingredientsave",
                location="/ErrorLogUserFeedback.ashx",
                replayed=True,
            )
        return ingredient.food_id == self.food_id and ingredient.portion_id == "0"

    async def search_addable_foods(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        self.addable_queries.append(query)
        return [
            FoodSearchResult(
                food_id=self.food_id,
                title="Сухари Панировочные",
                default_portion_id="0",
                default_portion_description="100г",
            )
        ]


class FakeCookbookClient:
    def __init__(self, summaries: list[RecipeSummary], account_key: str) -> None:
        self.account = FatSecretAccountConfig(
            key=account_key,
            label=account_key,
            username=f"{account_key}@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.summaries = summaries

    async def cookbook(self) -> list[RecipeSummary]:
        return list(self.summaries)

    async def close(self) -> None:
        return None


class FakeFailingCookbookClient(FakeCookbookClient):
    async def cookbook(self) -> list[RecipeSummary]:
        raise RuntimeError("cookbook failed")


class FakeFoodUsageClient:
    def __init__(
        self,
        recipes: list[Recipe],
        account_key: str,
        details: dict[str, FoodSearchResult] | None = None,
    ) -> None:
        self.account = FatSecretAccountConfig(
            key=account_key,
            label=account_key,
            username=f"{account_key}@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.recipes = {recipe.id: recipe for recipe in recipes}
        self.details = details or {}
        self.detail_calls: list[str] = []
        self.closed = False

    async def cookbook(self) -> list[RecipeSummary]:
        return [RecipeSummary(remote_id=recipe.id, title=recipe.title) for recipe in self.recipes.values()]

    async def get_recipe(self, remote_id: str) -> Recipe:
        return self.recipes[remote_id]

    async def ensure_logged_in(self) -> None:
        return None

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        self.detail_calls.append(result.food_id)
        return self.details.get(result.food_id, result)

    async def close(self) -> None:
        self.closed = True


class FakeSearchClient:
    def __init__(
        self,
        results: list[FoodSearchResult],
        search_results: list[FoodSearchResult] | dict[str, list[FoodSearchResult]] | None = None,
        details: dict[str, FoodSearchResult] | None = None,
    ) -> None:
        self.account = FatSecretAccountConfig(
            key="search",
            label="search",
            username="search@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.results = results
        self.search_results = search_results if search_results is not None else []
        self.details = details or {}

    async def autocomplete_food(self, query: str) -> list[FoodSearchResult]:
        return list(self.results)

    async def search_recipes(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        if isinstance(self.search_results, dict):
            return list(self.search_results.get(query, []))
        return list(self.search_results)

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        return self.details.get(result.food_id, result)

    async def close(self) -> None:
        return None


class FakeFailingCreateClient:
    def __init__(self, account_key: str) -> None:
        self.account = FatSecretAccountConfig(
            key=account_key,
            label=account_key,
            username=f"{account_key}@example.com",
            password="secret",
            market="BY",
            language="ru",
        )

    async def create_recipe(self, recipe: Recipe) -> str:
        raise RuntimeError("create failed")

    async def cookbook(self) -> list[RecipeSummary]:
        return []

    async def close(self) -> None:
        return None


class FakeCreateClient:
    def __init__(self, account_key: str = "tg11") -> None:
        self.account = FatSecretAccountConfig(
            key=account_key,
            label=account_key,
            username=f"{account_key}@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.created_recipe: Recipe | None = None
        self.create_calls = 0
        self.saved_ingredients: list[Ingredient] = []
        self.deleted_recipe_ids: list[str] = []
        self.saved_meta: list[Recipe] = []
        self.recipes: dict[str, Recipe] = {}

    async def create_recipe(self, recipe: Recipe) -> str:
        self.create_calls += 1
        self.created_recipe = copy.deepcopy(recipe)
        remote_id = f"remote-{self.account.key}"
        created = copy.deepcopy(recipe)
        created.id = remote_id
        created.ingredients = []
        self.recipes[remote_id] = created
        return remote_id

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        self._store_ingredient(remote_recipe_id, ingredient)
        return True

    def _store_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> None:
        stored = copy.deepcopy(ingredient)
        stored.recipe_id = remote_recipe_id
        stored.remote_ingredient_id = f"iid-{len(self.recipes[remote_recipe_id].ingredients) + 1}"
        self.recipes[remote_recipe_id].ingredients.append(stored)

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        self.saved_meta.append(
            Recipe(
                id=recipe.id,
                title=recipe.title,
                description=recipe.description,
                portions=recipe.portions,
                prep_time=recipe.prep_time,
                cook_time=recipe.cook_time,
                steps=list(recipe.steps),
                group_id=recipe.group_id,
            )
        )
        stored = self.recipes[remote_id]
        stored.title = recipe.title
        stored.description = recipe.description
        stored.portions = recipe.portions
        stored.prep_time = recipe.prep_time
        stored.cook_time = recipe.cook_time
        stored.steps = list(recipe.steps)
        return True

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        self.deleted_recipe_ids.append(remote_recipe_id)
        self.recipes.pop(remote_recipe_id, None)
        return True

    async def delete_ingredient(self, remote_recipe_id: str, remote_ingredient_id: str) -> bool:
        recipe = self.recipes[remote_recipe_id]
        recipe.ingredients = [
            ingredient
            for ingredient in recipe.ingredients
            if ingredient.remote_ingredient_id != remote_ingredient_id
        ]
        return True

    async def get_recipe(self, remote_recipe_id: str) -> Recipe:
        return copy.deepcopy(self.recipes[remote_recipe_id])

    async def cookbook(self) -> list[RecipeSummary]:
        return [RecipeSummary(remote_id=remote_id, title=recipe.title) for remote_id, recipe in self.recipes.items()]

    async def close(self) -> None:
        return None


class FakeRejectIngredientCreateClient(FakeCreateClient):
    def __init__(self, account_key: str, rejected_title: str) -> None:
        super().__init__(account_key)
        self.rejected_title = rejected_title

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        accepted = ingredient.title != self.rejected_title
        if accepted:
            self._store_ingredient(remote_recipe_id, ingredient)
        return accepted


class FakeLegacyAddableCreateClient(FakeCreateClient):
    def __init__(self, account_key: str, addable: FoodSearchResult) -> None:
        super().__init__(account_key)
        self.addable = addable
        self.addable_queries: list[str] = []

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        accepted = ingredient.food_id == self.addable.food_id
        if accepted:
            self._store_ingredient(remote_recipe_id, ingredient)
        return accepted

    async def search_addable_foods(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        self.addable_queries.append(query)
        return [self.addable]


class FakeAcceptSelectedFoodCreateClient(FakeLegacyAddableCreateClient):
    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        self._store_ingredient(remote_recipe_id, ingredient)
        return True


def _device() -> FatSecretDeviceConfig:
    return FatSecretDeviceConfig(
        app_version="9.99",
        device="android",
        build_sdk="35",
        build_api="35",
        build_model="test",
        build_resolution="1080x1920",
        device_identifier="test-device",
    )


def _cache_foods(
    storage: Storage,
    group_id: str,
    foods: list[tuple[str, str, int]],
    portion_id: str = "0",
    portion_description: str = "100г",
) -> None:
    ingredients: list[Ingredient] = []
    for food_id, title, count in foods:
        for index in range(count):
            ingredients.append(
                Ingredient(
                    id=f"{food_id}-{index}",
                    recipe_id=f"recipe-{index}",
                    food_id=food_id,
                    title=title,
                    portion_id=portion_id,
                    amount=Decimal("1"),
                    portion_description=portion_description,
                )
            )
    storage.replace_food_usage_cache(group_id, ingredients)


def test_sync_ingredients_updates_by_remote_iid_and_adds_missing(tmp_path) -> None:
    source = Recipe(id="local", title="Завтрак")
    source.ingredients = [
        Ingredient(
            id="src-1",
            recipe_id="local",
            food_id="food-1",
            title="Яичный Белок",
            portion_id="portion-new",
            amount=Decimal("125"),
        ),
        Ingredient(
            id="src-2",
            recipe_id="local",
            food_id="food-2",
            title="Соус",
            portion_id="portion-2",
            amount=Decimal("0.2"),
        ),
    ]
    target = Recipe(id="remote-target", title="Завтрак")
    target.ingredients = [
        Ingredient(
            id="iid-1",
            recipe_id="remote-target",
            food_id="food-1",
            title="Яичный Белок",
            portion_id="portion-old",
            amount=Decimal("100"),
            remote_ingredient_id="iid-1",
        ),
        Ingredient(
            id="iid-extra",
            recipe_id="remote-target",
            food_id="food-extra",
            title="Лишнее",
            portion_id="portion-extra",
            amount=Decimal("1"),
            remote_ingredient_id="iid-extra",
        ),
    ]
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeFatSecretClient(target)

        stats = asyncio.run(engine._sync_ingredients(client, source, target.id))

        assert stats.added == 1
        assert stats.updated == 1
        assert stats.unchanged == 0
        assert stats.deleted == 1
        assert client.saved_ingredients[0].remote_ingredient_id == "iid-1"
        assert client.saved_ingredients[0].amount == Decimal("125")
        assert client.saved_ingredients[1].remote_ingredient_id is None
        assert client.deleted_ingredient_ids == ["iid-extra"]
    finally:
        storage.close()


def test_sync_ingredients_clones_custom_food_before_cross_account_add_and_reuses_mapping(tmp_path) -> None:
    source_food_id = "132165426"
    cloned_food_id = "target-custom-1"
    source_recipe = Recipe(
        id="source-recipe",
        title="Котлеты обычные",
        ingredients=[
            Ingredient(
                id="source-iid",
                recipe_id="source-recipe",
                food_id=source_food_id,
                title="Сухая Смесь для Приготовления Мороженного",
                portion_id="0",
                amount=Decimal("0.75"),
                portion_description="100г",
                grams=Decimal("75"),
            )
        ],
    )
    definition = CustomFoodDefinition(
        source_recipe_id=source_food_id,
        title="Сухая Смесь для Приготовления Мороженного",
        manufacturer_name="Nina Farina",
        serving_type="Per100g",
        serving_size="100",
        metric_serving_size="100g",
        nutrients={
            "calories": Decimal("395"),
            "protein": Decimal("13"),
            "totalFat": Decimal("5"),
            "carbohydrate": Decimal("67"),
        },
    )
    source = FakeCustomFoodSourceClient(source_recipe, definition, account_key="tg-source")
    target_recipe = Recipe(id="target-recipe", title="Котлеты обычные")
    target = FakeCustomFoodTargetClient(
        target_recipe,
        source_food_id,
        cloned_food_id,
        account_key="tg-target",
    )
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())

        stats = asyncio.run(
            engine._sync_ingredients(
                target,
                source_recipe,
                target_recipe.id,
                source_client=source,
                source_account_key="tg-source",
                target_account_key="tg-target",
            )
        )

        assert stats.added == 1
        assert [item.food_id for item in target.saved_ingredients] == [source_food_id, cloned_food_id]
        assert target.saved_ingredients[1].amount == Decimal("0.75")
        assert target.saved_ingredients[1].portion_id == "0"
        assert target.created_custom_foods == [definition]
        assert source.custom_food_requests == [source_food_id]
        assert storage.custom_food_mapping("tg-source", source_food_id, "tg-target") == cloned_food_id
        assert storage.custom_food_mapping("tg-target", cloned_food_id, "tg-source") == source_food_id

        target.target.ingredients = [
            Ingredient(
                id="target-iid",
                recipe_id=target_recipe.id,
                food_id=cloned_food_id,
                title="Сухая Смесь для Приготовления Мороженного",
                portion_id="0",
                amount=Decimal("0.75"),
                portion_description="100г",
                remote_ingredient_id="target-iid",
                grams=Decimal("75"),
            )
        ]
        target.saved_ingredients.clear()

        repeat_stats = asyncio.run(
            engine._sync_ingredients(
                target,
                source_recipe,
                target_recipe.id,
                source_client=source,
                source_account_key="tg-source",
                target_account_key="tg-target",
            )
        )

        assert repeat_stats.unchanged == 1
        assert target.saved_ingredients == []
        assert target.created_custom_foods == [definition]
        assert source.custom_food_requests == [source_food_id]
    finally:
        storage.close()


def test_prepare_target_recipe_clones_nina_farina_before_any_ingredient_save(tmp_path) -> None:
    source_food_id = "132165426"
    cloned_food_id = "target-custom-1"
    source_recipe = Recipe(
        id="source-recipe",
        title="Мороженое",
        ingredients=[
            Ingredient(
                id="source-iid",
                recipe_id="source-recipe",
                food_id=source_food_id,
                title="Сухая Смесь для Приготовления Мороженного Nina Farina",
                portion_id="0",
                amount=Decimal("0.75"),
                portion_description="100г",
                grams=Decimal("75"),
            )
        ],
    )
    definition = CustomFoodDefinition(
        source_recipe_id=source_food_id,
        title="Сухая Смесь для Приготовления Мороженного Nina Farina",
        manufacturer_name="Nina Farina",
        serving_type="Per100g",
        serving_size="100",
        metric_serving_size="100g",
        nutrients={"calories": Decimal("395")},
    )
    source = FakeCustomFoodSourceClient(source_recipe, definition, account_key="tg-source")
    target = FakeCustomFoodTargetClient(
        Recipe(id="target", title="Мороженое"),
        source_food_id,
        cloned_food_id,
        account_key="tg-target",
    )
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())

        prepared = asyncio.run(
            engine._prepare_target_recipe(
                source,
                target,
                "tg-source",
                "tg-target",
                source_recipe,
                {},
            )
        )
        repeated = asyncio.run(
            engine._prepare_target_recipe(
                source,
                target,
                "tg-source",
                "tg-target",
                source_recipe,
                {},
            )
        )

        assert [item.food_id for item in prepared.ingredients] == [cloned_food_id]
        assert [item.food_id for item in repeated.ingredients] == [cloned_food_id]
        assert target.saved_ingredients == []
        assert target.created_custom_foods == [definition]
        assert storage.custom_food_mapping("tg-source", source_food_id, "tg-target") == cloned_food_id
        assert storage.custom_food_mapping("tg-target", cloned_food_id, "tg-source") == source_food_id
    finally:
        storage.close()


def test_prepare_target_recipe_keeps_public_food_without_mapping(tmp_path) -> None:
    ingredient = Ingredient(
        id="milk",
        recipe_id="source",
        food_id="46136861",
        title="Молоко Савушкин 1,5%",
        portion_id="0",
        amount=Decimal("1"),
        portion_description="100г",
        grams=Decimal("100"),
    )
    source_recipe = Recipe(id="source", title="Молоко", ingredients=[ingredient])
    source = FakeFatSecretClient(source_recipe, account_key="tg-source")
    target = FakeFatSecretClient(Recipe(id="target", title="Молоко"), account_key="tg-target")
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())

        prepared = asyncio.run(
            engine._prepare_target_recipe(
                source,
                target,
                "tg-source",
                "tg-target",
                source_recipe,
                {},
            )
        )

        assert prepared.ingredients[0].food_id == ingredient.food_id
        assert storage.custom_food_mapping("tg-source", ingredient.food_id, "tg-target") is None
    finally:
        storage.close()


def test_sync_ingredients_keeps_public_facebook_food_id_and_portion(tmp_path) -> None:
    food_id = "46136861"
    source_recipe = Recipe(
        id="source-recipe",
        title="Котлеты обычные",
        ingredients=[
            Ingredient(
                id="source-iid",
                recipe_id="source-recipe",
                food_id=food_id,
                title="Сухари Панировочные",
                portion_id="0",
                amount=Decimal("0.75"),
                portion_description="100г",
                remote_ingredient_id="source-iid",
                grams=Decimal("75"),
            )
        ],
    )
    source = FakeFatSecretClient(source_recipe, account_key="tg-source")
    target_recipe = Recipe(id="target-recipe", title="Котлеты обычные")
    target = FakeFacebookFoodTargetClient(target_recipe, food_id, account_key="tg-target")
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())

        stats = asyncio.run(
            engine._sync_ingredients(
                target,
                source_recipe,
                target_recipe.id,
                source_client=source,
                source_account_key="tg-source",
                target_account_key="tg-target",
            )
        )

        assert stats.added == 1
        assert [(item.food_id, item.portion_id, item.amount) for item in target.saved_ingredients] == [
            (food_id, "0", Decimal("0.75")),
        ]
        assert target.saved_ingredients[0].remote_ingredient_id is None
        assert target.addable_queries == []
        assert storage.custom_food_mapping("tg-source", food_id, "tg-target") is None
    finally:
        storage.close()


def test_sync_ingredients_never_substitutes_another_public_food_on_rejection(tmp_path) -> None:
    food_id = "46136861"
    source_recipe = Recipe(
        id="source-recipe",
        title="Котлеты обычные",
        ingredients=[
            Ingredient(
                id="source-iid",
                recipe_id="source-recipe",
                food_id=food_id,
                title="Сухари Панировочные",
                portion_id="-1",
                amount=Decimal("75"),
                portion_description="г",
                remote_ingredient_id="source-iid",
                grams=Decimal("75"),
            )
        ],
    )
    source = FakeFatSecretClient(source_recipe, account_key="tg-source")
    target_recipe = Recipe(id="target-recipe", title="Котлеты обычные")
    target = FakeFacebookFoodTargetClient(target_recipe, food_id, account_key="tg-target")
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())

        try:
            asyncio.run(
                engine._sync_ingredients(
                    target,
                    source_recipe,
                    target_recipe.id,
                    source_client=source,
                    source_account_key="tg-source",
                    target_account_key="tg-target",
                )
            )
        except FatSecretActionError as exc:
            assert exc.action == "ingredientsave"
        else:
            raise AssertionError("expected FatSecretActionError")

        assert target.addable_queries == []
        assert [item.food_id for item in target.saved_ingredients] == [food_id]
    finally:
        storage.close()


def test_sync_ingredients_fails_when_extra_cannot_be_deleted(tmp_path) -> None:
    source = Recipe(id="local", title="Завтрак")
    target = Recipe(id="remote-target", title="Завтрак")
    target.ingredients = [
        Ingredient(
            id="iid-extra",
            recipe_id="remote-target",
            food_id="food-extra",
            title="Лишнее",
            portion_id="portion-extra",
            amount=Decimal("1"),
            remote_ingredient_id="iid-extra",
        )
    ]
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeFatSecretClient(target, ingredient_delete_ok=False)

        try:
            asyncio.run(engine._sync_ingredients(client, source, target.id))
        except FatSecretError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected FatSecretError")

        assert "не удалил лишний ингредиент «Лишнее»" in message
        assert client.deleted_ingredient_ids == ["iid-extra"]
    finally:
        storage.close()


def test_sync_recipe_stores_normalized_grams_but_copies_raw_portion_fields(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Соус", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(recipe_id, "tg11", "remote-source", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "remote-target", last_synced_version=1)
        source_recipe = Recipe(id="remote-source", title="Соус")
        source_recipe.ingredients = [
            Ingredient(
                id="iid-1",
                recipe_id="remote-source",
                food_id="food-sauce",
                title="Соус",
                portion_id="serving-portion",
                amount=Decimal("1.5"),
                portion_description="порции",
                remote_ingredient_id="iid-1",
            )
        ]
        target_recipe = Recipe(id="remote-target", title="Соус")
        source = FakeFatSecretClient(
            source_recipe,
            account_key="tg11",
            details={
                "food-sauce": FoodSearchResult(
                    food_id="food-sauce",
                    title="Соус",
                    default_portion_id="gram-portion",
                    default_portion_description="100г",
                    grams_per_portion=Decimal("100"),
                )
            },
        )
        target = FakeFatSecretClient(target_recipe, account_key="tg22")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        results = asyncio.run(engine.sync_recipe_from_source(recipe_id, "tg11"))
        synced_recipe = storage.get_recipe(recipe_id)

        assert all(result.ok for result in results)
        assert target.saved_ingredients[0].portion_id == "serving-portion"
        assert target.saved_ingredients[0].amount == Decimal("1.5")
        assert target.saved_ingredients[0].portion_description == "порции"
        assert target.saved_ingredients[0].remote_ingredient_id is None
        assert synced_recipe is not None
        assert synced_recipe.ingredients[0].portion_id == "gram-portion"
        assert synced_recipe.ingredients[0].grams == Decimal("150.0")
    finally:
        storage.close()


def test_normalize_recipe_ingredients_resolves_details_in_parallel(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        ingredient_count = INGREDIENT_NORMALIZE_CONCURRENCY + 4
        ingredients = [
            Ingredient(
                id=f"i-{index}",
                recipe_id="recipe",
                food_id=f"food-{index}",
                title=f"Соус {index}",
                portion_id="serving",
                amount=Decimal("1"),
                portion_description="порции",
            )
            for index in range(ingredient_count)
        ]
        client = FakeSlowDetailClient(
            Recipe(id="recipe", title="Рецепт"),
            {
                f"food-{index}": FoodSearchResult(
                    food_id=f"food-{index}",
                    title=f"Соус {index}",
                    default_portion_id=f"gram-{index}",
                    default_portion_description="100г",
                    grams_per_portion=Decimal("100"),
                )
                for index in range(ingredient_count)
            },
        )

        normalized = asyncio.run(engine._normalize_recipe_ingredients(client, ingredients))

        assert client.max_active_detail_calls > 1
        assert client.max_active_detail_calls <= INGREDIENT_NORMALIZE_CONCURRENCY
        assert [item.id for item in normalized] == [f"i-{index}" for index in range(ingredient_count)]
        assert [item.amount for item in normalized] == [Decimal("1")] * ingredient_count
        assert [item.portion_description for item in normalized] == ["100г"] * ingredient_count
        assert [item.grams for item in normalized] == [Decimal("100")] * ingredient_count
    finally:
        storage.close()


def test_normalize_recipe_ingredients_skips_detail_lookup_for_known_gram_amounts(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        ingredients = [
            Ingredient(
                id=f"i-{index}",
                recipe_id="recipe",
                food_id=f"food-{index}",
                title=f"Филе {index}",
                portion_id=f"gram-{index}",
                amount=Decimal("100"),
                portion_description="г",
            )
            for index in range(3)
        ]
        client = FakeSlowDetailClient(Recipe(id="recipe", title="Рецепт"), {})

        normalized = asyncio.run(engine._normalize_recipe_ingredients(client, ingredients))

        assert client.max_active_detail_calls == 0
        assert [item.grams for item in normalized] == [Decimal("100")] * 3
    finally:
        storage.close()


def test_normalize_recipe_ingredient_keeps_100g_portion_amount_in_servings(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        ingredient = Ingredient(
            id="yogurt-iid",
            recipe_id="pancakes",
            food_id="93062070",
            title="Йогурт Черника",
            portion_id="0",
            amount=Decimal("1.080"),
            portion_description="serving",
            remote_ingredient_id="source-yogurt-iid",
        )
        client = FakeFatSecretClient(
            Recipe(id="pancakes", title="Блины"),
            details={
                "93062070": FoodSearchResult(
                    food_id="93062070",
                    title="Йогурт Черника",
                    default_portion_id="0",
                    default_portion_description="100g",
                    raw={
                        "_gram_portion_id": "74562979",
                        "_gram_portion_description": "100g",
                        "_gram_portion_gram_weight": "100.000",
                        "_gram_portion_default_amount": "1.000",
                    },
                )
            },
        )

        normalized = asyncio.run(engine._normalize_recipe_ingredient(client, ingredient))

        assert normalized.portion_id == "74562979"
        assert normalized.amount == Decimal("1.080")
        assert normalized.portion_description == "100g"
        assert normalized.grams == Decimal("108.000")
    finally:
        storage.close()


def test_refresh_food_usage_cache_for_all_groups_refreshes_groups_with_accounts(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        storage.register_user(22, "Two")
        group = storage.create_group(11, "Семья")
        empty_group = storage.create_group(22, "Без аккаунта")
        storage.upsert_fatsecret_account(11, "One", "one@example.com", "secret", "BY", "ru")
        recipe = Recipe(id="remote-1", title="Котлеты")
        recipe.ingredients = [
            Ingredient(
                id="i1",
                recipe_id="remote-1",
                food_id="food-mince",
                title="Свино-Куриный Фарш",
                portion_id="0",
                amount=Decimal("1"),
                portion_description="100г",
            )
        ]
        client = FakeFoodUsageClient([recipe], "tg11")
        engine = RecipeSyncEngine(storage, _device())

        def build_clients(group_id=None):  # type: ignore[no-untyped-def]
            assert group_id == group.id
            return {"tg11": client}

        engine._build_clients = build_clients  # type: ignore[method-assign]

        refreshed = asyncio.run(engine.refresh_food_usage_cache_for_all_groups())

        assert refreshed == {group.id: 1}
        assert [item.title for item in storage.list_food_usage_cache(group.id)] == ["Свино-Куриный Фарш"]
        assert storage.list_food_usage_cache(empty_group.id) == []
        assert client.closed is True
    finally:
        storage.close()


def test_food_usage_refresh_resolves_repeated_food_once_per_account(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        storage.upsert_fatsecret_account(11, "One", "one@example.com", "secret", "BY", "ru")
        recipes = []
        for index in range(2):
            recipe = Recipe(id=f"remote-{index}", title=f"Рецепт {index}")
            recipe.ingredients = [
                Ingredient(
                    id=f"i-{index}",
                    recipe_id=recipe.id,
                    food_id="food-yogurt",
                    title="Йогурт",
                    portion_id="serving",
                    amount=Decimal("1"),
                    portion_description="serving",
                )
            ]
            recipes.append(recipe)
        client = FakeFoodUsageClient(
            recipes,
            "tg11",
            details={
                "food-yogurt": FoodSearchResult(
                    food_id="food-yogurt",
                    title="Йогурт",
                    default_portion_id="gram-yogurt",
                    default_portion_description="100г",
                    grams_per_portion=Decimal("100"),
                )
            },
        )
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]

        refreshed = asyncio.run(engine.refresh_food_usage_cache(group.id))

        assert refreshed == 1
        assert client.detail_calls == ["food-yogurt"]
        assert storage.list_food_usage_cache(group.id)[0].use_count == 2
    finally:
        storage.close()


def test_recipe_list_candidates_prefers_frequent_local_shorter_tie(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(
            storage,
            group.id,
            [
                ("food-cheese", "Филе Куриное в Сыре", 1),
                ("food-chicken", "Куриное Филе", 2),
            ],
        )
        engine = RecipeSyncEngine(storage, _device())

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "Филе", Decimal("300"), limit=1))

        assert len(candidates) == 1
        assert candidates[0].ingredient.title == "Куриное Филе"
        assert candidates[0].ingredient.amount == Decimal("3")
        assert candidates[0].ingredient.portion_id == "0"
        assert candidates[0].ingredient.portion_description == "100г"
        assert candidates[0].source == "часто использовался"
    finally:
        storage.close()


def test_recipe_list_candidates_repairs_local_zero_portion_with_search_metadata(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(
            storage,
            group.id,
            [("food-oil", "Масло Растительное", 1)],
            portion_id="0",
            portion_description="г",
        )
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="food-oil",
                    title="Масло Растительное",
                    default_portion_id="0",
                    default_portion_description="100г",
                )
            ],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "масло", Decimal("10"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.portion_description == "100г"
        assert candidates[0].ingredient.amount == Decimal("0.1")
    finally:
        storage.close()


def test_recipe_list_candidates_enriches_frequent_local_with_macros(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-chicken", "Куриное Филе", 3)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="food-chicken",
                    title="Куриное Филе",
                    brand="",
                    default_portion_id="portion-1",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("110"),
                    protein_per_portion=Decimal("23"),
                    fat_per_portion=Decimal("2"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            ],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "филе", Decimal("300"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].energy_per_100g == Decimal("110")
        assert candidates[0].protein_per_100g == Decimal("23")
        assert candidates[0].fat_per_100g == Decimal("2")
        assert candidates[0].carbohydrate_per_100g == Decimal("0")
    finally:
        storage.close()


def test_recipe_list_candidates_enriches_cached_food_by_title_when_food_id_differs(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-cached-chicken", "Куриное Филе", 3)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="food-search-chicken",
                    title="Куриное Филе",
                    default_portion_id="portion-1",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("110"),
                    protein_per_portion=Decimal("23"),
                    fat_per_portion=Decimal("2"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            ],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "куриное филе", Decimal("631"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-cached-chicken"
        assert candidates[0].energy_per_100g == Decimal("110")
        assert candidates[0].protein_per_100g == Decimal("23")
    finally:
        storage.close()


def test_recipe_list_candidates_uses_direct_brand_when_enriching_cached_food(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-brest", "Сметана 20%", 1)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results={
                "Сметана 20%": [
                    FoodSearchResult(
                        food_id="food-wrong",
                        title="Сметана 20%",
                        energy_per_portion=Decimal("287"),
                        protein_per_portion=Decimal("3.6"),
                        fat_per_portion=Decimal("28.8"),
                        carbohydrate_per_portion=Decimal("4.9"),
                    )
                ],
                "сметана 20": [
                    FoodSearchResult(
                        food_id="food-wrong",
                        title="Сметана 20%",
                        energy_per_portion=Decimal("287"),
                        protein_per_portion=Decimal("3.6"),
                        fat_per_portion=Decimal("28.8"),
                        carbohydrate_per_portion=Decimal("4.9"),
                    )
                ],
                "Брест-Литовск Сметана 20%": [
                    FoodSearchResult(
                        food_id="food-search-brest",
                        title="Сметана 20%",
                        brand="Брест-Литовск",
                        energy_per_portion=Decimal("204"),
                        protein_per_portion=Decimal("2.5"),
                        fat_per_portion=Decimal("20"),
                        carbohydrate_per_portion=Decimal("3.4"),
                    )
                ],
            },
            details={
                "food-brest": FoodSearchResult(
                    food_id="food-brest",
                    title="Сметана 20%",
                    brand="Брест-Литовск",
                    default_portion_description="100г",
                )
            },
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "сметана 20", Decimal("150"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-brest"
        assert candidates[0].brand == "Брест-Литовск"
        assert candidates[0].energy_per_100g == Decimal("204")
        assert candidates[0].fat_per_100g == Decimal("20")
    finally:
        storage.close()


def test_recipe_list_candidates_does_not_enrich_cached_food_from_wrong_brand(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-brest", "Сметана 20%", 1)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="food-wrong",
                    title="Сметана 20%",
                    energy_per_portion=Decimal("287"),
                    protein_per_portion=Decimal("3.6"),
                    fat_per_portion=Decimal("28.8"),
                    carbohydrate_per_portion=Decimal("4.9"),
                )
            ],
            details={
                "food-brest": FoodSearchResult(
                    food_id="food-brest",
                    title="Сметана 20%",
                    brand="Брест-Литовск",
                    default_portion_description="100г",
                )
            },
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "сметана 20", Decimal("150"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-brest"
        assert candidates[0].brand == "Брест-Литовск"
        assert candidates[0].energy_per_100g is None
    finally:
        storage.close()


def test_recipe_list_candidates_does_not_enrich_cached_food_from_extra_title_words(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-cached-chicken", "Куриное Филе", 3)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="food-cheese-chicken",
                    title="Куриное Филе в Сыре",
                    energy_per_portion=Decimal("210"),
                    protein_per_portion=Decimal("22"),
                    fat_per_portion=Decimal("12"),
                    carbohydrate_per_portion=Decimal("5"),
                )
            ],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "куриное филе", Decimal("631"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-cached-chicken"
        assert candidates[0].energy_per_100g is None
    finally:
        storage.close()


def test_recipe_list_candidates_prefers_frequent_generic_over_unrequested_brand(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-cached-chicken", "Куриное Филе", 9)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="food-vitkon",
                    title="Филе Куриное",
                    brand="Витконпродукт",
                    default_portion_id="portion-vitkon",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("109"),
                    protein_per_portion=Decimal("21.6"),
                    fat_per_portion=Decimal("2.5"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            ],
            details={
                "food-cached-chicken": FoodSearchResult(
                    food_id="food-cached-chicken",
                    title="Куриное Филе",
                    default_portion_id="portion-chicken",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("110"),
                    protein_per_portion=Decimal("23.1"),
                    fat_per_portion=Decimal("1.2"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            },
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "филе куриное", Decimal("366"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-cached-chicken"
        assert candidates[0].ingredient.title == "Куриное Филе"
        assert candidates[0].brand == ""
        assert candidates[0].energy_per_100g == Decimal("110")
    finally:
        storage.close()


def test_recipe_list_candidates_uses_remote_when_usage_cache_is_empty(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(
                    food_id="food-remote",
                    title="Куриное Филе",
                    default_portion_id="portion-1",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("110"),
                    protein_per_portion=Decimal("23"),
                    fat_per_portion=Decimal("2"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            ],
            search_results=[],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "филе", Decimal("300"), limit=1))

        assert candidates[0].source == "FatSecret"
        assert candidates[0].ingredient.food_id == "food-remote"
        assert candidates[0].energy_per_100g == Decimal("110")
    finally:
        storage.close()


def test_recipe_list_candidates_ranks_remote_matches_before_raw_order(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-cheese", title="Филе Куриное в Сыре"),
                FoodSearchResult(food_id="food-chicken", title="Куриное Филе"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "Филе", Decimal("300"), limit=1))

        assert len(candidates) == 1
        assert candidates[0].ingredient.title == "Куриное Филе"
    finally:
        storage.close()


def test_recipe_list_candidates_prefers_exact_remote_over_bad_local_history(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-cheese", "Куриное Филе в Сыре", 5)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-cheese", title="Куриное Филе в Сыре"),
                FoodSearchResult(food_id="food-chicken", title="Куриное Филе"),
            ],
            search_results=[
                FoodSearchResult(food_id="food-chicken", title="Куриное Филе"),
            ],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "Куриное филе", Decimal("631"), limit=1))

        assert candidates[0].ingredient.food_id == "food-chicken"
        assert candidates[0].ingredient.title == "Куриное Филе"
        assert candidates[0].ingredient.amount == Decimal("6.31")
        assert candidates[0].ingredient.portion_id == "0"
        assert candidates[0].ingredient.portion_description == "100г"
    finally:
        storage.close()


def test_recipe_list_candidates_rejects_farshmak_for_meat_query(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-farshmak", title="Фаршмак", brand="Баренцево"),
                FoodSearchResult(food_id="food-mince", title='Фарш Мясной "Котлетный"', brand="Евроопт"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "Свино-куриный фарш", Decimal("259"), limit=3))

        assert candidates == []
    finally:
        storage.close()


def test_recipe_list_candidates_uses_cached_own_food_before_weak_remote_match(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-own-mince", "Свино-Куриный Фарш", 4)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-farshmak", title="Фаршмак", brand="Баренцево"),
                FoodSearchResult(food_id="food-own-mince", title="Свино-Куриный Фарш"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "Свино-куриный фарш", Decimal("259"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-own-mince"
        assert candidates[0].ingredient.title == "Свино-Куриный Фарш"
        assert candidates[0].ingredient.amount == Decimal("2.59")
    finally:
        storage.close()


def test_recipe_list_candidates_does_not_use_cached_food_missing_requested_detail(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-russian", "Кетчуп Русский Махеев", 10)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-tomato", title="Кетчуп Томатный", brand="Махеев"),
                FoodSearchResult(food_id="food-russian", title="Кетчуп Русский", brand="Махеев"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(
            engine.recipe_list_candidates(group.id, "кетчуп махеев томатный", Decimal("25"), limit=1)
        )

        assert candidates[0].source == "FatSecret"
        assert candidates[0].ingredient.food_id == "food-tomato"
        assert candidates[0].ingredient.title == "Кетчуп Томатный"
    finally:
        storage.close()


def test_recipe_list_candidates_prefers_cached_food_for_generic_brand_query(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("food-russian", "Кетчуп Русский Махеев", 10)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-generic", title="Кетчуп", brand="Махеев"),
                FoodSearchResult(food_id="food-russian", title="Кетчуп Русский", brand="Махеев"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "кетчуп махеев", Decimal("25"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "food-russian"
    finally:
        storage.close()


def test_recipe_list_candidates_requires_requested_percent(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-cream", title="Сметана"),
                FoodSearchResult(food_id="food-cream-20", title="Сметана 20%"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "Сметана 20%", Decimal("150"), limit=1))

        assert candidates[0].ingredient.food_id == "food-cream-20"
        assert candidates[0].ingredient.title == "Сметана 20%"
    finally:
        storage.close()


def test_recipe_list_candidates_ranks_brand_and_description_matches(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(food_id="food-chips", title="Чипсы", brand="Махеев"),
                FoodSearchResult(food_id="food-ketchup", title="Кетчуп Русский", brand="Махеев"),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "махеев русский", Decimal("25"), limit=1))

        assert len(candidates) == 1
        assert candidates[0].ingredient.title == "Кетчуп Русский"
        assert candidates[0].brand == "Махеев"
    finally:
        storage.close()


def test_recipe_list_candidates_corrects_inconsistent_energy_from_macros(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(
                    food_id="food-ketchup",
                    title="Кетчуп",
                    energy_per_portion=Decimal("7"),
                    protein_per_portion=Decimal("1"),
                    fat_per_portion=Decimal("0.1"),
                    carbohydrate_per_portion=Decimal("17"),
                ),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "кетчуп", Decimal("25"), limit=1))

        assert candidates[0].energy_per_100g == Decimal("72.9")
    finally:
        storage.close()


def test_recipe_list_candidates_does_not_display_internal_metadata_as_brand(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(
                    food_id="food-ketchup",
                    title="Кетчуп",
                    description="mtypeS#E{P<A*R*A>T}O!R1S#E{P<A*R*A>T}O!RmnameS#E{P<A*R*A>T}O",
                ),
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "кетчуп", Decimal("25"), limit=1))

        assert candidates[0].ingredient.title == "Кетчуп"
        assert candidates[0].brand == ""
    finally:
        storage.close()


def test_recipe_list_candidates_offset_returns_requested_remote_page(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [FoodSearchResult(food_id=f"food-{index}", title=f"Филе {index:02d}") for index in range(6)]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(
            engine.recipe_list_candidates("group", "Филе", Decimal("100"), limit=2, offset=3)
        )

        assert [item.ingredient.title for item in candidates] == ["Филе 03", "Филе 04"]
    finally:
        storage.close()


def test_recipe_list_candidates_keeps_remote_default_portion_description(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(
                    food_id="food-ketchup",
                    title="Кетчуп Русский",
                    default_portion_id="0",
                    default_portion_description="100г",
                )
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "кетчуп русский", Decimal("25"), limit=1))

        assert candidates[0].ingredient.portion_description == "100г"
        assert candidates[0].ingredient.amount == Decimal("0.25")
    finally:
        storage.close()


def test_recipe_list_candidates_uses_remote_gram_portion_id(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(
                    food_id="33908",
                    title="Соль",
                    default_portion_id="29654",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("0"),
                    protein_per_portion=Decimal("0"),
                    fat_per_portion=Decimal("0"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "соль", Decimal("9"), limit=1))

        assert candidates[0].ingredient.food_id == "33908"
        assert candidates[0].ingredient.portion_id == "29654"
        assert candidates[0].ingredient.amount == Decimal("0.09")
        assert candidates[0].ingredient.portion_description == "100г"
    finally:
        storage.close()


def test_recipe_list_candidates_uses_cached_food_gram_portion_id_from_metadata(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        _cache_foods(storage, group.id, [("33908", "Соль", 2)])
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [],
            search_results=[
                FoodSearchResult(
                    food_id="33908",
                    title="Соль",
                    default_portion_id="29654",
                    default_portion_description="100г",
                    energy_per_portion=Decimal("0"),
                    protein_per_portion=Decimal("0"),
                    fat_per_portion=Decimal("0"),
                    carbohydrate_per_portion=Decimal("0"),
                )
            ],
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "соль", Decimal("9"), limit=1))

        assert candidates[0].source == "часто использовался"
        assert candidates[0].ingredient.food_id == "33908"
        assert candidates[0].ingredient.portion_id == "29654"
        assert candidates[0].ingredient.amount == Decimal("0.09")
        assert candidates[0].ingredient.portion_description == "100г"
    finally:
        storage.close()


def test_recipe_list_candidates_forces_gram_portion_for_non_weight_remote_default(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeSearchClient(
            [
                FoodSearchResult(
                    food_id="food-egg",
                    title="Яйцо",
                    default_portion_id="large",
                    default_portion_description="большой",
                )
            ],
            details={
                "food-egg": FoodSearchResult(
                    food_id="food-egg",
                    title="Яйцо",
                    default_portion_id="large",
                    default_portion_description="большой",
                    energy_per_portion=Decimal("147"),
                    protein_per_portion=Decimal("12.58"),
                    fat_per_portion=Decimal("9.94"),
                    carbohydrate_per_portion=Decimal("0.77"),
                    raw={"_gram_portion_id": "51772", "_gram_portion_description": "г"},
                )
            },
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "яйцо куриное", Decimal("50"), limit=1))

        assert candidates[0].ingredient.title == "Яйцо"
        assert candidates[0].ingredient.portion_id == "51772"
        assert candidates[0].ingredient.amount == Decimal("50")
        assert candidates[0].ingredient.portion_description == "г"
        assert candidates[0].ingredient.grams == Decimal("50")
    finally:
        storage.close()


def test_create_recipe_from_list_uses_last_sync_description(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeCreateClient()
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Филе",
                grams=Decimal("100"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-1",
                    title="Куриное Филе",
                    portion_id="portion-1",
                    amount=Decimal("100"),
                    portion_description="г",
                ),
                source="FatSecret",
            )
        ]

        created = asyncio.run(
            engine.create_recipe_from_list(
                "group",
                "Тест",
                items,
                updated_by=11,
                steps=["Смешать", "Запечь"],
            )
        )

        assert client.created_recipe is not None
        assert client.created_recipe.description.startswith("Последняя синхронизация: ")
        assert client.created_recipe.steps == ["Смешать", "Запечь"]
        stored = storage.get_recipe(created.recipe_id)
        assert stored is not None
        assert stored.ingredients[0].id != "ingredient-1"
    finally:
        storage.close()


def test_create_recipe_from_list_recovers_create_before_remote_id_journal_write(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeCreateClient()
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Филе",
                grams=Decimal("100"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-1",
                    title="Куриное Филе",
                    portion_id="portion-1",
                    amount=Decimal("100"),
                    portion_description="г",
                ),
                source="FatSecret",
            )
        ]
        original_update = storage.update_recipe_list_run_account
        crash_injected = False

        class SimulatedProcessCrash(BaseException):
            pass

        def crash_before_remote_id_commit(run_id, account_key, status, **kwargs):  # noqa: ANN001, ANN003
            nonlocal crash_injected
            if status == "created" and not crash_injected:
                crash_injected = True
                raise SimulatedProcessCrash
            return original_update(run_id, account_key, status, **kwargs)

        storage.update_recipe_list_run_account = crash_before_remote_id_commit  # type: ignore[method-assign]

        with pytest.raises(SimulatedProcessCrash):
            asyncio.run(engine.create_recipe_from_list("group", "Тест", items, updated_by=11))

        assert client.create_calls == 1
        assert storage._conn.execute(
            "SELECT new_remote_id FROM recipe_list_run_accounts"
        ).fetchone()[0] is None

        resumed = asyncio.run(engine.create_recipe_from_list("group", "Тест", items, updated_by=11))

        assert client.create_calls == 1
        assert storage.remote_ids(resumed.recipe_id) == {"tg11": "remote-tg11"}
        assert storage._conn.execute("SELECT status FROM recipe_list_runs").fetchone()[0] == "completed"
    finally:
        storage.close()


def test_sync_description_uses_configured_timezone() -> None:
    value = _sync_description(dt.datetime(2026, 6, 17, 12, 50, tzinfo=dt.UTC), timezone="Europe/Minsk")

    assert value == "Последняя синхронизация: 17.06.2026 15:50"


def test_storage_next_available_recipe_title_skips_existing_titles(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.create_recipe("Омлет 2", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")

        assert storage.find_recipe_by_title("group", "омлет").title == "Омлет"
        assert storage.next_available_recipe_title("group", "Омлет") == "Омлет 3"
        assert storage.next_available_recipe_title("group", "Новый", include_base=False) == "Новый 2"
    finally:
        storage.close()


def test_sync_recipe_preserves_source_description_without_writing_source(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "старое описание", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        engine = RecipeSyncEngine(storage, _device())
        source = FakeFatSecretClient(
            Recipe(id="111", title="Омлет", description="старое описание"),
            account_key="tg11",
        )
        engine._build_clients = lambda group_id=None: {"tg11": source}  # type: ignore[method-assign]

        results = asyncio.run(engine.sync_recipe_from_source(recipe_id, "tg11"))

        assert results[0].ok is True
        assert results[0].message == "источник"
        assert source.saved_meta == []
        assert storage.get_recipe(recipe_id).description == "старое описание"
    finally:
        storage.close()


def test_create_recipe_from_list_rejects_duplicate_title_without_replace(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        engine = RecipeSyncEngine(storage, _device())
        items = [
            ResolvedRecipeListItem(
                requested_query="Яйцо",
                grams=Decimal("50"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-egg",
                    title="Яйцо",
                    portion_id="51772",
                    amount=Decimal("50"),
                    portion_description="г",
                    grams=Decimal("50"),
                ),
                source="FatSecret",
            )
        ]

        try:
            asyncio.run(engine.create_recipe_from_list("group", "омлет", items, updated_by=11))
        except FatSecretError as exc:
            assert "Рецепт с таким именем уже есть" in str(exc)
        else:
            raise AssertionError("expected FatSecretError")

        assert [recipe.title for recipe in storage.list_recipes("group")] == ["Омлет"]
    finally:
        storage.close()


def test_create_recipe_from_list_replaces_existing_recipe_after_new_create(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        existing_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(existing_id, "tg11", "old-11", last_synced_version=1)
        storage.set_remote_recipe_id(existing_id, "tg22", "old-22", last_synced_version=1)
        first = FakeCreateClient("tg11")
        second = FakeCreateClient("tg22")
        first.recipes["old-11"] = Recipe(id="old-11", title="Омлет")
        second.recipes["old-22"] = Recipe(id="old-22", title="Омлет")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Яйцо",
                grams=Decimal("50"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-egg",
                    title="Яйцо",
                    portion_id="51772",
                    amount=Decimal("50"),
                    portion_description="г",
                    grams=Decimal("50"),
                ),
                source="FatSecret",
            )
        ]

        created = asyncio.run(
            engine.create_recipe_from_list(
                "group",
                "Омлет",
                items,
                updated_by=11,
                portions=Decimal("2"),
                steps=["Взбить", "Запечь"],
                replace_existing_recipe_id=existing_id,
            )
        )
        stored = storage.get_recipe(created.recipe_id)

        assert created.title == "Омлет"
        assert created.temporary_title == "Омлет 2"
        assert created.replaced_recipe_id == existing_id
        assert all(result.ok for result in created.replacement_results)
        assert all(result.ok for result in created.rename_results)
        assert first.created_recipe is not None
        assert first.created_recipe.title == "Омлет 2"
        assert first.deleted_recipe_ids == ["old-11"]
        assert second.deleted_recipe_ids == ["old-22"]
        assert [meta.title for meta in first.saved_meta] == ["Омлет 2", "Омлет"]
        assert stored is not None
        assert stored.title == "Омлет"
        assert stored.portions == Decimal("2")
        assert stored.steps == ["Взбить", "Запечь"]
        assert created.recipe_id == existing_id
        assert storage.get_recipe(existing_id) is not None
        assert storage.remote_ids(created.recipe_id) == {"tg11": "remote-tg11", "tg22": "remote-tg22"}
        assert [recipe.id for recipe in storage.list_recipes("group")] == [created.recipe_id]
    finally:
        storage.close()


def test_create_recipe_from_list_replaces_live_recipe_ref_after_new_create(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        first = FakeCreateClient("tg11")
        second = FakeCreateClient("tg22")
        first.recipes["old-11"] = Recipe(id="old-11", title="Омлет")
        first.recipes["old-12"] = Recipe(id="old-12", title="Омлет")
        second.recipes["old-22"] = Recipe(id="old-22", title="Омлет")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "old-11", "tg22": "old-22"},
            remote_ids_by_account={"tg11": ["old-11", "old-12"], "tg22": ["old-22"]},
        )
        items = [
            ResolvedRecipeListItem(
                requested_query="Яйцо",
                grams=Decimal("50"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-egg",
                    title="Яйцо",
                    portion_id="51772",
                    amount=Decimal("50"),
                    portion_description="г",
                    grams=Decimal("50"),
                ),
                source="FatSecret",
            )
        ]

        created = asyncio.run(
            engine.create_recipe_from_list(
                "group",
                "Омлет",
                items,
                updated_by=11,
                replace_existing_recipe_ref=recipe_ref,
            )
        )
        stored = storage.get_recipe(created.recipe_id)

        assert created.title == "Омлет"
        assert created.temporary_title == "Омлет 2"
        assert created.replaced_recipe_id == "live-omlet"
        assert all(result.ok for result in created.replacement_results)
        assert all(result.ok for result in created.rename_results)
        assert first.created_recipe is not None
        assert first.created_recipe.title == "Омлет 2"
        assert first.deleted_recipe_ids == ["old-11", "old-12"]
        assert second.deleted_recipe_ids == ["old-22"]
        assert [meta.title for meta in first.saved_meta] == ["Омлет 2", "Омлет"]
        assert stored is not None
        assert stored.title == "Омлет"
        assert storage.remote_ids(created.recipe_id) == {"tg11": "remote-tg11", "tg22": "remote-tg22"}
        assert [recipe.id for recipe in storage.list_recipes("group")] == [created.recipe_id]
    finally:
        storage.close()


def test_create_recipe_from_list_resumes_local_finalization_without_remote_recreate(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        existing_id = storage.create_recipe("Омлет", "старый", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(existing_id, "tg11", "old-11", last_synced_version=1)
        client = FakeCreateClient("tg11")
        client.recipes["old-11"] = Recipe(id="old-11", title="Омлет", description="старый")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Яйцо",
                grams=Decimal("50"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-egg",
                    title="Яйцо",
                    portion_id="51772",
                    amount=Decimal("50"),
                    portion_description="г",
                    grams=Decimal("50"),
                ),
                source="FatSecret",
            )
        ]
        original_finalize = storage.finalize_recipe_list_run
        finalize_calls = 0

        def fail_once(run_id, recipe, remote_ids):  # noqa: ANN001
            nonlocal finalize_calls
            finalize_calls += 1
            if finalize_calls == 1:
                raise RuntimeError("injected local finalization failure")
            return original_finalize(run_id, recipe, remote_ids)

        storage.finalize_recipe_list_run = fail_once  # type: ignore[method-assign]

        with pytest.raises(FatSecretError, match="remote ID сохранены"):
            asyncio.run(
                engine.create_recipe_from_list(
                    "group",
                    "Омлет",
                    items,
                    updated_by=11,
                    portions=Decimal("2"),
                    replace_existing_recipe_id=existing_id,
                )
            )

        assert client.create_calls == 1
        assert client.deleted_recipe_ids == ["old-11"]
        assert storage.remote_ids(existing_id) == {"tg11": "old-11"}
        assert storage._conn.execute("SELECT status FROM recipe_list_runs").fetchone()[0] == "recovery_pending"

        resumed = asyncio.run(
            engine.create_recipe_from_list(
                "group",
                "Омлет",
                items,
                updated_by=11,
                portions=Decimal("2"),
                replace_existing_recipe_id=existing_id,
            )
        )

        assert resumed.recipe_id == existing_id
        assert client.create_calls == 1
        assert client.deleted_recipe_ids == ["old-11"]
        assert storage.remote_ids(existing_id) == {"tg11": "remote-tg11"}
        assert storage._conn.execute("SELECT status FROM recipe_list_runs").fetchone()[0] == "completed"
    finally:
        storage.close()


def test_create_recipe_from_list_deletes_local_recipe_when_every_account_fails(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {  # type: ignore[method-assign]
            "tg11": FakeFailingCreateClient("tg11"),
            "tg22": FakeFailingCreateClient("tg22"),
        }
        items = [
            ResolvedRecipeListItem(
                requested_query="Филе",
                grams=Decimal("100"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-1",
                    title="Куриное Филе",
                    portion_id="portion-1",
                    amount=Decimal("100"),
                    portion_description="г",
                ),
                source="FatSecret",
            )
        ]

        try:
            asyncio.run(engine.create_recipe_from_list("group", "Тест", items, updated_by=11))
        except FatSecretError as exc:
            assert "Локальный черновик удален" in str(exc)
        else:
            raise AssertionError("expected FatSecretError")

        assert storage.list_recipes("group") == []
    finally:
        storage.close()


def test_create_recipe_from_list_rolls_back_remote_when_ingredient_is_rejected(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeRejectIngredientCreateClient("tg11", "Морская Соль")
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Соль",
                grams=Decimal("9"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-salt",
                    title="Морская Соль",
                    portion_id="0",
                    amount=Decimal("0.09"),
                    portion_description="100г",
                ),
                source="FatSecret",
            )
        ]

        try:
            asyncio.run(engine.create_recipe_from_list("group", "Котлета тест", items, updated_by=11))
        except FatSecretError as exc:
            assert "FatSecret не принял ингредиент «Морская Соль»" in str(exc)
            assert "созданный рецепт remote-tg11 удален после ошибки" in str(exc)
        else:
            raise AssertionError("expected FatSecretError")

        assert client.deleted_recipe_ids == ["remote-tg11"]
        assert storage.list_recipes("group") == []
    finally:
        storage.close()


def test_create_recipe_from_list_rolls_back_successful_accounts_when_any_account_fails(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        first = FakeCreateClient("tg11")
        second = FakeRejectIngredientCreateClient("tg22", "Лук")
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Лук",
                grams=Decimal("119"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-onion",
                    title="Лук",
                    portion_id="0",
                    amount=Decimal("1.19"),
                    portion_description="100г",
                ),
                source="FatSecret",
            )
        ]

        try:
            asyncio.run(engine.create_recipe_from_list("group", "Котлета тест", items, updated_by=11))
        except FatSecretError as exc:
            assert "FatSecret не принял ингредиент «Лук»" in str(exc)
            assert "созданный рецепт remote-tg11 удален после ошибки" in str(exc)
            assert "созданный рецепт remote-tg22 удален после ошибки" in str(exc)
        else:
            raise AssertionError("expected FatSecretError")

        assert first.deleted_recipe_ids == ["remote-tg11"]
        assert second.deleted_recipe_ids == ["remote-tg22"]
        assert storage.list_recipes("group") == []
    finally:
        storage.close()


def test_create_recipe_from_list_retries_ingredient_with_legacy_addable_id(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeLegacyAddableCreateClient(
            "tg11",
            FoodSearchResult(
                food_id="legacy-onion",
                title="Лук Репчатый",
                default_portion_id="59173",
                default_portion_description="100г",
            ),
        )
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Лук",
                grams=Decimal("119"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="app-onion",
                    title="Лук",
                    portion_id="0",
                    amount=Decimal("1.19"),
                    portion_description="100г",
                ),
                source="FatSecret",
            )
        ]

        created = asyncio.run(engine.create_recipe_from_list("group", "Котлета тест", items, updated_by=11))
        recipe = storage.get_recipe(created.recipe_id)

        assert [item.food_id for item in client.saved_ingredients] == ["app-onion", "legacy-onion"]
        assert client.saved_ingredients[1].portion_id == "59173"
        assert client.saved_ingredients[1].amount == Decimal("1.19")
        assert client.saved_ingredients[1].portion_description == "100г"
        assert client.deleted_recipe_ids == []
        assert recipe is not None
        assert recipe.ingredients[0].food_id == "legacy-onion"
        assert recipe.ingredients[0].title == "Лук Репчатый"
    finally:
        storage.close()


def test_create_recipe_from_list_tries_selected_food_before_different_addable_match(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeAcceptSelectedFoodCreateClient(
            "tg11",
            FoodSearchResult(
                food_id="1517",
                title="Бекон",
                default_portion_id="50197",
                default_portion_description="г",
            ),
        )
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Бекон",
                grams=Decimal("80"),
                ingredient=Ingredient(
                    id="ingredient-bacon",
                    recipe_id="",
                    food_id="83623982",
                    title="Бекон",
                    portion_id="0",
                    amount=Decimal("0.8"),
                    portion_description="100г",
                    grams=Decimal("80"),
                ),
                source="FatSecret",
                brand="Мираторг",
            )
        ]

        created = asyncio.run(engine.create_recipe_from_list("group", "Омлет", items, updated_by=11))
        recipe = storage.get_recipe(created.recipe_id)

        assert [item.food_id for item in client.saved_ingredients] == ["83623982"]
        assert client.saved_ingredients[0].amount == Decimal("0.8")
        assert client.saved_ingredients[0].portion_description == "100г"
        assert recipe is not None
        assert recipe.ingredients[0].food_id == "83623982"
    finally:
        storage.close()


def test_create_recipe_from_list_prepares_real_gram_portion_before_add(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeLegacyAddableCreateClient(
            "tg11",
            FoodSearchResult(
                food_id="food-egg",
                title="Яйцо",
                default_portion_id="10270",
                default_portion_description="средний",
                energy_per_portion=Decimal("147"),
                protein_per_portion=Decimal("12.58"),
                fat_per_portion=Decimal("9.94"),
                carbohydrate_per_portion=Decimal("0.77"),
                raw={"_gram_portion_id": "51772", "_gram_portion_description": "г"},
            ),
        )
        engine._build_clients = lambda group_id=None: {"tg11": client}  # type: ignore[method-assign]
        items = [
            ResolvedRecipeListItem(
                requested_query="Яйцо",
                grams=Decimal("55"),
                ingredient=Ingredient(
                    id="ingredient-1",
                    recipe_id="",
                    food_id="food-egg",
                    title="Яйцо",
                    portion_id="0",
                    amount=Decimal("0.55"),
                    portion_description="100г",
                    grams=Decimal("55"),
                ),
                source="FatSecret",
            )
        ]

        created = asyncio.run(engine.create_recipe_from_list("group", "Омлет", items, updated_by=11))
        recipe = storage.get_recipe(created.recipe_id)

        assert client.saved_ingredients[0].food_id == "food-egg"
        assert client.saved_ingredients[0].portion_id == "51772"
        assert client.saved_ingredients[0].amount == Decimal("55")
        assert client.saved_ingredients[0].portion_description == "г"
        assert recipe is not None
        assert recipe.ingredients[0].portion_id == "51772"
        assert recipe.ingredients[0].grams == Decimal("55")
    finally:
        storage.close()


def test_load_remote_recipe_index_merges_live_cookbooks_by_title(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {  # type: ignore[method-assign]
            "tg11": FakeCookbookClient(
                [
                    RecipeSummary(remote_id="111", title="Омлет"),
                    RecipeSummary(remote_id="112", title="омлет"),
                ],
                "tg11",
            ),
            "tg22": FakeCookbookClient(
                [
                    RecipeSummary(remote_id="222", title="омлет"),
                    RecipeSummary(remote_id="333", title="Салат"),
                ],
                "tg22",
            ),
        }

        recipes = asyncio.run(engine.load_remote_recipe_index("group"))

        assert [(recipe.title, recipe.remote_ids) for recipe in recipes] == [
            ("Омлет", {"tg11": "111", "tg22": "222"}),
            ("Салат", {"tg22": "333"}),
        ]
        assert recipes[0].remote_ids_by_account == {"tg11": ["111", "112"], "tg22": ["222"]}
        assert storage.list_recipes("group") == []
    finally:
        storage.close()


def test_hydrate_live_recipe_variants_keeps_conflicting_account_versions(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        first_recipe = Recipe(
            id="111",
            title="Омлет",
            portions=Decimal("2"),
            steps=["Смешать"],
            ingredients=[
                Ingredient("a", "111", "egg", "Яйцо", "0", Decimal("1"), "100г", grams=Decimal("100"))
            ],
        )
        second_recipe = Recipe(
            id="222",
            title="Омлет",
            portions=Decimal("4"),
            steps=["Запечь"],
            ingredients=[
                Ingredient("b", "222", "egg", "Яйцо", "0", Decimal("2"), "100г", grams=Decimal("200"))
            ],
        )
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
            remote_ids_by_account={"tg11": ["111"], "tg22": ["222"]},
        )
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {  # type: ignore[method-assign]
            "tg11": FakeFatSecretClient(first_recipe, account_key="tg11"),
            "tg22": FakeFatSecretClient(second_recipe, account_key="tg22"),
        }

        variants = asyncio.run(engine.hydrate_live_recipe_variants(recipe_ref))

        assert [(item.account_key, item.remote_recipe_id) for item in variants] == [
            ("tg11", "111"),
            ("tg22", "222"),
        ]
        assert len({item.fingerprint.digest for item in variants}) == 2
        assert storage.remote_recipe_snapshot("tg11", "111") is not None
        assert storage.remote_recipe_snapshot("tg22", "222") is not None
    finally:
        storage.close()


def test_verify_remote_recipe_accepts_server_portion_and_metadata_canonicalization(tmp_path) -> None:
    expected = Recipe(
        id="local",
        title="Куриные котлеты",
        description="expected",
        portions=Decimal("2"),
        steps=["Приготовить"],
        ingredients=[
            Ingredient(
                "flour",
                "local",
                "18138808",
                "Пшеничная мука",
                "0",
                Decimal("0.4"),
                "100г",
                grams=Decimal("40"),
            )
        ],
    )
    actual = Recipe(
        id="remote",
        title="FatSecret canonical title",
        description="actual",
        portions=Decimal("1"),
        steps=[],
        ingredients=[
            Ingredient(
                "remote-flour",
                "remote",
                "18138808",
                "ПШЕНИЧНАЯ МУКА",
                "0",
                Decimal("0.4"),
                "serving",
                remote_ingredient_id="iid-flour",
                grams=None,
            )
        ],
    )
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeFatSecretClient(actual, account_key="tg11")

        verified = asyncio.run(engine._verify_remote_recipe(client, "tg11", "remote", expected))

        assert verified.ingredients[0].portion_description == "serving"
        assert verified.ingredients[0].grams is None
        assert storage.remote_recipe_snapshot("tg11", "remote") is not None
    finally:
        storage.close()


@pytest.mark.parametrize(
    ("actual_ingredients", "message"),
    [
        ([], "не добавлен продукт «Пшеничная мука»"),
        (
            [
                Ingredient("flour", "remote", "18138808", "Пшеничная мука", "0", Decimal("0.4")),
                Ingredient("extra", "remote", "999", "Лишний продукт", "0", Decimal("1")),
            ],
            "добавлен лишний продукт «Лишний продукт»",
        ),
    ],
)
def test_verify_remote_recipe_rejects_missing_or_unexpected_products(
    tmp_path,
    actual_ingredients: list[Ingredient],
    message: str,
) -> None:
    expected = Recipe(
        id="local",
        title="Котлеты",
        ingredients=[
            Ingredient("flour", "local", "18138808", "Пшеничная мука", "0", Decimal("0.4"))
        ],
    )
    actual = Recipe(id="remote", title="Котлеты", ingredients=actual_ingredients)
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        client = FakeFatSecretClient(actual, account_key="tg11")

        with pytest.raises(FatSecretError, match=message):
            asyncio.run(engine._verify_remote_recipe(client, "tg11", "remote", expected))

        assert storage.remote_recipe_snapshot("tg11", "remote") is None
    finally:
        storage.close()


def test_load_remote_recipe_index_reconciles_recipes_deleted_outside_bot(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        stale_id = storage.create_recipe(
            "Блины тонкие",
            "",
            Decimal("1"),
            0,
            0,
            updated_by=11,
            group_id="group",
        )
        storage.set_remote_recipe_id(stale_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(stale_id, "tg22", "222", last_synced_version=1)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {  # type: ignore[method-assign]
            "tg11": FakeCookbookClient([], "tg11"),
            "tg22": FakeCookbookClient([], "tg22"),
        }

        recipes = asyncio.run(engine.load_remote_recipe_index("group"))

        assert recipes == []
        assert storage.get_recipe(stale_id) is None
        assert storage.find_recipe_by_title("group", "Блины тонкие") is None
        assert storage.next_available_recipe_title("group", "Блины тонкие") == "Блины тонкие"
    finally:
        storage.close()


def test_load_remote_recipe_index_does_not_reconcile_partial_snapshot(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {  # type: ignore[method-assign]
            "tg11": FakeCookbookClient([], "tg11"),
            "tg22": FakeFailingCookbookClient([], "tg22"),
        }

        try:
            asyncio.run(engine.load_remote_recipe_index("group"))
        except RuntimeError as exc:
            assert str(exc) == "cookbook failed"
        else:
            raise AssertionError("expected cookbook failure")

        assert storage.remote_ids(recipe_id) == {"tg11": "111", "tg22": "222"}
        assert storage.get_recipe(recipe_id) is not None
    finally:
        storage.close()


def test_sync_live_recipe_from_source_does_not_create_local_recipe_rows(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        source_recipe = Recipe(id="111", title="Омлет", group_id="group")
        source_recipe.ingredients = [
            Ingredient(
                id="src-1",
                recipe_id="111",
                food_id="food-1",
                title="Яйцо",
                portion_id="portion-1",
                amount=Decimal("2"),
            )
        ]
        target_recipe = Recipe(
            id="222",
            title="Омлет",
            group_id="group",
            ingredients=[
                Ingredient(
                    id="iid-extra",
                    recipe_id="222",
                    food_id="food-extra",
                    title="Лишнее",
                    portion_id="portion-extra",
                    amount=Decimal("1"),
                    remote_ingredient_id="iid-extra",
                )
            ],
        )
        recipe_ref = Recipe(
            id="local-live",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
            remote_ids_by_account={"tg11": ["111", "112"], "tg22": ["222"]},
        )
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(source_recipe, account_key="tg11")
        second = FakeFatSecretClient(target_recipe, account_key="tg22")
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert synced.id == "local-live"
        assert synced.remote_ids == {"tg11": "111", "tg22": "222-created-1"}
        assert [result.ok for result in results] == [True, True]
        assert first.saved_meta == []
        assert second.saved_ingredients[0].title == "Яйцо"
        assert second.deleted_ingredient_ids == []
        assert second.deleted_recipe_ids == ["222"]
        assert results[1].message == "добавлено ингредиентов: 1"
        assert storage.list_recipes("group") == []
    finally:
        storage.close()


def test_sync_live_recipe_copies_raw_yogurt_payload_and_returns_normalized_display(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        source_recipe = Recipe(
            id="95538732",
            title="Блины",
            description="1",
            portions=Decimal("10"),
            prep_time=1,
            cook_time=1,
            group_id="group",
            ingredients=[
                Ingredient(
                    id="source-yogurt-iid",
                    recipe_id="95538732",
                    food_id="93062070",
                    title="Йогурт Черника",
                    portion_id="0",
                    amount=Decimal("1.080"),
                    portion_description="serving",
                    remote_ingredient_id="source-yogurt-iid",
                )
            ],
        )
        target_recipe = Recipe(id="132400676", title="Блины", group_id="group")
        recipe_ref = Recipe(
            id="live-pancakes",
            title="Блины",
            group_id="group",
            remote_ids={"tg11": "95538732", "tg22": "132400676"},
            remote_ids_by_account={"tg11": ["95538732"], "tg22": ["132400676"]},
        )
        yogurt_detail = FoodSearchResult(
            food_id="93062070",
            title="Йогурт Черника",
            default_portion_id="0",
            default_portion_description="100g",
            raw={
                "_gram_portion_id": "74562979",
                "_gram_portion_description": "100g",
            },
        )
        source = FakeFatSecretClient(source_recipe, account_key="tg11", details={"93062070": yogurt_detail})
        target = FakeFatSecretClient(target_recipe, account_key="tg22")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert all(result.ok for result in results)
        assert len(target.saved_ingredients) == 1
        copied = target.saved_ingredients[0]
        assert copied.food_id == "93062070"
        assert copied.portion_id == "0"
        assert copied.amount == Decimal("1.080")
        assert copied.portion_description == "serving"
        assert copied.remote_ingredient_id is None
        assert source.saved_meta == []
        assert target.saved_meta[0].description == "1"
        assert target.saved_meta[0].portions == Decimal("10")
        assert target.saved_meta[0].prep_time == 1
        assert target.saved_meta[0].cook_time == 1
        assert synced.ingredients[0].portion_id == "74562979"
        assert synced.ingredients[0].amount == Decimal("1.080")
        assert synced.ingredients[0].grams == Decimal("108.000")
    finally:
        storage.close()


def test_sync_live_recipe_swap_does_not_depend_on_deleting_old_ingredients(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        source_recipe = Recipe(id="111", title="Омлет", group_id="group")
        target_recipe = Recipe(
            id="222",
            title="Омлет",
            group_id="group",
            ingredients=[
                Ingredient(
                    id="iid-extra",
                    recipe_id="222",
                    food_id="food-extra",
                    title="Лишнее",
                    portion_id="portion-extra",
                    amount=Decimal("1"),
                    remote_ingredient_id="iid-extra",
                )
            ],
        )
        recipe_ref = Recipe(
            id="local-live",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
            remote_ids_by_account={"tg11": ["111"], "tg22": ["222"]},
        )
        source = FakeFatSecretClient(source_recipe, account_key="tg11")
        target = FakeFatSecretClient(target_recipe, account_key="tg22", ingredient_delete_ok=False)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert [result.ok for result in results] == [True, True]
        assert target.deleted_ingredient_ids == []
        assert target.deleted_recipe_ids == ["222"]
        assert synced.remote_ids == {"tg11": "111", "tg22": "222-created-1"}
    finally:
        storage.close()


def test_sync_live_recipe_rolls_back_new_target_after_ingredient_failure(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        source_recipe = Recipe(
            id="111",
            title="Омлет",
            group_id="group",
            ingredients=[
                Ingredient(
                    id="src-1",
                    recipe_id="111",
                    food_id="food-1",
                    title="Яйцо",
                    portion_id="portion-1",
                    amount=Decimal("2"),
                )
            ],
        )
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111"},
            remote_ids_by_account={"tg11": ["111"]},
        )
        source = FakeFatSecretClient(source_recipe, account_key="tg11")
        target = FakeCreatedSyncTargetClient("new-222", "tg22")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert [result.ok for result in results] == [True, False]
        assert results[1].remote_recipe_id == "new-222"
        assert "созданный рецепт new-222 удален после ошибки" in results[1].message
        assert target.created_recipe is not None
        assert target.deleted_recipe_ids == ["new-222"]
        assert synced.remote_ids == {"tg11": "111"}
        assert synced.remote_ids_by_account == {"tg11": ["111"]}
    finally:
        storage.close()


def test_sync_local_recipe_rolls_back_new_target_after_ingredient_failure(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        local_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(local_id, "tg11", "111", last_synced_version=1)
        source_recipe = Recipe(
            id="111",
            title="Омлет",
            group_id="group",
            ingredients=[
                Ingredient(
                    id="src-1",
                    recipe_id="111",
                    food_id="food-1",
                    title="Яйцо",
                    portion_id="portion-1",
                    amount=Decimal("2"),
                )
            ],
        )
        source = FakeFatSecretClient(source_recipe, account_key="tg11")
        target = FakeCreatedSyncTargetClient("new-222", "tg22")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        results = asyncio.run(engine.sync_recipe_from_source(local_id, "tg11"))

        assert [result.ok for result in results] == [True, False]
        assert target.deleted_recipe_ids == ["new-222"]
        assert storage.remote_ids(local_id) == {"tg11": "111"}
        assert "созданный рецепт new-222 удален после ошибки" in results[1].message
    finally:
        storage.close()


def test_sync_live_recipe_keeps_new_target_mapping_when_rollback_fails(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        source_recipe = Recipe(
            id="111",
            title="Омлет",
            group_id="group",
            ingredients=[
                Ingredient(
                    id="src-1",
                    recipe_id="111",
                    food_id="food-1",
                    title="Яйцо",
                    portion_id="portion-1",
                    amount=Decimal("2"),
                )
            ],
        )
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111"},
            remote_ids_by_account={"tg11": ["111"]},
        )
        source = FakeFatSecretClient(source_recipe, account_key="tg11")
        target = FakeCreatedSyncTargetClient("new-222", "tg22", delete_ok=False)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert [result.ok for result in results] == [True, False]
        assert target.deleted_recipe_ids == ["new-222"]
        assert synced.remote_ids == {"tg11": "111", "tg22": "new-222"}
        assert synced.remote_ids_by_account == {"tg11": ["111"], "tg22": ["new-222"]}
        assert "не удалось удалить после ошибки" in results[1].message
    finally:
        storage.close()


def test_sync_live_recipe_rolls_back_swap_copy_without_deleting_preexisting_target(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        source_recipe = Recipe(
            id="111",
            title="Омлет",
            group_id="group",
            ingredients=[
                Ingredient(
                    id="src-1",
                    recipe_id="111",
                    food_id="food-1",
                    title="Яйцо",
                    portion_id="portion-1",
                    amount=Decimal("2"),
                )
            ],
        )
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
            remote_ids_by_account={"tg11": ["111"], "tg22": ["222"]},
        )
        source = FakeFatSecretClient(source_recipe, account_key="tg11")
        target = FakeCreatedSyncTargetClient("222", "tg22", created_remote_id="333")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": source, "tg22": target}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert [result.ok for result in results] == [True, False]
        assert target.created_recipe is not None
        assert target.deleted_recipe_ids == ["333"]
        assert "222" in target.recipes
        assert synced.remote_ids == {"tg11": "111", "tg22": "222"}
    finally:
        storage.close()


def test_ambiguous_ingredient_timeout_resumes_from_fresh_target_diff(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        expected = Recipe(
            id="live",
            title="Омлет",
            ingredients=[
                Ingredient("egg", "live", "food-egg", "Яйцо", "0", Decimal("1"), "100г")
            ],
        )
        client = FakeTimeoutAfterIngredientClient(Recipe(id="base", title="unused"), "tg22")
        engine = RecipeSyncEngine(storage, _device())

        remote_id, stats, created = asyncio.run(
            engine._synchronize_target_recipe(
                client,
                "tg22",
                expected,
                None,
                persist_mapping=False,
            )
        )

        assert created is True
        assert remote_id == "base-created-1"
        assert len(client.saved_ingredients) == 1
        assert stats.unchanged == 1
        assert len(client.recipes[remote_id].ingredients) == 1
    finally:
        storage.close()


def test_verified_sync_rejects_stale_detail_and_rolls_back_new_target(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        expected = Recipe(
            id="live",
            title="Омлет",
            ingredients=[
                Ingredient("egg", "live", "food-egg", "Яйцо", "0", Decimal("1"), "100г")
            ],
        )
        client = FakeStaleDetailClient(Recipe(id="base", title="unused"), account_key="tg22")
        engine = RecipeSyncEngine(storage, _device())

        with pytest.raises(FatSecretError) as error:
            asyncio.run(
                engine._synchronize_target_recipe(
                    client,
                    "tg22",
                    expected,
                    None,
                    persist_mapping=False,
                )
            )

        assert "неверный набор продуктов" in str(error.value)
        assert "не добавлен продукт «Яйцо»" in str(error.value)
        assert client.deleted_recipe_ids == ["base-created-1"]
        assert "base-created-1" not in client.recipes
    finally:
        storage.close()


@pytest.mark.parametrize("disappeared, expected_calls", [(True, 1), (False, 2)])
def test_ambiguous_delete_uses_cookbook_readback_before_optional_retry(
    tmp_path,
    disappeared: bool,
    expected_calls: int,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        client = FakeAmbiguousDeleteClient(
            Recipe(id="222", title="Омлет"),
            "tg22",
            disappeared=disappeared,
        )
        engine = RecipeSyncEngine(storage, _device())

        deleted = asyncio.run(engine._delete_remote_recipe_confirmed(client, "222"))

        assert deleted is True
        assert client.delete_calls == expected_calls
        assert "222" not in client.recipes
    finally:
        storage.close()


def test_non_transient_delete_error_is_not_replayed_after_readback(tmp_path) -> None:
    class RedirectDeleteClient(FakeFatSecretClient):
        def __init__(self) -> None:
            super().__init__(Recipe(id="222", title="Омлет"), "tg22")
            self.delete_calls = 0

        async def delete_recipe(self, remote_recipe_id: str) -> bool:
            self.delete_calls += 1
            raise FatSecretActionError(
                "redirect",
                status_code=302,
                page="RecipeActionAndroidPage.aspx",
                action="recipedelete",
                location="/ErrorLogUserFeedback.ashx",
                replayed=True,
            )

    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        client = RedirectDeleteClient()
        engine = RecipeSyncEngine(storage, _device())

        with pytest.raises(FatSecretActionError):
            asyncio.run(engine._delete_remote_recipe_confirmed(client, "222"))

        assert client.delete_calls == 1
        assert "222" in client.recipes
    finally:
        storage.close()


def test_recipe_swap_resumes_after_old_target_deleted_and_final_rename_failed(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        expected = Recipe(
            id="live-omlet",
            title="Омлет",
            ingredients=[
                Ingredient("egg", "live-omlet", "food-egg", "Яйцо", "0", Decimal("1"), "100г")
            ],
        )
        client = FakeFinalRenameFailureClient(Recipe(id="222", title="Старый"), "tg22")
        engine = RecipeSyncEngine(storage, _device())

        with pytest.raises(FatSecretError, match="final rename interrupted"):
            asyncio.run(
                engine._synchronize_target_recipe(
                    client,
                    "tg22",
                    expected,
                    "222",
                    persist_mapping=False,
                )
            )
        runs = storage.incomplete_recipe_swap_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "old_deleted"

        remote_id, _, swapped = asyncio.run(
            engine._synchronize_target_recipe(
                client,
                "tg22",
                expected,
                "222",
                persist_mapping=False,
            )
        )

        assert swapped is True
        assert remote_id == "222-created-1"
        assert storage.incomplete_recipe_swap_runs() == []
        assert client.recipes[remote_id].title == "Омлет"
    finally:
        storage.close()


def test_delete_live_recipes_everywhere_does_not_require_local_recipe_rows(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_ref = Recipe(
            id="local-live",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
            remote_ids_by_account={"tg11": ["111", "112"], "tg22": ["222"]},
        )
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22")
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_live_recipes_everywhere([recipe_ref]))

        assert all(result.ok for result in results["local-live"])
        assert first.deleted_recipe_ids == ["111", "112"]
        assert second.deleted_recipe_ids == ["222"]
        assert storage.list_recipes("group") == []
    finally:
        storage.close()


def test_delete_live_recipe_removes_matching_local_mappings(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        local_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(local_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(local_id, "tg22", "222", last_synced_version=1)
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
        )
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_live_recipe_everywhere(recipe_ref))

        assert all(result.ok for result in results)
        assert storage.get_recipe(local_id) is None
    finally:
        storage.close()


def test_delete_live_recipe_keeps_failed_local_mapping(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        local_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(local_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(local_id, "tg22", "222", last_synced_version=1)
        recipe_ref = Recipe(
            id="live-omlet",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
        )
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22", delete_ok=False)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_live_recipe_everywhere(recipe_ref))

        assert [result.ok for result in results] == [True, False]
        assert storage.remote_ids(local_id) == {"tg22": "222"}
        assert storage.get_recipe(local_id) is not None
    finally:
        storage.close()


def test_delete_recipe_everywhere_deletes_all_mappings_and_local_recipe(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11)
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22")
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_recipe_everywhere(recipe_id))

        assert all(result.ok for result in results)
        assert first.deleted_recipe_ids == ["111"]
        assert second.deleted_recipe_ids == ["222"]
        assert storage.get_recipe(recipe_id) is None
        assert storage.remote_ids(recipe_id) == {}
    finally:
        storage.close()


def test_delete_recipe_everywhere_keeps_failed_remote_mapping(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11)
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22", delete_ok=False)
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_recipe_everywhere(recipe_id))

        assert [result.ok for result in results] == [True, False]
        assert storage.get_recipe(recipe_id) is not None
        assert storage.remote_ids(recipe_id) == {"tg22": "222"}
    finally:
        storage.close()


def _qa_custom_food_definition(*, barcode: str = "") -> CustomFoodDefinition:
    return CustomFoodDefinition(
        source_recipe_id="",
        title="QA Group Product",
        manufacturer_name="",
        serving_type="Per100g",
        serving_size="100",
        metric_serving_size="100g",
        nutrients={
            "calories": Decimal("321"),
            "protein": Decimal("12.3"),
            "totalFat": Decimal("4.5"),
            "carbohydrate": Decimal("56.7"),
        },
        barcode=barcode,
        barcode_type="EAN_13" if barcode else "",
    )


def test_suggest_custom_food_brands_ranks_canonical_matches_and_caches_catalog(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        client = FakeGroupCustomFoodClient("a1")
        client.brand_catalog = [
            "Santa Maria",
            "Санта Ритейл",
            "Санта",
            "Санта Бремор",
            "Несквик",
        ]
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"a1": client}  # type: ignore[method-assign]

        first = asyncio.run(engine.suggest_custom_food_brands("group", "  санта  "))
        second = asyncio.run(engine.suggest_custom_food_brands("group", "санта б"))

        assert first == ["Санта", "Санта Бремор", "Санта Ритейл"]
        assert second == ["Санта Бремор"]
        assert client.brand_catalog_calls == 1
    finally:
        storage.close()


def test_create_custom_food_for_group_journals_verifies_maps_and_reuses(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        first = FakeGroupCustomFoodClient("a1")
        second = FakeGroupCustomFoodClient("a2")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"a1": first, "a2": second}  # type: ignore[method-assign]
        definition = _qa_custom_food_definition(barcode="4006381333931")

        created = asyncio.run(engine.create_custom_food_for_group("group", definition, 11))
        repeated = asyncio.run(engine.create_custom_food_for_group("group", definition, 11))

        assert created.food_ids == {"a1": "a1-food-1", "a2": "a2-food-1"}
        assert repeated.food_ids == created.food_ids
        assert len(first.created_custom_foods) == 1
        assert len(second.created_custom_foods) == 1
        assert first.created_custom_foods[0].barcode == "4006381333931"
        assert second.created_custom_foods[0].barcode == ""
        assert first.remap_calls == [("4006381333931", "a1-food-1", True, None)]
        assert second.remap_calls == []
        assert storage.custom_food_mapping("a1", "a1-food-1", "a2") == "a2-food-1"
        assert storage.custom_food_mapping("a2", "a2-food-1", "a1") == "a1-food-1"
        run = storage.custom_food_run(created.run_id)
        assert run is not None
        assert run["status"] == "completed"
        assert {row["status"] for row in run["accounts"]} == {"verified"}
    finally:
        storage.close()


def test_create_custom_food_for_group_recovers_timeout_from_exact_readback(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        client = FakeGroupCustomFoodClient("a1", timeout_after_first_create=True)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"a1": client}  # type: ignore[method-assign]

        created = asyncio.run(
            engine.create_custom_food_for_group("group", _qa_custom_food_definition(), 11)
        )

        assert created.food_ids == {"a1": "a1-food-1"}
        assert len(client.created_custom_foods) == 1
        assert storage.custom_food_run(created.run_id)["status"] == "completed"  # type: ignore[index]
    finally:
        storage.close()


def test_create_custom_food_for_group_recovers_applied_barcode_remap_timeout(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        client = FakeAmbiguousBarcodeClient("a1", mapping_applied=True)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"a1": client}  # type: ignore[method-assign]

        created = asyncio.run(
            engine.create_custom_food_for_group(
                "group",
                _qa_custom_food_definition(barcode="4006381333931"),
                11,
            )
        )

        assert created.food_ids == {"a1": "a1-food-1"}
        assert len(client.remap_calls) == 1
        assert storage.custom_food_run(created.run_id)["status"] == "completed"  # type: ignore[index]
    finally:
        storage.close()


def test_create_custom_food_for_group_never_replays_uncertain_barcode_remap(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        client = FakeAmbiguousBarcodeClient("a1", mapping_applied=False)
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"a1": client}  # type: ignore[method-assign]
        definition = _qa_custom_food_definition(barcode="4006381333931")

        with pytest.raises(FatSecretError, match="Продукт создан не во всех аккаунтах"):
            asyncio.run(engine.create_custom_food_for_group("group", definition, 11))
        with pytest.raises(FatSecretError, match="Повторная отправка отключена"):
            asyncio.run(engine.create_custom_food_for_group("group", definition, 11))

        assert len(client.remap_calls) == 1
        run = storage.matching_custom_food_run("group", _custom_food_request_fingerprint(definition))
        assert run is not None
        assert run["status"] == "recovery_pending"
        assert run["accounts"][0]["status"] == "barcode_submitting"
    finally:
        storage.close()


def test_recipe_list_uses_account_specific_ids_for_new_custom_food(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        first = FakeFatSecretClient(Recipe(id="a1-seed", title="Seed"), account_key="a1")
        second = FakeFatSecretClient(Recipe(id="a2-seed", title="Seed"), account_key="a2")
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {"a1": first, "a2": second}  # type: ignore[method-assign]
        definition = _qa_custom_food_definition()
        item = ResolvedRecipeListItem(
            requested_query="QA product",
            grams=Decimal("75"),
            ingredient=Ingredient(
                id="draft",
                recipe_id="",
                food_id="a1-food-1",
                title=definition.title,
                portion_id="0",
                amount=Decimal("0.75"),
                portion_description="100г",
                grams=Decimal("75"),
            ),
            source="создан в группе",
            custom_food_ids={"a1": "a1-food-1", "a2": "a2-food-1"},
        )

        created = asyncio.run(
            engine.create_recipe_from_list("group", "QA recipe", [item], 11)
        )

        assert all(result.ok for result in created.results)
        assert [ingredient.food_id for ingredient in first.saved_ingredients] == ["a1-food-1"]
        assert [ingredient.food_id for ingredient in second.saved_ingredients] == ["a2-food-1"]
        stored = storage.get_recipe(created.recipe_id)
        assert stored is not None
        assert [ingredient.food_id for ingredient in stored.ingredients] == ["a1-food-1"]
    finally:
        storage.close()
