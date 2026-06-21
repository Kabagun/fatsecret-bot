from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, FoodSearchResult, Ingredient, Recipe, RecipeSummary
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import FatSecretError, RecipeSyncEngine, ResolvedRecipeListItem, _sync_description


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
        self.saved_meta: list[Recipe] = []

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


class FakeSearchClient:
    def __init__(
        self,
        results: list[FoodSearchResult],
        search_results: list[FoodSearchResult] | None = None,
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

    async def autocomplete_food(self, query: str) -> list[FoodSearchResult]:
        return list(self.results)

    async def search_recipes(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        return list(self.search_results)

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
        recipe_id = storage.create_recipe("A", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        storage.add_ingredient(recipe_id, "food-oil", "Масло Растительное", "0", Decimal("10"), "г")
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
        recipe_id = storage.create_recipe("A", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        storage.add_ingredient(recipe_id, "food-chicken", "Куриное Филе", "portion-1", Decimal("300"), "г")
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


def test_recipe_list_candidates_skips_local_when_fatsecret_metadata_missing(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        recipe_id = storage.create_recipe("A", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        storage.add_ingredient(recipe_id, "food-local", "Куриное Филе", "portion-1", Decimal("300"), "г")
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
        recipe_id = storage.create_recipe("Bad", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        storage.add_ingredient(recipe_id, "food-cheese", "Куриное Филе в Сыре", "portion-cheese", Decimal("1"), "100г")
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
