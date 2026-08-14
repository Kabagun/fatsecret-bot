from __future__ import annotations

import datetime as dt
import os
import sqlite3
from decimal import Decimal

import pytest

from fatsecret_bot.models import FatSecretSession, Ingredient, Recipe, RecipeSummary
from fatsecret_bot.recipe_compare import recipe_fingerprint
from fatsecret_bot.storage import Storage, normalize_title


def test_normalize_title_collapses_case_and_spaces() -> None:
    assert normalize_title("  Курица   В Соусе ") == normalize_title("курица в соусе")


def test_custom_food_run_journals_each_account_before_remote_ids(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        run_id = storage.create_custom_food_run(
            "group-1",
            11,
            "QA product",
            '{"title":"QA product"}',
            "request-hash",
            "content-hash",
            ["a1", "a2"],
        )

        run = storage.custom_food_run(run_id)
        assert run is not None
        assert run["status"] == "pending"
        assert [row["account_key"] for row in run["accounts"]] == ["a1", "a2"]
        assert all(row["remote_food_id"] is None for row in run["accounts"])

        assert storage.update_custom_food_run_account(
            run_id,
            "a1",
            "verified",
            remote_food_id="101",
        )
        assert storage.update_custom_food_run(run_id, "recovery_pending", error="retry")
        matched = storage.matching_custom_food_run("group-1", "request-hash")

        assert matched is not None
        assert matched["id"] == run_id
        assert matched["status"] == "recovery_pending"
        assert matched["accounts"][0]["remote_food_id"] == "101"
    finally:
        storage.close()


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


def test_import_remote_recipe_is_group_scoped(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        storage.register_user(22, "Two")
        first_group = storage.create_group(11, "Дом")
        second_group = storage.create_group(22, "Работа")

        first = storage.import_remote_recipe("a1", RecipeSummary(remote_id="101", title="Омлет"), first_group.id)
        second = storage.import_remote_recipe("a2", RecipeSummary(remote_id="202", title="омлет"), second_group.id)

        assert first != second
        assert [recipe.id for recipe in storage.list_recipes(first_group.id)] == [first]
        assert [recipe.id for recipe in storage.list_recipes(second_group.id)] == [second]
    finally:
        storage.close()


def test_rename_recipe_for_remote_identities_updates_matching_local_row(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Старое имя", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(recipe_id, "a1", "101", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "a2", "202", last_synced_version=1)

        renamed_id = storage.rename_recipe_for_remote_identities(
            "group",
            {("a1", "101"), ("a2", "202")},
            "Новое имя",
            updated_by=22,
        )

        renamed = storage.get_recipe(recipe_id)
        assert renamed_id == recipe_id
        assert renamed is not None
        assert renamed.title == "Новое имя"
        assert renamed.version == 2
        assert renamed.remote_ids == {"a1": "101", "a2": "202"}
    finally:
        storage.close()


def test_count_recipes_and_list_recipe_page_are_group_scoped(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Дом")
        other_group = storage.create_group(11, "Другое")
        for index in range(5):
            storage.create_recipe(f"Рецепт {index}", "", Decimal("1"), 0, 0, updated_by=11, group_id=group.id)
        storage.create_recipe("Чужой", "", Decimal("1"), 0, 0, updated_by=11, group_id=other_group.id)

        page = storage.list_recipe_page(group.id, page=1, page_size=2)

        assert storage.count_recipes(group.id) == 5
        assert [recipe.title for recipe in page] == ["Рецепт 2", "Рецепт 3"]
    finally:
        storage.close()


def test_group_join_switch_and_group_scoped_accounts(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        storage.register_user(22, "Two")
        group = storage.create_group(11, "Семья")
        joined = storage.join_group_by_code(22, group.invite_code)
        assert joined == group
        assert storage.active_group_for_user(22) == group

        storage.upsert_fatsecret_account(11, "One", "one@example.com", "secret", "BY", "ru")
        storage.upsert_fatsecret_account(22, "Two", "two@example.com", "secret", "BY", "ru")

        assert storage.fatsecret_account_count(group.id) == 2
        assert {account.key for account in storage.list_fatsecret_accounts(group.id)} == {"tg11", "tg22"}

        other_group = storage.create_group(11, "Solo")
        assert storage.set_active_group_for_user(22, other_group.id) is False
        assert {account.key for account in storage.list_fatsecret_accounts(other_group.id)} == {"tg11"}
        assert {account.key for account in storage.list_fatsecret_accounts(group.id)} == {"tg22"}

        assert storage.set_active_group_for_user(11, group.id) is True
        assert storage.list_fatsecret_accounts(other_group.id) == []
        assert {account.key for account in storage.list_fatsecret_accounts(group.id)} == {"tg11", "tg22"}

        assert storage.set_active_group_for_user(11, other_group.id) is True
        assert {account.key for account in storage.list_fatsecret_accounts(other_group.id)} == {"tg11"}
    finally:
        storage.close()


def test_list_group_ids_returns_all_groups_in_creation_order(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        first = storage.create_group(11, "Первая")
        second = storage.create_group(11, "Вторая")

        assert storage.list_group_ids() == [first.id, second.id]
    finally:
        storage.close()


def test_group_members_and_leave_active_group(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        storage.register_user(22, "Two")
        group = storage.create_group(11, "Семья")
        storage.join_group_by_code(22, group.invite_code)
        storage.upsert_fatsecret_account(11, "One FS", "one@example.com", "secret", "BY", "ru")

        members = storage.group_members(group.id)
        assert [(member.telegram_id, member.display_name, member.fatsecret_label) for member in members] == [
            (11, "One", "One FS"),
            (22, "Two", None),
        ]

        assert storage.leave_active_group(22) == group
        assert storage.active_group_for_user(22) is None
        assert [member.telegram_id for member in storage.group_members(group.id)] == [11]
    finally:
        storage.close()


def test_group_switch_moves_only_owned_accounts_and_preserves_session(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        storage.register_user(22, "Two")
        shared = storage.create_group(11, "Общая")
        storage.join_group_by_code(22, shared.invite_code)
        first_key = storage.create_fatsecret_account(
            11, "One FS", "one@example.com", "secret-one", "BY", "ru", group_id=shared.id
        )
        second_key = storage.create_fatsecret_account(
            22, "Two FS", "two@example.com", "secret-two", "BY", "ru", group_id=shared.id
        )
        session = FatSecretSession("server", "device", "secret")
        assert storage.update_fatsecret_session(first_key, session) is True
        recipe_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, 11, shared.id)
        storage.set_remote_recipe_id(recipe_id, first_key, "remote-one", last_synced_version=1)
        storage.mark_synced(recipe_id, first_key, "remote-one", version=1)

        destination = storage.create_group(11, "Личная")

        assert storage.fatsecret_account_group_id(first_key) == destination.id
        assert storage.fatsecret_account_group_id(second_key) == shared.id
        assert storage.get_fatsecret_session(first_key) == session
        assert storage.get_fatsecret_account(first_key).password == "secret-one"
        assert storage.get_recipe(recipe_id) is None
    finally:
        storage.close()


def test_leaving_only_group_detaches_owned_accounts_without_deleting_credentials(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Одна")
        account_key = storage.create_fatsecret_account(
            11, "One FS", "one@example.com", "secret", "BY", "ru", group_id=group.id
        )
        session = FatSecretSession("server", "device", "secret")
        storage.update_fatsecret_session(account_key, session)

        assert storage.leave_active_group(11) == group

        assert storage.active_group_for_user(11) is None
        assert storage.fatsecret_account_group_id(account_key) is None
        assert storage.get_fatsecret_account(account_key) is not None
        assert storage.get_fatsecret_session(account_key) == session
    finally:
        storage.close()


def test_group_creator_can_rename_active_group(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        storage.register_user(22, "Two")
        group = storage.create_group(11, "Старое")
        storage.join_group_by_code(22, group.invite_code)

        renamed = storage.rename_active_group(11, "Новое")
        assert renamed is not None
        assert renamed.name == "Новое"
        assert storage.rename_active_group(22, "Чужое") is None
        assert storage.active_group_created_by(11) is True
        assert storage.active_group_created_by(22) is False
    finally:
        storage.close()


def test_delete_selected_fatsecret_account_removes_remote_mapping(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        account_key = storage.upsert_fatsecret_account(11, "One FS", "one@example.com", "secret", "BY", "ru")
        recipe_id = storage.import_remote_recipe(account_key, RecipeSummary(remote_id="101", title="Омлет"), group.id)

        assert storage.delete_fatsecret_account(account_key) is True
        assert storage.get_fatsecret_account(account_key) is None
        recipe = storage.get_recipe(recipe_id)
        assert recipe is not None
        assert recipe.remote_ids == {}
        assert storage.delete_fatsecret_account(account_key) is False
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


def test_recipe_steps_are_stored_and_updated_from_remote(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe(
            "Омлет",
            "",
            Decimal("2"),
            5,
            10,
            updated_by=1,
            steps=["Смешать", "Запечь"],
        )
        recipe = storage.get_recipe(recipe_id)

        assert recipe is not None
        assert recipe.steps == ["Смешать", "Запечь"]

        storage.update_recipe_from_remote(
            recipe_id,
            "Омлет",
            "remote",
            Decimal("3"),
            1,
            2,
            steps=["Нарезать", "Подать"],
        )
        updated = storage.get_recipe(recipe_id)

        assert updated is not None
        assert updated.steps == ["Нарезать", "Подать"]
    finally:
        storage.close()


def test_recipe_steps_keep_first_100_items(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        steps = [f"Шаг {index}" for index in range(1, 102)]
        recipe_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=1, steps=steps)
        recipe = storage.get_recipe(recipe_id)

        assert recipe is not None
        assert recipe.steps == steps[:100]
    finally:
        storage.close()


def test_migration_normalizes_legacy_zero_portion_gram_ingredients(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite3"
    storage = Storage(db_path)
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=1)
        storage.add_ingredient(recipe_id, "food-turmeric", "Куркума", "0", Decimal("5"), "г")
    finally:
        storage.close()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE ingredients
            SET amount = '5', portion_description = 'г', grams = NULL
            WHERE recipe_id = ?
            """,
            (recipe_id,),
        )
        connection.execute("PRAGMA user_version = 0")

    storage = Storage(db_path)
    try:
        recipe = storage.get_recipe(recipe_id)

        assert recipe is not None
        assert recipe.ingredients[0].amount == Decimal("0.05")
        assert recipe.ingredients[0].portion_description == "100г"
        assert recipe.ingredients[0].grams == Decimal("5")
        assert storage._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        storage.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are enforced on production Linux")
def test_storage_restricts_existing_database_permissions(tmp_path) -> None:
    db_path = tmp_path / "bot.sqlite3"
    db_path.touch(mode=0o644)
    db_path.chmod(0o644)

    storage = Storage(db_path)
    try:
        assert db_path.stat().st_mode & 0o777 == 0o600
        assert storage._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        storage.close()


def test_diary_copy_claim_rejects_active_run_and_reclaims_stale_heartbeat(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        run_id = storage.create_diary_copy_run(
            "group",
            11,
            "tg11",
            dt.date(2026, 7, 14),
            dt.date(2026, 7, 15),
            dt.date(2026, 7, 15),
            {"entries": []},
        )
        started_at = dt.datetime(2026, 7, 16, 10, 0, tzinfo=dt.UTC)

        assert storage.claim_diary_copy_run(run_id, now=started_at) is True
        assert storage.claim_diary_copy_run(run_id, now=started_at + dt.timedelta(minutes=29)) is False
        assert storage.touch_diary_copy_run(run_id, now=started_at + dt.timedelta(minutes=20)) is True
        assert storage.claim_diary_copy_run(run_id, now=started_at + dt.timedelta(minutes=49)) is False
        assert storage.claim_diary_copy_run(run_id, now=started_at + dt.timedelta(minutes=51)) is True

        run = storage.diary_copy_run(run_id)
        assert run is not None
        assert run["status"] == "running"
        assert run["created_at"]
        assert run["updated_at"] == (started_at + dt.timedelta(minutes=51)).isoformat()

        storage.finish_diary_copy_run(run_id, "completed", {"dates": []})
        assert storage.claim_diary_copy_run(run_id, now=started_at + dt.timedelta(days=1)) is False
    finally:
        storage.close()


def test_ingredient_grams_are_persisted(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=1)
        storage.add_ingredient(recipe_id, "food-sauce", "Соус", "p1", Decimal("1.5"), "порции", grams=Decimal("150"))

        recipe = storage.get_recipe(recipe_id)

        assert recipe is not None
        assert recipe.ingredients[0].amount == Decimal("1.5")
        assert recipe.ingredients[0].portion_description == "порции"
        assert recipe.ingredients[0].grams == Decimal("150")
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


def test_fatsecret_session_is_cached_and_reset_on_account_upsert(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        account_key = storage.upsert_fatsecret_account(
            telegram_id=11,
            label="User One",
            username="one@example.com",
            password="password",
            market="BY",
            language="ru",
        )
        session = FatSecretSession(server_id="server", device_key="device", secret_key="secret")

        assert storage.get_fatsecret_session(account_key) is None
        assert storage.update_fatsecret_session(account_key, session) is True
        assert storage.get_fatsecret_session(account_key) == session

        storage.upsert_fatsecret_account(
            telegram_id=11,
            label="User One",
            username="one@example.com",
            password="new-password",
            market="BY",
            language="ru",
        )
        assert storage.get_fatsecret_session(account_key) is None
    finally:
        storage.close()


def test_update_fatsecret_account_label(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        account_key = storage.upsert_fatsecret_account(
            telegram_id=11,
            label="Long Original",
            username="one@example.com",
            password="password",
            market="BY",
            language="ru",
        )

        assert storage.update_fatsecret_account_label(account_key, "  One  ", owner_telegram_id=11) is True
        assert storage.update_fatsecret_account_label(account_key, " ", owner_telegram_id=11) is False
        assert storage.update_fatsecret_account_label("missing", "Two", owner_telegram_id=11) is False
        assert storage.update_fatsecret_account_label(account_key, "Other", owner_telegram_id=22) is False
        account = storage.get_fatsecret_account(account_key)
        assert account is not None
        assert account.label == "One"
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


def test_delete_unlinked_recipes_keeps_remote_mapped_recipes(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        stale_id = storage.create_recipe("Черновик", "", Decimal("1"), 0, 0, updated_by=11, group_id="g1")
        mapped_id = storage.create_recipe("Омлет", "", Decimal("1"), 0, 0, updated_by=11, group_id="g1")
        other_group_id = storage.create_recipe("Другое", "", Decimal("1"), 0, 0, updated_by=11, group_id="g2")
        storage.set_remote_recipe_id(mapped_id, "tg11", "111", last_synced_version=1)

        assert storage.delete_unlinked_recipes("g1") == 1
        assert storage.get_recipe(stale_id) is None
        assert storage.get_recipe(mapped_id) is not None
        assert storage.get_recipe(other_group_id) is not None
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


def test_remove_remote_recipe_mapping_deletes_recipe_only_after_last_mapping(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Омлет", "", Decimal("2"), 5, 10, updated_by=11, group_id="g1")
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)

        assert storage.remove_remote_recipe_mapping("tg11", "111") is True
        assert storage.remove_remote_recipe_mapping("tg11", "111") is False
        assert storage.remote_ids(recipe_id) == {"tg22": "222"}
        assert storage.get_recipe(recipe_id) is not None

        assert storage.remove_remote_recipe_mapping("tg22", "222") is True
        assert storage.get_recipe(recipe_id) is None
    finally:
        storage.close()


def test_reconcile_group_remote_recipes_prunes_stale_mappings_and_keeps_unrelated_drafts(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe_id = storage.create_recipe("Блины тонкие", "", Decimal("2"), 5, 10, updated_by=11, group_id="g1")
        draft_id = storage.create_recipe("Черновик", "", Decimal("1"), 0, 0, updated_by=11, group_id="g1")
        other_group_id = storage.create_recipe("Другой", "", Decimal("1"), 0, 0, updated_by=11, group_id="g2")
        storage.set_remote_recipe_id(recipe_id, "tg11", "111", last_synced_version=1)
        storage.set_remote_recipe_id(recipe_id, "tg22", "222", last_synced_version=1)
        storage.set_remote_recipe_id(other_group_id, "tg11", "other-111", last_synced_version=1)

        removed = storage.reconcile_group_remote_recipes(
            "g1",
            {"tg11": set(), "tg22": {"222"}},
        )

        assert removed == 1
        assert storage.remote_ids(recipe_id) == {"tg22": "222"}
        assert storage.get_recipe(recipe_id) is not None
        assert storage.get_recipe(draft_id) is not None
        assert storage.remote_ids(other_group_id) == {"tg11": "other-111"}

        assert storage.reconcile_group_remote_recipes("g1", {"tg11": set(), "tg22": set()}) == 1
        assert storage.get_recipe(recipe_id) is None
        assert storage.find_recipe_by_title("g1", "Блины тонкие") is None
        assert storage.get_recipe(draft_id) is not None
    finally:
        storage.close()


def test_one_telegram_user_can_own_multiple_accounts_but_each_account_has_one_group(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        first_group = storage.create_group(11, "Первая")
        first_key = storage.create_fatsecret_account(
            11, "Основной", "one@example.com", "secret", "BY", "ru", group_id=first_group.id
        )
        second_key = storage.create_fatsecret_account(
            11, "Тест", "test@example.com", "secret", "BY", "ru", group_id=first_group.id
        )
        second_group = storage.create_group(11, "Вторая")

        assert [item.key for item in storage.list_fatsecret_accounts_for_owner(11)] == [first_key, second_key]
        assert storage.list_fatsecret_accounts(first_group.id) == []
        assert {item.key for item in storage.list_fatsecret_accounts(second_group.id)} == {first_key, second_key}
        assert storage.set_active_group_for_user(11, first_group.id) is True
        assert {item.key for item in storage.list_fatsecret_accounts(first_group.id)} == {first_key, second_key}
        assert storage.list_fatsecret_accounts(second_group.id) == []
        assert storage.set_active_group_for_user(11, second_group.id) is True
        assert storage.list_fatsecret_accounts(first_group.id) == []
        assert {item.key for item in storage.list_fatsecret_accounts(second_group.id)} == {first_key, second_key}
    finally:
        storage.close()


def test_version_one_account_migration_preserves_credentials_session_and_group(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE telegram_users (
                telegram_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL,
                active_group_id TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE recipe_groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, invite_code TEXT NOT NULL UNIQUE,
                created_by INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE group_members (
                group_id TEXT NOT NULL, telegram_id INTEGER NOT NULL, joined_at TEXT NOT NULL,
                PRIMARY KEY (group_id, telegram_id)
            );
            CREATE TABLE fatsecret_accounts (
                account_key TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL UNIQUE,
                label TEXT NOT NULL, username TEXT NOT NULL, password TEXT NOT NULL,
                market TEXT NOT NULL, language TEXT NOT NULL,
                session_server_id TEXT, session_device_key TEXT, session_secret_key TEXT,
                session_updated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO telegram_users VALUES (11, 'One', 'g1', 'now');
            INSERT INTO recipe_groups VALUES ('g1', 'QA', 'ABCDEFGH', 11, 'now');
            INSERT INTO group_members VALUES ('g1', 11, 'now');
            INSERT INTO fatsecret_accounts VALUES (
                'tg11', 11, 'Kabaye', 'one@example.com', 'password', 'BY', 'ru',
                'server', 'device', 'secret', 'now', 'now', 'now'
            );
            PRAGMA user_version = 1;
            """
        )

    storage = Storage(db_path)
    try:
        account = storage.get_fatsecret_account("tg11")
        assert account is not None
        assert (account.username, account.password, account.label) == (
            "one@example.com",
            "password",
            "Kabaye",
        )
        assert storage.fatsecret_account_owner("tg11") == 11
        assert storage.fatsecret_account_group_id("tg11") == "g1"
        assert storage.get_fatsecret_session("tg11") == FatSecretSession("server", "device", "secret")
        assert storage._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        storage.close()


def test_remote_recipe_snapshots_round_trip_and_reconcile_by_account(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        recipe = Recipe(
            id="local",
            title="Омлет",
            portions=Decimal("2"),
            steps=["Смешать", "Запечь"],
            ingredients=[
                Ingredient(
                    id="i1",
                    recipe_id="local",
                    food_id="food-1",
                    title="Яйцо",
                    portion_id="p1",
                    amount=Decimal("2"),
                    grams=Decimal("100"),
                )
            ],
        )
        fingerprint = recipe_fingerprint(recipe)
        storage.upsert_remote_recipe_snapshot("a1", "111", recipe, fingerprint)
        storage.upsert_remote_recipe_summary("a1", "112", "Старый")

        stored = storage.remote_recipe_snapshot("a1", "111")
        assert stored is not None
        restored, digest = stored
        assert digest == fingerprint.digest
        assert recipe_fingerprint(restored).digest == fingerprint.digest
        assert storage.reconcile_remote_recipe_snapshots("a1", {"111"}) == 1
        assert storage.remote_recipe_snapshot("a1", "111") is not None
        assert storage.remote_recipe_snapshots_by_title("Омлет", account_keys={"a1"})[0][1] == "111"
    finally:
        storage.close()
