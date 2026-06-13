from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.telegram_bot import _parse_recipe_list_lines


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
