from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import RecipeSummary
from fatsecret_bot.storage import Storage, normalize_title


def test_normalize_title_collapses_case_and_spaces() -> None:
    assert normalize_title("  Курица   В Соусе ") == normalize_title("курица в соусе")


def test_import_remote_recipe_merges_by_title(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        first = storage.import_remote_recipe("a1", RecipeSummary(remote_id="101", title="Омлет"))
        second = storage.import_remote_recipe("a2", RecipeSummary(remote_id="202", title="омлет"))
        assert first == second
        recipe = storage.get_recipe(first)
        assert recipe is not None
        assert recipe.remote_ids == {"a1": "101", "a2": "202"}
    finally:
        storage.close()


def test_add_ingredient_bumps_version(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=1)
        before = storage.get_recipe(recipe_id)
        ingredient_id = storage.add_ingredient(recipe_id, "4881229", "Куриное Филе", "4751539", Decimal("100"))
        after = storage.get_recipe(recipe_id)
        assert before is not None
        assert after is not None
        assert after.version == before.version + 1
        assert after.ingredients[0].id == ingredient_id
    finally:
        storage.close()


def test_remote_hydration_update_does_not_bump_version(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=1)
        before = storage.get_recipe(recipe_id)
        assert before is not None
        storage.update_recipe_from_remote(recipe_id, "Омлет", "remote", Decimal("3"), 1, 2)
        after = storage.get_recipe(recipe_id)
        assert after is not None
        assert after.version == before.version
        assert after.portions == Decimal("3")
    finally:
        storage.close()
