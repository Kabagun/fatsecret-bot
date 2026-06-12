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


def test_fatsecret_account_upsert_replaces_user_account(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        first_key = storage.upsert_fatsecret_account(
            telegram_id=11,
            label="User One",
            username="old@example.com",
            password="old-password",
            market="BY",
            language="ru",
        )
        second_key = storage.upsert_fatsecret_account(
            telegram_id=11,
            label="User One",
            username="new@example.com",
            password="new-password",
            market="PL",
            language="en",
        )

        account = storage.get_fatsecret_account_by_telegram_id(11)
        assert first_key == second_key == "tg11"
        assert storage.fatsecret_account_count() == 1
        assert account is not None
        assert account.username == "new@example.com"
        assert account.password == "new-password"
        assert account.market == "PL"
        assert account.language == "en"
    finally:
        storage.close()


def test_delete_fatsecret_account_removes_remote_recipe_mapping(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.upsert_fatsecret_account(
            telegram_id=11,
            label="User One",
            username="one@example.com",
            password="password",
            market="BY",
            language="ru",
        )
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11)
        storage.set_remote_recipe_id(recipe_id, "tg11", "123", last_synced_version=1)

        assert storage.delete_fatsecret_account_for_user(11) is True
        assert storage.delete_fatsecret_account_for_user(11) is False
        assert storage.get_fatsecret_account_by_telegram_id(11) is None
        assert storage.remote_ids(recipe_id) == {}
    finally:
        storage.close()


def test_delete_recipe_removes_local_recipe_data(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11)
        storage.add_ingredient(recipe_id, "4881229", "Куриное Филе", "4751539", Decimal("100"))
        storage.set_remote_recipe_id(recipe_id, "tg11", "123", last_synced_version=1)
        storage.record_sync(recipe_id, "tg11", "ok", "synced")

        assert storage.delete_recipe(recipe_id) is True
        assert storage.delete_recipe(recipe_id) is False
        assert storage.get_recipe(recipe_id) is None
        assert storage.list_ingredients(recipe_id) == []
        assert storage.remote_ids(recipe_id) == {}
    finally:
        storage.close()


def test_delete_remote_recipe_id_removes_one_mapping(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11)
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)

        assert storage.delete_remote_recipe_id(recipe_id, "tg11") is True
        assert storage.delete_remote_recipe_id(recipe_id, "tg11") is False
        assert storage.remote_ids(recipe_id) == {"tg22": "222"}
        assert storage.get_recipe(recipe_id) is not None
    finally:
        storage.close()
