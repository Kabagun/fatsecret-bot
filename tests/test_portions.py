from __future__ import annotations

from decimal import Decimal

import pytest

from fatsecret_bot.portions import grams_from_portion, is_explicit_weight_portion, portion_unit_size


@pytest.mark.parametrize(
    ("amount", "description", "expected"),
    [
        ("2", "100г", "200"),
        ("1.5", "200 мл", "300.0"),
        ("5", "г", "5"),
        ("5", "", "5"),
        ("2", "0,5 г", "1.0"),
        ("2", "serving", None),
    ],
)
def test_grams_from_portion_parses_shared_weight_rules(
    amount: str,
    description: str,
    expected: str | None,
) -> None:
    value = grams_from_portion(Decimal(amount), description)

    assert value == (Decimal(expected) if expected is not None else None)


def test_grams_from_portion_prefers_explicit_xml_signals() -> None:
    assert grams_from_portion(
        Decimal("2"),
        "100г",
        explicit_grams=Decimal("77"),
        grams_per_portion=Decimal("55"),
    ) == Decimal("77")
    assert grams_from_portion(
        Decimal("2"),
        "100г",
        grams_per_portion=Decimal("55"),
    ) == Decimal("110")


def test_explicit_weight_portions_exclude_empty_and_serving_descriptions() -> None:
    assert portion_unit_size(" 108,5 г") == Decimal("108.5")
    assert is_explicit_weight_portion("г") is True
    assert is_explicit_weight_portion("100г") is True
    assert is_explicit_weight_portion("") is False
    assert is_explicit_weight_portion("serving") is False
