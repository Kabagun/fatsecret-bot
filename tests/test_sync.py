from __future__ import annotations

import asyncio
from decimal import Decimal

from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, Ingredient, Recipe
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import RecipeSyncEngine


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


def test_delete_recipe_everywhere_deletes_all_mappings_and_local_recipe(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11)
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)
        engine = RecipeSyncEngine(storage, _device())
        first = FakeFatSecretClient(Recipe(id="111", title="Омлет"), account_key="tg11")
        second = FakeFatSecretClient(Recipe(id="222", title="Омлет"), account_key="tg22")
        engine._build_clients = lambda: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

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
        engine._build_clients = lambda: {"tg11": first, "tg22": second}  # type: ignore[method-assign]

        results = asyncio.run(engine.delete_recipe_everywhere(recipe_id))

        assert [result.ok for result in results] == [True, False]
        assert storage.get_recipe(recipe_id) is not None
        assert storage.remote_ids(recipe_id) == {"tg22": "222"}
    finally:
        storage.close()
