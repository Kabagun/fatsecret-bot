from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import Ingredient, Recipe
from fatsecret_bot.storage import Storage
from fatsecret_bot.telegram_bot import TelegramRecipeBot, _format_recipe, _recipe_actions_keyboard


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
    assert "- Соус: 0.06 порции" in text
    assert "- Кетчуп: 300г" in text
    assert "<b>Шаги</b>" in text
    assert "1. Смешать" in text


def test_recipe_actions_keyboard_keeps_only_recipe_actions_and_list_return() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=1, page_action="list", total_pages=3)
    rows = keyboard.inline_keyboard

    assert [button.text for button in rows[0]] == ["Синхронизировать"]
    assert [button.text for button in rows[1]] == ["Удалить в FatSecret"]
    assert [button.text for button in rows[2]] == ["К списку"]
    assert rows[2][0].callback_data == "list:1"
    flat_texts = [button.text for row in rows for button in row]
    assert "Назад" not in flat_texts
    assert "Дальше" not in flat_texts
    assert "Поиск" not in flat_texts
    assert "Создать из списка" not in flat_texts
    assert "В меню" not in flat_texts


def test_recipe_actions_keyboard_keeps_actions_without_navigation() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=0, page_action="list", total_pages=1)
    flat_texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Назад" not in flat_texts
    assert "Дальше" not in flat_texts
    assert "Синхронизировать" in flat_texts
    assert "Удалить в FatSecret" in flat_texts


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
        assert "Выйти: Света" in flat_texts
        assert "Поменять ник: Каба" not in flat_texts
        assert "Выйти: Каба" not in flat_texts
        assert own_account is not None
        assert other_account is None
    finally:
        storage.close()
