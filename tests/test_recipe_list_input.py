from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import Ingredient
from fatsecret_bot.sync import RecipeListItem, ResolvedRecipeListItem
from fatsecret_bot.telegram_bot import (
    _format_recipe_list_draft,
    _format_resolved_item,
    _parse_recipe_list_lines,
    _parse_recipe_list_payload,
    _parse_recipe_steps,
    _recipe_list_draft_keyboard,
)


def test_parse_recipe_list_lines_uses_last_number_as_grams() -> None:
    items, bad_lines = _parse_recipe_list_lines(
        """
        Филе 100
        Теос греческий 200,5
        Масло оливковое extra 6
        """
    )

    assert bad_lines == []
    assert [(item.query, item.grams) for item in items] == [
        ("Филе", Decimal("100")),
        ("Теос греческий", Decimal("200.5")),
        ("Масло оливковое extra", Decimal("6")),
    ]


def test_parse_recipe_list_lines_reports_bad_lines() -> None:
    items, bad_lines = _parse_recipe_list_lines(
        """
        Филе сто
        Масло 0
        Теос 100
        """
    )

    assert [(item.query, item.grams) for item in items] == [("Теос", Decimal("100"))]
    assert bad_lines == ["Филе сто", "Масло 0"]


def test_parse_recipe_list_payload_splits_ingredients_and_steps() -> None:
    portions, items, bad_lines, steps = _parse_recipe_list_payload(
        """
        Порций: 4
        Филе 300
        Куркума 5

        Шаги:
        1. Нарезать филе
        2. Запечь
        - Подать
        4. Лишнее
        """
    )

    assert portions == Decimal("4")
    assert bad_lines == []
    assert [(item.query, item.grams) for item in items] == [
        ("Филе", Decimal("300")),
        ("Куркума", Decimal("5")),
    ]
    assert steps == ["Нарезать филе", "Запечь", "Подать", "Лишнее"]


def test_parse_recipe_list_payload_requires_portions_separately() -> None:
    portions, items, bad_lines, steps = _parse_recipe_list_payload(
        """
        Филе 300
        Шаги:
        Запечь
        """
    )

    assert portions is None
    assert bad_lines == []
    assert [(item.query, item.grams) for item in items] == [("Филе", Decimal("300"))]
    assert steps == ["Запечь"]


def test_format_resolved_item_shows_macros_per_100g_and_brand() -> None:
    item = ResolvedRecipeListItem(
        requested_query="кетчуп",
        grams=Decimal("25"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="f1",
            title="Кетчуп",
            portion_id="p1",
            amount=Decimal("25"),
            portion_description="г",
        ),
        source="FatSecret",
        brand="Махеевъ",
        energy_per_100g=Decimal("96"),
        protein_per_100g=Decimal("1.2"),
        fat_per_100g=Decimal("0.1"),
        carbohydrate_per_100g=Decimal("25.2"),
    )

    assert _format_resolved_item(item) == "- Кетчуп (Махеевъ) | 100г: 96/1.2/0.1/25.2 | масса: 25г"


def test_format_resolved_item_keeps_zero_energy_visible() -> None:
    item = ResolvedRecipeListItem(
        requested_query="вода",
        grams=Decimal("420"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="food-water",
            title="Вода",
            portion_id="0",
            amount=Decimal("4.2"),
            portion_description="100г",
        ),
        source="FatSecret",
        energy_per_100g=Decimal("0"),
        protein_per_100g=Decimal("0"),
        fat_per_100g=Decimal("0"),
        carbohydrate_per_100g=Decimal("0"),
    )

    assert _format_resolved_item(item) == "- Вода | 100г: 0/0/0/0 | масса: 420г"


def test_parse_recipe_steps_keeps_first_100_non_empty_lines() -> None:
    steps = "\n".join(f"Шаг {index}" for index in range(1, 102))

    assert _parse_recipe_steps(steps) == [f"Шаг {index}" for index in range(1, 101)]
    assert _parse_recipe_steps("-") == []


def test_format_recipe_list_draft_includes_steps() -> None:
    item = ResolvedRecipeListItem(
        requested_query="филе",
        grams=Decimal("100"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="f1",
            title="Куриное Филе",
            portion_id="p1",
            amount=Decimal("100"),
            portion_description="г",
        ),
        source="FatSecret",
        energy_per_100g=Decimal("110"),
        protein_per_100g=Decimal("23"),
        fat_per_100g=Decimal("2"),
        carbohydrate_per_100g=Decimal("0"),
    )

    text = _format_recipe_list_draft("Тест", [item], ["Смешать", "Запечь"], portions=Decimal("2"))

    assert "Порций: 2" in text
    assert "<b>Шаги</b>" in text
    assert "1. Смешать" in text
    assert "2. Запечь" in text


def test_recipe_list_draft_shows_unresolved_items_and_blocks_create() -> None:
    item = ResolvedRecipeListItem(
        requested_query="филе",
        grams=Decimal("100"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="f1",
            title="Куриное Филе",
            portion_id="p1",
            amount=Decimal("100"),
            portion_description="г",
        ),
        source="FatSecret",
    )
    unresolved = [RecipeListItem(query="Приправа для фарша Green", grams=Decimal("3"))]

    text = _format_recipe_list_draft("Тест", [item], unresolved=unresolved)
    keyboard = _recipe_list_draft_keyboard([item], unresolved=unresolved)
    flat_buttons = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "<b>Нужно заполнить или удалить</b>" in text
    assert "- ? Приправа для фарша Green | масса: 3г" in text
    assert "Заполнить: Приправа для фарша Green" in flat_buttons
    assert "Удалить" in flat_buttons
    assert "Создать рецепт" not in flat_buttons
