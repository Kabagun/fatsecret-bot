from __future__ import annotations

import asyncio
from decimal import Decimal

from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, FoodSearchResult, Ingredient, Recipe
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import FatSecretError, RecipeSyncEngine, ResolvedRecipeListItem


class FakeFatSecretClient:
    def __init__(self, target: Recipe, account_key: str = "target", delete_ok: bool = True) -> None:
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

    async def close(self) -> None:
        return None


class FakeSearchClient:
    def __init__(self, results: list[FoodSearchResult]) -> None:
        self.account = FatSecretAccountConfig(
            key="search",
            label="search",
            username="search@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.results = results

    async def autocomplete_food(self, query: str) -> list[FoodSearchResult]:
        return list(self.results)

    async def search_recipes(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        return []

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        return result

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

    async def create_recipe(self, recipe: Recipe) -> str:
        self.created_recipe = recipe
        return "remote-1"

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        self.saved_ingredients.append(ingredient)
        return True

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        return True

    async def close(self) -> None:
        return None


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


def test_recipe_list_candidates_prefers_frequent_local_shorter_tie(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        first = storage.create_recipe("A", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        second = storage.create_recipe("B", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        storage.add_ingredient(first, "food-cheese", "Филе Куриное в Сыре", "portion-1", Decimal("100"), "г")
        storage.add_ingredient(second, "food-chicken", "Куриное Филе", "portion-2", Decimal("100"), "г")
        engine = RecipeSyncEngine(storage, _device())

        candidates = asyncio.run(engine.recipe_list_candidates(group.id, "Филе", Decimal("300"), limit=1))

        assert len(candidates) == 1
        assert candidates[0].ingredient.title == "Куриное Филе"
        assert candidates[0].ingredient.amount == Decimal("300")
        assert candidates[0].ingredient.portion_description == "г"
        assert candidates[0].source == "часто использовался"
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


def test_create_recipe_from_list_uses_non_empty_description(tmp_path) -> None:
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

        asyncio.run(engine.create_recipe_from_list("group", "Тест", items, updated_by=11))

        assert client.created_recipe is not None
        assert client.created_recipe.description == "Создано через Telegram бот."
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
