from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import Ingredient, Recipe
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
        ],
    )

    text = _format_recipe(recipe)

    assert "Remote:" not in text
    assert "Порций: 2;" in text
    assert "- Яичный Белок: 125.25г" in text
    assert "- Соус: 0.06 порции" in text


def test_recipe_actions_keyboard_uses_navigation_without_page_label() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=1, page_action="list", total_pages=3)
    rows = keyboard.inline_keyboard

    assert [button.text for button in rows[0]] == ["Назад", "Дальше"]
    assert all("/" not in button.text for button in rows[0])
    flat_texts = [button.text for row in rows for button in row]
    assert "Синхронизировать" not in flat_texts
    assert "Удалить в FatSecret" not in flat_texts
    assert "В меню" not in flat_texts


def test_recipe_actions_keyboard_is_absent_without_navigation() -> None:
    assert _recipe_actions_keyboard("recipe-1", page=0, page_action="list", total_pages=1) is None


def test_recipe_list_keyboard_keeps_only_recipe_buttons_and_navigation_inline() -> None:
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
    assert "Удалить несколько" not in flat_texts
    assert "В меню" not in flat_texts
