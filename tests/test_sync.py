from __future__ import annotations

import asyncio
from decimal import Decimal

from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, Ingredient, Recipe
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import RecipeSyncEngine


class FakeFatSecretClient:
    def __init__(self, target: Recipe) -> None:
        self.account = FatSecretAccountConfig(
            key="target",
            label="Target",
            username="target@example.com",
            password="secret",
            market="BY",
            language="ru",
        )
        self.target = target
        self.saved_ingredients: list[Ingredient] = []

    async def get_recipe(self, remote_id: str) -> Recipe:
        assert remote_id == self.target.id
        return self.target

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        assert remote_recipe_id == self.target.id
        self.saved_ingredients.append(ingredient)
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
