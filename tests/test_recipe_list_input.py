from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import Ingredient
from fatsecret_bot.sync import ResolvedRecipeListItem
from fatsecret_bot.telegram_bot import _format_resolved_item, _parse_recipe_list_lines


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
