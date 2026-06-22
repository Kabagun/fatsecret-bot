from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, FoodSearchResult, Ingredient, Recipe, RecipeSummary
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import FatSecretError, RecipeSyncEngine, ResolvedRecipeListItem, _sync_description


class FakeFatSecretClient:
    def __init__(
        self,
        target: Recipe,
        account_key: str = "target",
        delete_ok: bool = True,
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
        self.delete_ok = delete_ok
        self.saved_ingredients: list[Ingredient] = []
        self.deleted_recipe_ids: list[str] = []
        self.saved_meta: list[Recipe] = []
        self.details = details or {}

    async def get_recipe(self, remote_id: str) -> Recipe:
        assert remote_id == self.target.id
        return self.target

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        assert remote_recipe_id == self.target.id
        self.saved_ingredients.append(ingredient)
        return True

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        self.deleted_recipe_ids.append(remote_recipe_id)
        return self.delete_ok

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        self.saved_meta.append(recipe)
        return True

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        return self.details.get(result.food_id, result)

    async def close(self) -> None:
        return None


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


class FakeFoodUsageClient:
    def __init__(self, recipes: list[Recipe], account_key: str) -> None:
        self.account = FatSecretAccountConfig(
            key=account_key,
            label=account_key,
            username=f"{account_key}@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.recipes = {recipe.id: recipe for recipe in recipes}
        self.closed = False

    async def cookbook(self) -> list[RecipeSummary]:
        return [RecipeSummary(remote_id=recipe.id, title=recipe.title) for recipe in self.recipes.values()]

    async def get_recipe(self, remote_id: str) -> Recipe:
        return self.recipes[remote_id]

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
        self.saved_ingredients: list[Ingredient] = []
        self.deleted_recipe_ids: list[str] = []

    async def create_recipe(self, recipe: Recipe) -> str:
        self.created_recipe = recipe
        return f"remote-{self.account.key}"

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        return True

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        return True

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        self.deleted_recipe_ids.append(remote_recipe_id)
        return True

    async def close(self) -> None:
        return None


class FakeRejectIngredientCreateClient(FakeCreateClient):
    def __init__(self, account_key: str, rejected_title: str) -> None:
        super().__init__(account_key)
        self.rejected_title = rejected_title

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        return ingredient.title != self.rejected_title


class FakeLegacyAddableCreateClient(FakeCreateClient):
    def __init__(self, account_key: str, addable: FoodSearchResult) -> None:
        super().__init__(account_key)
        self.addable = addable
        self.addable_queries: list[str] = []

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        return ingredient.food_id == self.addable.food_id

    async def search_addable_foods(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        self.addable_queries.append(query)
        return [self.addable]


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
        assert stats.extras == 1
        assert client.saved_ingredients[0].remote_ingredient_id == "iid-1"
        assert client.saved_ingredients[0].amount == Decimal("125")
        assert client.saved_ingredients[1].remote_ingredient_id is None
    finally:
        storage.close()


def test_sync_recipe_normalizes_portion_ingredients_to_grams(tmp_path) -> None:
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
        assert target.saved_ingredients[0].portion_id == "gram-portion"
        assert target.saved_ingredients[0].amount == Decimal("150.0")
        assert target.saved_ingredients[0].portion_description == "г"
        assert target.saved_ingredients[0].grams == Decimal("150.0")
        assert synced_recipe is not None
        assert synced_recipe.ingredients[0].grams == Decimal("150.0")
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
        assert candidates[0].ingredient.amount == Decimal("9")
        assert candidates[0].ingredient.portion_description == "г"
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
        assert candidates[0].ingredient.amount == Decimal("9")
        assert candidates[0].ingredient.portion_description == "г"
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
            ]
        )
        engine._build_clients = lambda group_id=None: {"search": client}  # type: ignore[method-assign]

        candidates = asyncio.run(engine.recipe_list_candidates("group", "яйцо куриное", Decimal("50"), limit=1))

        assert candidates[0].ingredient.title == "Яйцо"
        assert candidates[0].ingredient.portion_id == "0"
        assert candidates[0].ingredient.amount == Decimal("0.5")
        assert candidates[0].ingredient.portion_description == "100г"
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

        asyncio.run(
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
    finally:
        storage.close()


def test_sync_description_uses_configured_timezone() -> None:
    value = _sync_description(dt.datetime(2026, 6, 17, 12, 50, tzinfo=dt.UTC), timezone="Europe/Minsk")

    assert value == "Последняя синхронизация: 17.06.2026 15:50"


def test_sync_recipe_updates_source_description_with_last_sync(tmp_path) -> None:
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
        assert results[0].message == "источник; дата обновлена"
        assert source.saved_meta[0].description.startswith("Последняя синхронизация: ")
        assert storage.get_recipe(recipe_id).description.startswith("Последняя синхронизация: ")
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
        assert client.saved_ingredients[1].amount == Decimal("119")
        assert client.saved_ingredients[1].portion_description == "г"
        assert client.deleted_recipe_ids == []
        assert recipe is not None
        assert recipe.ingredients[0].food_id == "legacy-onion"
        assert recipe.ingredients[0].title == "Лук Репчатый"
    finally:
        storage.close()


def test_load_remote_recipe_index_merges_live_cookbooks_by_title(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        engine = RecipeSyncEngine(storage, _device())
        engine._build_clients = lambda group_id=None: {  # type: ignore[method-assign]
            "tg11": FakeCookbookClient([RecipeSummary(remote_id="111", title="Омлет")], "tg11"),
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
        assert storage.list_recipes("group") == []
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
        target_recipe = Recipe(id="222", title="Омлет", group_id="group")
        recipe_ref = Recipe(
            id="local-live",
            title="Омлет",
            group_id="group",
            remote_ids={"tg11": "111", "tg22": "222"},
        )
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(source_recipe, account_key="tg11")
        second = FakeFatSecretClient(target_recipe, account_key="tg22")
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        synced, results = asyncio.run(engine.sync_live_recipe_from_source(recipe_ref, "tg11"))

        assert synced.id == "local-live"
        assert synced.remote_ids == {"tg11": "111", "tg22": "222"}
        assert [result.ok for result in results] == [True, True]
        assert first.saved_meta
        assert second.saved_ingredients[0].title == "Яйцо"
        assert storage.list_recipes("group") == []
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
        )
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22")
        engine._build_clients = lambda group_id=None: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_live_recipes_everywhere([recipe_ref]))

        assert all(result.ok for result in results["local-live"])
        assert first.deleted_recipe_ids == ["111"]
        assert second.deleted_recipe_ids == ["222"]
        assert storage.list_recipes("group") == []
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
