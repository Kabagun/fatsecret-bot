from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import Ingredient, Recipe
from fatsecret_bot.telegram_bot import _format_recipe, _recipe_actions_keyboard


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


def test_recipe_actions_keyboard_starts_with_list_navigation() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=1, page_action="list", total_pages=3)
    rows = keyboard.inline_keyboard

    assert [button.text for button in rows[0]] == ["Назад", "2/3", "Дальше"]
    assert rows[0][1].callback_data == "noop:0"
    assert [button.text for button in rows[1]] == ["Поиск", "Создать из списка"]
    assert [button.text for button in rows[-1]] == ["Удалить в FatSecret", "В меню"]
