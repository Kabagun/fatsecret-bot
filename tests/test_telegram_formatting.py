from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from fatsecret_bot.models import Ingredient, Recipe, RemoteRecipeVariant
from fatsecret_bot.recipe_compare import recipe_content_fingerprint, recipe_fingerprint
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import AccountSyncResult, RecipeCreateResult
from fatsecret_bot.telegram_bot import (
    RECIPE_WARNING_RENDER_TASK_KEY,
    TelegramRecipeBot,
    _compare_recipe_products,
    _format_recipe,
    _format_recipe_conflict,
    _format_recipe_product_differences,
    _parse_recipe_list_payload,
    _recipe_actions_keyboard,
    _recipe_export_payload,
    _recipe_list_button_text,
    _recipe_list_message,
    _recipe_versions_differ,
)


def test_authorization_requires_allowlist_or_existing_registration(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "Existing")
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.allowed_user_ids = {22}

        assert bot._is_authorized(11) is True
        assert bot._is_authorized(22) is True
        assert bot._is_authorized(33) is False

        bot.allowed_user_ids = set()
        assert bot._is_authorized(11) is True
        assert bot._is_authorized(22) is False
    finally:
        storage.close()


def test_denied_user_log_contains_identity_but_not_message_text(tmp_path, caplog) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.allowed_user_ids = set()
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=33,
                username="qa_user",
                full_name="QA User",
            ),
            effective_message=SimpleNamespace(
                text="private message text",
                reply_text=reply_text,
            ),
        )
        caplog.set_level(logging.WARNING, logger="fatsecret_bot.telegram_bot")

        authorized = asyncio.run(bot._require_user(update))

        assert authorized is False
        reply_text.assert_awaited_once_with("Этот бот закрыт для двух заданных пользователей.")
        assert "telegram_id=33" in caplog.text
        assert "username='qa_user'" in caplog.text
        assert "full_name='QA User'" in caplog.text
        assert "private message text" not in caplog.text
    finally:
        storage.close()


def test_format_recipe_hides_remote_ids_and_pretty_prints_amounts() -> None:
    recipe = Recipe(
        id="local",
        title="Завтрак",
        description="Описание",
        portions=Decimal("2.0"),
        prep_time=30,
        cook_time=10,
        remote_ids={"tg1": "123"},
        steps=["Смешать", "Запечь"],
        ingredients=[
            Ingredient(
                id="i1",
                recipe_id="local",
                food_id="f1",
                title="Яичный Белок",
                portion_id="p1",
                amount=Decimal("125.250"),
                portion_description="г",
            ),
            Ingredient(
                id="i2",
                recipe_id="local",
                food_id="f2",
                title="Соус",
                portion_id="p2",
                amount=Decimal("0.060"),
                portion_description="serving",
                grams=Decimal("6"),
            ),
            Ingredient(
                id="i3",
                recipe_id="local",
                food_id="f3",
                title="Кетчуп",
                portion_id="0",
                amount=Decimal("3"),
                portion_description="100г",
            ),
        ],
    )

    text = _format_recipe(recipe)

    assert "Remote:" not in text
    assert "Порций: 2;" in text
    assert "- Яичный Белок: 125.25г" in text
    assert "- Соус: 6г" in text
    assert "- Кетчуп: 300г" in text
    assert "<b>Шаги</b>" in text
    assert "1. Смешать" in text


def test_format_recipe_conflict_shows_only_differing_fields() -> None:
    first = Recipe(id="local", title="Омлет", portions=Decimal("2"), steps=["Смешать"])
    second = Recipe(id="local", title="Омлет", portions=Decimal("4"), steps=["Смешать"])
    variants = [
        RemoteRecipeVariant("tg11", "111", first, recipe_fingerprint(first)),
        RemoteRecipeVariant("tg22", "222", second, recipe_fingerprint(second)),
    ]

    text = _format_recipe_conflict(variants, {"tg11": "thekabaye", "tg22": "Святичек"})

    assert "Версии рецепта различаются" in text
    assert "<b>Порций</b>" in text
    assert "• thekabaye — 2" in text
    assert "• Святичек — 4" in text
    assert "Ингредиенты и остальные поля совпадают." in text
    assert "ID <code>" not in text
    assert "<b>Ингредиенты</b>" not in text
    assert "<b>Шаги</b>" not in text


def _recipe_variant(account_key: str, remote_id: str, ingredients: list[Ingredient]) -> RemoteRecipeVariant:
    recipe = Recipe(id="local", title="Омлет", ingredients=ingredients)
    return RemoteRecipeVariant(account_key, remote_id, recipe, recipe_content_fingerprint(recipe))


def _ingredient(
    identifier: str,
    title: str,
    grams: str,
    *,
    food_id: str | None = None,
    portion_id: str = "portion",
) -> Ingredient:
    return Ingredient(
        id=identifier,
        recipe_id="local",
        food_id=food_id or f"food-{identifier}",
        title=title,
        portion_id=portion_id,
        amount=Decimal("1"),
        portion_description="г",
        grams=Decimal(grams),
    )


def test_compare_recipe_products_ignores_ids_order_and_preserves_duplicate_counts() -> None:
    first = _recipe_variant(
        "tg11",
        "111",
        [
            _ingredient("salt-a1", "Соль", "5", food_id="food-a", portion_id="a"),
            _ingredient("egg-a", "Яйцо", "120"),
            _ingredient("salt-a2", "Соль", "5", food_id="food-b", portion_id="b"),
        ],
    )
    second = _recipe_variant(
        "tg22",
        "222",
        [
            _ingredient("salt-b1", "  соль  ", "5", food_id="food-x", portion_id="x"),
            _ingredient("salt-b2", "СОЛЬ", "5", food_id="food-y", portion_id="y"),
            _ingredient("egg-b", "Яйцо", "120", food_id="food-z", portion_id="z"),
        ],
    )

    comparison = _compare_recipe_products([first, second])

    assert comparison.has_differences is False
    assert [item.title for item in comparison.same_products] == ["Соль", "Яйцо", "Соль"]


def test_format_recipe_product_differences_groups_accounts_and_same_products() -> None:
    first = _recipe_variant(
        "tg11",
        "111",
        [
            _ingredient("egg-a", "Яйцо куриное", "120"),
            _ingredient("milk-a", "Молоко Савушкин 1,5%", "200"),
            _ingredient("salt-a", "Соль", "5"),
        ],
    )
    second = _recipe_variant(
        "tg22",
        "222",
        [
            _ingredient("egg-b", "Яйцо столовое С-1", "120"),
            _ingredient("milk-b", "Молоко Простоквашино 2,5%", "200"),
            _ingredient("salt-b", "Соль", "5"),
        ],
    )

    comparison = _compare_recipe_products([first, second])
    text = _format_recipe_product_differences(comparison, {"tg11": "thekabaye", "tg22": "Святичек"})

    assert comparison.has_differences is True
    assert "<b>Продукты отличаются у thekabaye:</b>" in text
    assert "1. Яйцо куриное — 120 г" in text
    assert "2. Молоко Савушкин 1,5% — 200 г" in text
    assert "<b>Продукты отличаются у Святичек:</b>" in text
    assert "1. Яйцо столовое С-1 — 120 г" in text
    assert "<b>Совпадающие продукты:</b>" in text
    assert "1. Соль — 5 г" in text
    assert "ID <code>" not in text


def test_format_recipe_product_differences_shows_absent_account_counterpart() -> None:
    first = _recipe_variant("tg11", "111", [_ingredient("cheese", "Сыр", "100")])
    second = _recipe_variant("tg22", "222", [])

    text = _format_recipe_product_differences(
        _compare_recipe_products([first, second]),
        {"tg11": "Первый", "tg22": "Второй"},
    )

    assert "<b>Продукты отличаются у Первый:</b>\n\n1. Сыр — 100 г" in text
    assert "<b>Продукты отличаются у Второй:</b>\n\n1. Отсутствует" in text
    assert "<b>Совпадающие продукты:</b>\n\nОтсутствуют." in text

    partial_text = _format_recipe_product_differences(
        _compare_recipe_products(
            [
                _recipe_variant(
                    "tg11",
                    "111",
                    [_ingredient("cheese-a", "Сыр", "100"), _ingredient("ham-a", "Ветчина", "100")],
                ),
                _recipe_variant("tg22", "222", [_ingredient("ham-b", "Ветчина", "200")]),
            ]
        ),
        {"tg11": "Первый", "tg22": "Второй"},
    )

    assert "<b>Продукты отличаются у Второй:</b>\n\n1. Ветчина — 200 г\n2. Отсутствует" in partial_text


def test_compare_recipe_products_requires_match_across_three_accounts() -> None:
    variants = [
        _recipe_variant(
            "tg11",
            "111",
            [_ingredient("salt-a", "Соль", "5"), _ingredient("egg-a", "Яйцо", "100")],
        ),
        _recipe_variant(
            "tg22",
            "222",
            [_ingredient("egg-b", "Яйцо", "100"), _ingredient("salt-b", "Соль", "5")],
        ),
        _recipe_variant(
            "tg33",
            "333",
            [_ingredient("salt-c", "Соль", "5"), _ingredient("egg-c", "Яйцо", "200")],
        ),
    ]

    comparison = _compare_recipe_products(variants)

    assert [item.title for item in comparison.same_products] == ["Соль"]
    assert [[item.grams for item in products] for _, products in comparison.different_products] == [
        [Decimal("100")],
        [Decimal("100")],
        [Decimal("200")],
    ]


def test_format_recipe_product_differences_truncates_on_complete_html_lines() -> None:
    first = _recipe_variant(
        "tg11",
        "111",
        [_ingredient(f"a-{index}", f"Продукт <A> {index} " + "я" * 230, str(index + 1)) for index in range(30)],
    )
    second = _recipe_variant("tg22", "222", [])

    text = _format_recipe_product_differences(_compare_recipe_products([first, second]), {})

    assert len(text) <= 4000
    assert text.endswith("…")
    assert "&lt;A&gt;" in text
    assert text.count("<b>") == text.count("</b>")


def test_recipe_actions_keyboard_keeps_only_recipe_actions_and_list_return() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=1, page_action="list", total_pages=3)
    rows = keyboard.inline_keyboard

    assert [button.text for button in rows[0]] == ["Экспортировать"]
    assert rows[0][0].callback_data == "recipe_export:recipe-1:-1"
    assert [button.text for button in rows[1]] == ["Переименовать"]
    assert rows[1][0].callback_data == "recipe_rename:recipe-1"
    assert [button.text for button in rows[2]] == ["Удалить в FatSecret"]
    assert [button.text for button in rows[3]] == ["К списку"]
    assert rows[3][0].callback_data == "list:1"
    flat_texts = [button.text for row in rows for button in row]
    assert "Назад" not in flat_texts
    assert "Дальше" not in flat_texts
    assert "Поиск" not in flat_texts
    assert "Создать из списка" not in flat_texts
    assert "В меню" not in flat_texts


def test_recipe_actions_keyboard_keeps_actions_without_navigation() -> None:
    keyboard = _recipe_actions_keyboard(
        "recipe-1",
        page=0,
        page_action="list",
        total_pages=1,
        can_sync=True,
    )
    flat_texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Назад" not in flat_texts
    assert "Дальше" not in flat_texts
    assert "Экспортировать" in flat_texts
    assert "Синхронизировать" in flat_texts
    assert "Удалить в FatSecret" in flat_texts


def test_recipe_export_round_trips_through_real_import_parser_with_special_characters() -> None:
    recipe = Recipe(
        id="recipe-1",
        title="Соус <летний> & сыр",
        portions=Decimal("2.5"),
        description="Не экспортируется",
        prep_time=10,
        cook_time=20,
        ingredients=[
            _ingredient("tomato", 'Томаты <черри> & "соль"', "125.5"),
            _ingredient("cheese", "Сыр 50%", "40"),
        ],
        steps=["Смешать <аккуратно>", "Подать & съесть"],
    )

    payload = _recipe_export_payload(recipe)
    portions, items, bad_lines, steps = _parse_recipe_list_payload(payload)

    assert payload.startswith("Порций: 2.5\n")
    assert "Не экспортируется" not in payload
    assert portions == Decimal("2.5")
    assert bad_lines == []
    assert [(item.query, item.grams) for item in items] == [
        ('Томаты <черри> & "соль"', Decimal("125.5")),
        ("Сыр 50%", Decimal("40")),
    ]
    assert steps == ["Смешать <аккуратно>", "Подать & съесть"]


def test_recipe_export_refuses_an_ingredient_without_a_resolvable_gram_weight() -> None:
    recipe = Recipe(
        id="recipe-1",
        title="Небезопасный",
        ingredients=[
            Ingredient(
                id="unknown",
                recipe_id="recipe-1",
                food_id="food",
                title="Щепотка соли",
                portion_id="portion",
                amount=Decimal("1"),
                portion_description="serving",
            )
        ],
    )

    with pytest.raises(ValueError, match="Щепотка соли"):
        _recipe_export_payload(recipe)


def test_recipe_export_uses_in_memory_text_document_when_telegram_text_is_too_large(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Solo")
        recipe = Recipe(
            id="recipe-1",
            title="Большой рецепт",
            group_id=group.id,
            ingredients=[
                _ingredient(str(index), f"Очень длинный ингредиент {index} " + "я" * 180, "10")
                for index in range(30)
            ],
        )
        for ingredient in recipe.ingredients:
            ingredient.recipe_id = recipe.id
        variant = RemoteRecipeVariant("tg11", "remote-1", recipe, recipe_fingerprint(recipe))
        message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=11),
            message=message,
            edit_message_text=AsyncMock(),
        )
        context = SimpleNamespace(
            user_data={"recipe_variants": [variant]},
            chat_data={},
        )
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage

        asyncio.run(bot._export_recipe(query, context, f"{recipe.id}:0"))

        message.reply_text.assert_not_awaited()
        message.reply_document.assert_awaited_once()
        document = message.reply_document.await_args.kwargs["document"]
        assert document.filename == "recipe-import.txt"
    finally:
        storage.close()


def test_recipe_version_difference_requires_one_identical_version_per_connected_account() -> None:
    same_a = _recipe_variant("tg11", "111", [_ingredient("a", "Яйцо", "100")])
    same_b = _recipe_variant("tg22", "222", [_ingredient("b", "Яйцо", "100")])
    duplicate_b = _recipe_variant("tg22", "223", [_ingredient("c", "Яйцо", "100")])
    metadata_recipe = Recipe(
        id="local",
        title="Омлет",
        portions=Decimal("2"),
        ingredients=[_ingredient("d", "Яйцо", "100")],
    )
    metadata_b = RemoteRecipeVariant(
        "tg22",
        "222",
        metadata_recipe,
        recipe_content_fingerprint(metadata_recipe),
    )
    ingredient_b = _recipe_variant("tg22", "222", [_ingredient("e", "Яйцо", "200")])

    assert _recipe_versions_differ([same_a, same_b], {"tg11", "tg22"}) is False
    assert _recipe_versions_differ([same_a], {"tg11", "tg22"}) is True
    assert _recipe_versions_differ([same_a, same_b, duplicate_b], {"tg11", "tg22"}) is True
    assert _recipe_versions_differ([same_a, metadata_b], {"tg11", "tg22"}) is True
    assert _recipe_versions_differ([same_a, ingredient_b], {"tg11", "tg22"}) is True


def _two_account_recipe_flow(tmp_path):  # noqa: ANN001, ANN202
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.register_user(11, "One")
    storage.register_user(22, "Two")
    group = storage.create_group(11, "Семья")
    storage.join_group_by_code(22, group.invite_code)
    storage.create_fatsecret_account(
        11,
        "Первый",
        "one@example.com",
        "secret",
        "BY",
        "ru",
        group_id=group.id,
    )
    storage.create_fatsecret_account(
        22,
        "Второй",
        "two@example.com",
        "secret",
        "BY",
        "ru",
        group_id=group.id,
    )
    recipe_ref = Recipe(
        id="recipe-live",
        title="Омлет",
        group_id=group.id,
        remote_ids={"tg11": "111", "tg22": "222"},
        remote_ids_by_account={"tg11": ["111"], "tg22": ["222"]},
    )
    context = SimpleNamespace(
        user_data={"group_id": group.id},
        chat_data={"recipe_cache_group_id": group.id, "recipe_cache": [recipe_ref]},
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=11),
        message=SimpleNamespace(),
        edit_message_text=AsyncMock(),
    )
    return storage, group, recipe_ref, context, query


def _flow_variant(
    recipe_ref: Recipe,
    account_key: str,
    remote_id: str,
    *,
    grams: str = "100",
    portions: str = "1",
) -> RemoteRecipeVariant:
    recipe = Recipe(
        id=recipe_ref.id,
        title=recipe_ref.title,
        group_id=recipe_ref.group_id,
        portions=Decimal(portions),
        ingredients=[_ingredient(f"ingredient-{account_key}-{remote_id}", "Яйцо", grams)],
        remote_ids={account_key: remote_id},
        remote_ids_by_account={account_key: [remote_id]},
    )
    return RemoteRecipeVariant(
        account_key,
        remote_id,
        recipe,
        recipe_content_fingerprint(recipe),
    )


def test_open_identical_recipe_versions_renders_one_shared_card_without_sync(tmp_path) -> None:
    storage, _, recipe_ref, context, query = _two_account_recipe_flow(tmp_path)
    try:
        variants = [
            _flow_variant(recipe_ref, "tg11", "111"),
            _flow_variant(recipe_ref, "tg22", "222"),
        ]
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = SimpleNamespace(
            hydrate_live_recipe_variants=AsyncMock(return_value=variants),
        )

        asyncio.run(bot._open_recipe(query, context, f"{recipe_ref.id}:0:list"))

        rendered = query.edit_message_text.await_args
        assert rendered.args[0].startswith("<b>Омлет</b>")
        buttons = [
            button.text
            for row in rendered.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "Экспортировать" in buttons
        assert "Синхронизировать" not in buttons
        assert "Первый" not in rendered.args[0]
        assert context.user_data["recipe_versions_differ"] is False
    finally:
        storage.close()


def test_open_missing_or_duplicate_recipe_versions_uses_simple_account_buttons(tmp_path) -> None:
    storage, _, recipe_ref, context, query = _two_account_recipe_flow(tmp_path)
    try:
        variants = [
            _flow_variant(recipe_ref, "tg11", "111"),
            _flow_variant(recipe_ref, "tg11", "112"),
        ]
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = SimpleNamespace(
            hydrate_live_recipe_variants=AsyncMock(return_value=variants),
        )

        asyncio.run(bot._open_recipe(query, context, f"{recipe_ref.id}:0:list"))

        rendered = query.edit_message_text.await_args
        assert "Версии рецепта различаются" in rendered.args[0]
        assert "Нет версии: Второй" in rendered.args[0]
        buttons = [
            button.text
            for row in rendered.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "Первый (ID 111)" in buttons
        assert "Первый (ID 112)" in buttons
        assert all(not button.startswith("Источник:") for button in buttons)
        assert context.user_data["recipe_versions_differ"] is True
    finally:
        storage.close()


def test_sync_source_preview_requires_confirmation_and_passes_approval_fingerprint(tmp_path) -> None:
    storage, group, recipe_ref, context, query = _two_account_recipe_flow(tmp_path)
    try:
        source = _flow_variant(recipe_ref, "tg11", "111", grams="100")
        target = _flow_variant(recipe_ref, "tg22", "222", grams="200")
        context.user_data.update(
            {
                "recipe_variants": [source, target],
                "recipe_versions_differ": True,
                "current_recipe_id": recipe_ref.id,
            }
        )
        synced = Recipe(
            id=recipe_ref.id,
            title=recipe_ref.title,
            group_id=group.id,
            remote_ids=dict(recipe_ref.remote_ids),
            remote_ids_by_account={key: list(values) for key, values in recipe_ref.remote_ids_by_account.items()},
        )
        sync_live = AsyncMock(
            return_value=(
                synced,
                [
                    AccountSyncResult("tg11", "111", True, "source"),
                    AccountSyncResult("tg22", "333", True, "updated"),
                ],
            )
        )
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = SimpleNamespace(
            hydrate_live_recipe_variants=AsyncMock(return_value=[source, target]),
            sync_live_recipe_from_source=sync_live,
            load_remote_recipe_index=AsyncMock(return_value=[synced]),
        )

        asyncio.run(bot._show_sync_preview(query, context, 0))

        sync_live.assert_not_awaited()
        preview_render = query.edit_message_text.await_args
        assert "Оригинал из аккаунта: Первый" in preview_render.args[0]
        assert "Подтвердить синхронизацию" in [
            button.text
            for row in preview_render.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]

        asyncio.run(bot._confirm_sync_preview(query, context))

        sync_live.assert_awaited_once_with(
            recipe_ref,
            "tg11",
            expected_source_remote_id="111",
            expected_source_content_digest=source.fingerprint.digest,
        )
        assert "Синхронизация завершена" in query.edit_message_text.await_args.args[0]
    finally:
        storage.close()


def test_sync_confirmation_rejects_a_changed_source_without_mutation(tmp_path) -> None:
    storage, _, recipe_ref, context, query = _two_account_recipe_flow(tmp_path)
    try:
        source = _flow_variant(recipe_ref, "tg11", "111", grams="100")
        target = _flow_variant(recipe_ref, "tg22", "222", grams="200")
        context.user_data.update(
            {
                "recipe_variants": [source, target],
                "recipe_versions_differ": True,
            }
        )
        sync_live = AsyncMock()
        changed_source = _flow_variant(recipe_ref, "tg11", "111", grams="150")
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = SimpleNamespace(
            hydrate_live_recipe_variants=AsyncMock(return_value=[changed_source, target]),
            sync_live_recipe_from_source=sync_live,
        )

        asyncio.run(bot._show_sync_preview(query, context, 0))
        asyncio.run(bot._confirm_sync_preview(query, context))

        sync_live.assert_not_awaited()
        assert "Выбранная версия изменилась" in query.edit_message_text.await_args.args[0]
        assert "recipe_sync_preview" not in context.user_data
    finally:
        storage.close()


def test_sync_confirmation_does_nothing_when_versions_are_already_identical(tmp_path) -> None:
    storage, _, recipe_ref, context, query = _two_account_recipe_flow(tmp_path)
    try:
        source = _flow_variant(recipe_ref, "tg11", "111", grams="100")
        target = _flow_variant(recipe_ref, "tg22", "222", grams="200")
        identical_target = _flow_variant(recipe_ref, "tg22", "222", grams="100")
        context.user_data.update(
            {
                "recipe_variants": [source, target],
                "recipe_versions_differ": True,
            }
        )
        sync_live = AsyncMock()
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = SimpleNamespace(
            hydrate_live_recipe_variants=AsyncMock(return_value=[source, identical_target]),
            sync_live_recipe_from_source=sync_live,
        )

        asyncio.run(bot._show_sync_preview(query, context, 0))
        asyncio.run(bot._confirm_sync_preview(query, context))

        sync_live.assert_not_awaited()
        rendered = query.edit_message_text.await_args
        assert "синхронизация не нужна" in rendered.args[0].casefold()
        buttons = [
            button.text
            for row in rendered.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "Синхронизировать" not in buttons
        assert context.user_data["recipe_versions_differ"] is False
    finally:
        storage.close()


def test_recipe_list_keyboard_keeps_recipe_buttons_navigation_and_actions_inline() -> None:
    recipes = [
        Recipe(id=f"recipe-{index}", title=f"Рецепт {index}", remote_ids={"tg1": "remote"})
        for index in range(9)
    ]
    bot = object.__new__(TelegramRecipeBot)

    keyboard = TelegramRecipeBot._recipe_list_keyboard(bot, recipes, 0, "list", {"tg1": "Каба"})
    rows = keyboard.inline_keyboard
    flat_texts = [button.text for row in rows for button in row]

    assert "Дальше" in flat_texts
    assert "1/2" not in flat_texts
    assert "Поиск" not in flat_texts
    assert "Создать из списка" not in flat_texts
    assert "Удалить несколько" in flat_texts
    assert "В меню" not in flat_texts


def test_recipe_list_marker_has_no_count_and_footer_is_conditional() -> None:
    recipe = Recipe(
        id="recipe-1",
        title="Очень длинное название рецепта " * 5,
        remote_ids={"tg1": "111", "tg2": "222"},
    )

    marked = _recipe_list_button_text(
        recipe,
        {"tg1": "thekabaye", "tg2": "Святичек"},
        has_product_differences=True,
    )
    plain_message = _recipe_list_message("Общий список рецептов:")
    marked_message = _recipe_list_message("Общий список рецептов:", has_product_differences=True)

    assert marked.endswith(" ⚠️")
    assert len(marked) <= 90
    assert "различ" not in marked
    assert "⚠️ — в рецепте есть различия между аккаунтами." not in plain_message
    assert marked_message.endswith("⚠️ — в рецепте есть различия между аккаунтами.")


def test_recipe_list_keyboard_marks_only_selected_recipes() -> None:
    recipes = [
        Recipe(id="different", title="Омлет", remote_ids={"tg1": "111", "tg2": "222"}),
        Recipe(id="same", title="Суп", remote_ids={"tg1": "333", "tg2": "444"}),
    ]
    bot = object.__new__(TelegramRecipeBot)

    keyboard = TelegramRecipeBot._recipe_list_keyboard(
        bot,
        recipes,
        0,
        "list",
        {"tg1": "Каба", "tg2": "Света"},
        product_difference_ids={"different"},
    )

    assert keyboard.inline_keyboard[0][0].text == "Омлет · Каба, Света ⚠️"
    assert keyboard.inline_keyboard[1][0].text == "Суп · Каба, Света"


def _warning_test_recipes() -> list[Recipe]:
    return [
        Recipe(
            id=f"recipe-{index}",
            title=f"Рецепт {index}",
            group_id="group",
            remote_ids={"tg11": f"a-{index}", "tg22": f"b-{index}"},
            remote_ids_by_account={
                "tg11": [f"a-{index}"],
                "tg22": [f"b-{index}"],
            },
        )
        for index in range(9)
    ]


class _FakeApplication:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []

    def create_task(self, coroutine, **kwargs):  # noqa: ANN001, ANN003
        task = asyncio.create_task(coroutine, name=kwargs.get("name"))
        self.tasks.append(task)
        return task


class _EditableRecipeListMessage:
    def __init__(self) -> None:
        self.edits: list[tuple[str, object]] = []

    async def edit_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
        self.edits.append((text, kwargs.get("reply_markup")))


class _RecipeSearchMessage:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object, _EditableRecipeListMessage]] = []

    async def reply_text(self, text: str, **kwargs) -> _EditableRecipeListMessage:  # noqa: ANN003
        target = _EditableRecipeListMessage()
        self.sent.append((text, kwargs.get("reply_markup"), target))
        return target


def _recipe_search_bot_and_context(recipes: list[Recipe], engine):  # noqa: ANN001, ANN202
    bot = object.__new__(TelegramRecipeBot)
    bot.sync_engine = engine
    bot.storage = SimpleNamespace(
        list_fatsecret_accounts=lambda group_id: [
            SimpleNamespace(key="tg11", label="Первый"),
            SimpleNamespace(key="tg22", label="Второй"),
        ]
    )
    application = _FakeApplication()
    context = SimpleNamespace(
        application=application,
        user_data={"group_id": "group"},
        chat_data={"recipe_cache_group_id": "group", "recipe_cache": recipes},
    )
    return bot, context, application


def test_recipe_warning_state_marks_structural_differences_without_hydration() -> None:
    recipe = Recipe(
        id="missing",
        title="Нет копии",
        group_id="group",
        remote_ids={"tg11": "111"},
        remote_ids_by_account={"tg11": ["111"]},
    )
    bot, context, _ = _recipe_search_bot_and_context([recipe], SimpleNamespace())

    marked, pending, accounts = bot._recipe_warning_state(context, "group", [recipe])

    assert marked == {"missing"}
    assert pending == []
    assert accounts == {"tg11", "tg22"}


def test_recipe_search_renders_immediately_then_updates_visible_page_once() -> None:
    recipes = _warning_test_recipes()

    class FakeEngine:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def hydrate_live_recipe_variants_batch(
            self,
            requested: list[Recipe],
        ) -> dict[str, list[RemoteRecipeVariant]]:
            self.calls.append([recipe.id for recipe in requested])
            self.started.set()
            await self.release.wait()
            result: dict[str, list[RemoteRecipeVariant]] = {}
            for recipe in requested:
                grams = "200" if recipe.id == "recipe-0" else "100"
                result[recipe.id] = [
                    _recipe_variant("tg11", recipe.remote_ids["tg11"], [_ingredient("a", "Яйцо", "100")]),
                    _recipe_variant("tg22", recipe.remote_ids["tg22"], [_ingredient("b", "Яйцо", grams)]),
                ]
            return result

    async def scenario() -> None:
        engine = FakeEngine()
        bot, context, application = _recipe_search_bot_and_context(recipes, engine)
        message = _RecipeSearchMessage()
        update = SimpleNamespace(effective_message=message)

        await bot._handle_recipe_search(update, context, "рецепт")

        assert len(message.sent) == 1
        initial_text, keyboard, target = message.sent[0]
        assert "Проверяю версии в фоне" in initial_text
        assert keyboard.inline_keyboard[0][0].text.startswith("Рецепт 0")
        assert all("Рецепт 8" not in button.text for row in keyboard.inline_keyboard for button in row)
        assert target.edits == []
        await engine.started.wait()
        assert engine.calls == [[recipe.id for recipe in recipes[:8]]]

        engine.release.set()
        await asyncio.gather(*application.tasks)

        assert len(target.edits) == 1
        final_text, final_keyboard = target.edits[0]
        assert "Проверяю версии в фоне" not in final_text
        assert "⚠️" in final_text
        assert final_keyboard.inline_keyboard[0][0].text.endswith("⚠️")
        marked, pending, _ = bot._recipe_warning_state(context, "group", recipes[:8])
        assert marked == {"recipe-0"}
        assert pending == []

    asyncio.run(scenario())


def test_recipe_warning_scan_failure_keeps_rendered_list_usable() -> None:
    recipes = _warning_test_recipes()[:2]

    class FailingEngine:
        async def hydrate_live_recipe_variants_batch(self, requested):  # noqa: ANN001, ANN202
            raise RuntimeError("unavailable")

    async def scenario() -> None:
        bot, context, application = _recipe_search_bot_and_context(recipes, FailingEngine())
        message = _RecipeSearchMessage()

        await bot._handle_recipe_search(SimpleNamespace(effective_message=message), context, "рецепт")
        await asyncio.gather(*application.tasks)

        initial_text, _, target = message.sent[0]
        assert "Проверяю версии в фоне" in initial_text
        assert len(target.edits) == 1
        assert "Найдено рецептов: 2" in target.edits[0][0]
        assert "Проверяю версии в фоне" not in target.edits[0][0]

    asyncio.run(scenario())


def test_recipe_warning_render_ignores_stale_results_after_navigation() -> None:
    recipes = _warning_test_recipes()[:1]

    class BlockingEngine:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def hydrate_live_recipe_variants_batch(self, requested):  # noqa: ANN001, ANN202
            self.started.set()
            await self.release.wait()
            recipe = requested[0]
            return {
                recipe.id: [
                    _recipe_variant("tg11", "a", [_ingredient("a", "Яйцо", "100")]),
                    _recipe_variant("tg22", "b", [_ingredient("b", "Яйцо", "200")]),
                ]
            }

    async def scenario() -> None:
        engine = BlockingEngine()
        bot, context, application = _recipe_search_bot_and_context(recipes, engine)
        message = _RecipeSearchMessage()
        await bot._handle_recipe_search(SimpleNamespace(effective_message=message), context, "рецепт")
        await engine.started.wait()

        bot._cancel_recipe_warning_render(context)
        engine.release.set()
        await asyncio.gather(*application.tasks, return_exceptions=True)

        assert message.sent[0][2].edits == []
        assert RECIPE_WARNING_RENDER_TASK_KEY not in context.chat_data

    asyncio.run(scenario())


def test_recipe_warning_scan_deduplicates_identical_in_flight_requests() -> None:
    recipes = _warning_test_recipes()[:2]

    class BlockingEngine:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.calls = 0

        async def hydrate_live_recipe_variants_batch(self, requested):  # noqa: ANN001, ANN202
            self.calls += 1
            await self.release.wait()
            return {recipe.id: [] for recipe in requested}

    async def scenario() -> None:
        engine = BlockingEngine()
        bot, context, _ = _recipe_search_bot_and_context(recipes, engine)
        connected = {"tg11", "tg22"}

        first = bot._shared_recipe_warning_scan(context, "group", recipes, connected)
        second = bot._shared_recipe_warning_scan(context, "group", recipes, connected)

        assert first is second
        await asyncio.sleep(0)
        assert engine.calls == 1
        engine.release.set()
        await first

    asyncio.run(scenario())


def test_ensure_main_keyboard_does_not_send_extra_message() -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def reply_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.sent.append(text)

    class FakeContext:
        def __init__(self) -> None:
            self.chat_data: dict[str, str] = {}

    bot = object.__new__(TelegramRecipeBot)
    message = FakeMessage()
    context = FakeContext()

    asyncio.run(TelegramRecipeBot._ensure_main_keyboard(bot, message, context))

    assert message.sent == []
    assert context.chat_data["reply_keyboard"] == "main"


def test_custom_food_skip_buttons_advance_optional_steps() -> None:
    query = SimpleNamespace(edit_message_text=AsyncMock())
    context = SimpleNamespace(
        user_data={
            "mode": "custom_food_barcode",
            "custom_food_barcode": "4006381333931",
            "custom_food_barcode_type": "EAN_13",
            "custom_food_manufacturer_name": "stale brand",
            "custom_food_brand_query": "stale query",
            "custom_food_brand_suggestions": ["stale suggestion"],
            "custom_food_brand_choice_token": "stale token",
        }
    )
    bot = object.__new__(TelegramRecipeBot)

    asyncio.run(bot._skip_custom_food_barcode(query, context))

    assert context.user_data["mode"] == "custom_food_brand"
    assert "custom_food_barcode" not in context.user_data
    assert "custom_food_barcode_type" not in context.user_data
    barcode_skip_kwargs = query.edit_message_text.await_args.kwargs
    brand_buttons = [
        button.callback_data
        for row in barcode_skip_kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "food_skip_brand:0" in brand_buttons

    asyncio.run(bot._skip_custom_food_brand(query, context))

    assert context.user_data["mode"] == "custom_food_macros"
    assert "custom_food_manufacturer_name" not in context.user_data
    assert "custom_food_brand_query" not in context.user_data
    assert "custom_food_brand_suggestions" not in context.user_data
    assert "custom_food_brand_choice_token" not in context.user_data


def test_custom_food_brand_is_normalized_and_added_to_definition() -> None:
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(
        user_data={
            "mode": "custom_food_brand",
            "custom_food_title": "QA Burger",
            "group_id": "group",
        }
    )
    bot = object.__new__(TelegramRecipeBot)
    bot.sync_engine = SimpleNamespace(
        suggest_custom_food_brands=AsyncMock(return_value=["Burger King"]),
    )

    asyncio.run(bot._handle_custom_food_brand(update, context, "  Burger   King  "))

    assert context.user_data["custom_food_brand_query"] == "Burger King"
    assert context.user_data["custom_food_brand_suggestions"] == ["Burger King"]
    assert context.user_data["mode"] == "custom_food_brand_choice"
    choice_token = context.user_data["custom_food_brand_choice_token"]
    suggestion_buttons = [
        button.callback_data
        for row in status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"food_brand_pick:{choice_token}:0" in suggestion_buttons
    assert f"food_brand_custom:{choice_token}" in suggestion_buttons

    query = SimpleNamespace(edit_message_text=AsyncMock())
    asyncio.run(bot._pick_custom_food_brand(query, context, f"{choice_token}:0"))

    assert context.user_data["custom_food_manufacturer_name"] == "Burger King"
    assert context.user_data["mode"] == "custom_food_macros"

    asyncio.run(bot._handle_custom_food_macros(update, context, "250 12 8 30"))

    definition = context.user_data["custom_food_definition"]
    assert definition.manufacturer_name == "Burger King"
    assert definition.serving_size == ""
    assert definition.metric_serving_size == "100g"
    assert context.user_data["mode"] == "custom_food_confirm"


def test_custom_food_brand_allows_explicit_new_free_text_when_catalog_has_no_match() -> None:
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(
        user_data={
            "mode": "custom_food_brand",
            "custom_food_title": "QA custom brand",
            "group_id": "group",
        }
    )
    bot = object.__new__(TelegramRecipeBot)
    bot.sync_engine = SimpleNamespace(
        suggest_custom_food_brands=AsyncMock(return_value=[]),
    )

    asyncio.run(bot._handle_custom_food_brand(update, context, "  Новый   Бренд  "))
    query = SimpleNamespace(edit_message_text=AsyncMock())
    choice_token = context.user_data["custom_food_brand_choice_token"]
    asyncio.run(bot._use_custom_food_brand_text(query, context, choice_token))

    assert context.user_data["custom_food_manufacturer_name"] == "Новый Бренд"
    assert context.user_data["mode"] == "custom_food_macros"


def test_custom_food_brand_rejects_stale_suggestion_without_changing_current_choice() -> None:
    context = SimpleNamespace(
        user_data={
            "mode": "custom_food_brand_choice",
            "custom_food_brand_query": "Санта",
            "custom_food_brand_suggestions": ["Санта"],
            "custom_food_brand_choice_token": "current",
        }
    )
    query = SimpleNamespace(edit_message_text=AsyncMock())
    bot = object.__new__(TelegramRecipeBot)

    asyncio.run(bot._pick_custom_food_brand(query, context, "old:0"))

    assert context.user_data["mode"] == "custom_food_brand_choice"
    assert context.user_data["custom_food_brand_suggestions"] == ["Санта"]
    assert "устарел" in query.edit_message_text.await_args.args[0]


def test_custom_food_macro_step_notifies_user_about_inconsistent_kbju() -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(
        user_data={
            "mode": "custom_food_macros",
            "custom_food_title": "QA impossible macros",
        }
    )
    bot = object.__new__(TelegramRecipeBot)

    asyncio.run(bot._handle_custom_food_macros(update, context, "100 0 100 0"))

    notification = message.reply_text.await_args.args[0]
    assert "4×Б + 9×Ж + 4×У" in notification
    assert "примерно 900 ккал" in notification
    assert context.user_data["mode"] == "custom_food_macros"
    assert "custom_food_definition" not in context.user_data


def test_next_food_usage_refresh_runs_at_noon_in_bot_timezone() -> None:
    bot = object.__new__(TelegramRecipeBot)
    bot.sync_engine = type("Engine", (), {"timezone": "Europe/Minsk"})()
    timezone = ZoneInfo("Europe/Minsk")

    before_noon = TelegramRecipeBot._next_food_usage_refresh_at(
        bot,
        dt.datetime(2026, 6, 21, 11, 30, tzinfo=timezone),
    )
    after_noon = TelegramRecipeBot._next_food_usage_refresh_at(
        bot,
        dt.datetime(2026, 6, 21, 12, 1, tzinfo=timezone),
    )

    assert before_noon == dt.datetime(2026, 6, 21, 12, 0, tzinfo=timezone)
    assert after_noon == dt.datetime(2026, 6, 22, 12, 0, tzinfo=timezone)


def test_recipe_list_create_refreshes_live_titles_and_ignores_reconciled_stale_recipe(tmp_path) -> None:
    class FakeQuery:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def edit_message_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.messages.append(text)

    class FakeContext:
        def __init__(self) -> None:
            self.user_data = {
                "recipe_list_title": "Блины тонкие",
                "group_id": "group",
                "recipe_list_draft": [object()],
                "recipe_list_unresolved": [],
                "recipe_list_portions": Decimal("1"),
                "recipe_list_steps": [],
            }
            self.chat_data: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, storage: Storage) -> None:
            self.storage = storage
            self.created_titles: list[str] = []

        async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
            self.storage.reconcile_group_remote_recipes(group_id, {"tg11": set()})
            return []

        async def create_recipe_from_list(self, group_id: str, title: str, items, updated_by: int, **kwargs):  # noqa: ANN001, ANN003
            self.created_titles.append(title)
            recipe_id = self.storage.create_recipe(title, "", Decimal("1"), 0, 0, updated_by, group_id)
            return RecipeCreateResult(recipe_id=recipe_id, results=[], title=title)

    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        stale_id = storage.create_recipe("Блины тонкие", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(stale_id, "tg11", "old-111", last_synced_version=1)
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = FakeEngine(storage)
        query = FakeQuery()
        context = FakeContext()

        asyncio.run(TelegramRecipeBot._create_recipe_list_from_draft(bot, query, context, 11))

        assert bot.sync_engine.created_titles == ["Блины тонкие"]
        assert storage.get_recipe(stale_id) is None
        assert all("Рецепт с таким названием уже есть" not in message for message in query.messages)
    finally:
        storage.close()


def test_recipe_list_create_still_prompts_for_current_live_duplicate(tmp_path) -> None:
    class FakeQuery:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def edit_message_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.messages.append(text)

    class FakeContext:
        def __init__(self) -> None:
            self.user_data = {
                "recipe_list_title": "Блины тонкие",
                "group_id": "group",
                "recipe_list_draft": [object()],
                "recipe_list_unresolved": [],
                "recipe_list_portions": Decimal("1"),
                "recipe_list_steps": [],
            }
            self.chat_data: dict[str, object] = {}

    class FakeEngine:
        async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
            return [Recipe(id="live", title="Блины тонкие", group_id=group_id, remote_ids={"tg11": "111"})]

        async def create_recipe_from_list(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("duplicate must block creation")

    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = FakeEngine()
        query = FakeQuery()
        context = FakeContext()

        asyncio.run(TelegramRecipeBot._create_recipe_list_from_draft(bot, query, context, 11))

        assert query.messages[-1].startswith("Рецепт с таким названием уже есть")
    finally:
        storage.close()


def test_recipe_cache_is_reloaded_from_authoritative_cookbook_after_sync() -> None:
    fresh = Recipe(id="fresh", title="Омлет", group_id="group", remote_ids={"tg11": "new-111"})
    bot = object.__new__(TelegramRecipeBot)
    bot.sync_engine = SimpleNamespace(load_remote_recipe_index=AsyncMock(return_value=[fresh]))
    context = SimpleNamespace(
        chat_data={"recipe_cache_group_id": "group", "recipe_cache": [Recipe(id="old", title="Старый")]}
    )

    refreshed = asyncio.run(bot._refresh_recipe_cache_after_sync(context, "group"))

    assert refreshed is True
    assert bot._recipe_cache(context, "group") == [fresh]
    bot.sync_engine.load_remote_recipe_index.assert_awaited_once_with("group")


def test_recipe_rename_replaces_duplicate_only_after_selected_recipe_is_renamed(tmp_path) -> None:
    selected = Recipe(
        id="selected",
        title="Старое имя",
        group_id="group",
        remote_ids={"tg11": "111"},
        remote_ids_by_account={"tg11": ["111"]},
    )
    duplicate = Recipe(
        id="duplicate",
        title="Новое имя",
        group_id="group",
        remote_ids={"tg11": "222"},
        remote_ids_by_account={"tg11": ["222"]},
    )
    renamed = Recipe(
        id="renamed",
        title="Новое имя",
        group_id="group",
        remote_ids={"tg11": "111"},
        remote_ids_by_account={"tg11": ["111"]},
    )

    class FakeTarget:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def edit_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.messages.append(text)

    class FakeEngine:
        def __init__(self) -> None:
            self.events: list[tuple[str, set[tuple[str, str]]]] = []
            self.loads = 0

        async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
            self.loads += 1
            return [selected, duplicate] if self.loads == 1 else [renamed]

        async def rename_live_recipe_everywhere(self, recipe: Recipe, title: str) -> list[AccountSyncResult]:
            self.events.append(("rename", {(key, value) for key, value in recipe.remote_ids.items()}))
            assert title == "Новое имя"
            return [AccountSyncResult("tg11", "111", True, "переименован")]

        async def delete_live_recipe_everywhere(self, recipe: Recipe) -> list[AccountSyncResult]:
            self.events.append(("delete", {(key, value) for key, value in recipe.remote_ids.items()}))
            return [AccountSyncResult("tg11", "222", True, "удален")]

    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = FakeEngine()
        context = SimpleNamespace(
            user_data={
                "recipe_rename_title": "Новое имя",
                "recipe_rename_group_id": "group",
                "recipe_rename_updated_by": 11,
                "recipe_rename_ref": selected,
            },
            chat_data={},
        )
        target = FakeTarget()

        asyncio.run(bot._execute_recipe_rename(target, context, replace_existing=True))

        assert bot.sync_engine.events == [
            ("rename", {("tg11", "111")}),
            ("delete", {("tg11", "222")}),
        ]
        assert "Прежний одноимённый рецепт удалён" in target.messages[-1]
        assert context.user_data["current_recipe_id"] == "renamed"
    finally:
        storage.close()


def test_accounts_keyboard_and_lookup_allow_only_owner_account_actions(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        storage.upsert_fatsecret_account(11, "Каба", "one@example.com", "secret", "BY", "ru")
        storage.register_user(22, "Two")
        storage.join_group_by_code(22, group.invite_code)
        storage.upsert_fatsecret_account(22, "Света", "two@example.com", "secret", "BY", "ru")
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage

        keyboard = TelegramRecipeBot._accounts_keyboard(bot, 22, group)
        flat_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        _, own_account = TelegramRecipeBot._active_group_account(bot, 22, "tg22")
        _, other_account = TelegramRecipeBot._active_group_account(bot, 22, "tg11")

        assert "Поменять ник: Света" in flat_texts
        assert "Отсоединить: Света" in flat_texts
        assert "Удалить подключение: Света" in flat_texts
        assert "Поменять ник: Каба" not in flat_texts
        assert "Удалить подключение: Каба" not in flat_texts
        assert own_account is not None
        assert other_account is None
    finally:
        storage.close()


def test_accounts_and_groups_keyboards_support_multiple_owned_accounts_and_switching(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        first_group = storage.create_group(11, "QA")
        first = storage.create_fatsecret_account(
            11, "Kabaye", "one@example.com", "secret", "BY", "ru", group_id=first_group.id
        )
        second = storage.create_fatsecret_account(
            11, "Test", "test@example.com", "secret", "BY", "ru", group_id=first_group.id
        )
        second_group = storage.create_group(11, "Основная")
        storage.set_active_group_for_user(11, first_group.id)
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage

        account_keyboard = TelegramRecipeBot._accounts_keyboard(bot, 11, first_group)
        account_callbacks = [
            button.callback_data
            for row in account_keyboard.inline_keyboard
            for button in row
        ]
        group_keyboard = TelegramRecipeBot._groups_keyboard(bot, 11)
        group_callbacks = [button.callback_data for row in group_keyboard.inline_keyboard for button in row]

        assert f"account_label:{first}" in account_callbacks
        assert f"account_label:{second}" in account_callbacks
        assert f"account_detach:{first}" in account_callbacks
        assert f"account_delete:{second}" in account_callbacks
        assert f"group_switch:{second_group.id}" in group_callbacks
        assert "group_create:0" in group_callbacks
        assert "group_join:0" in group_callbacks
    finally:
        storage.close()
