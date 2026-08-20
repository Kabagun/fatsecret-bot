from __future__ import annotations

from decimal import Decimal

from fatsecret_bot.models import Ingredient, Recipe
from fatsecret_bot.recipe_compare import recipe_content_fingerprint, recipe_fingerprint, recipe_fingerprint_diff


def _ingredient(identifier: str, title: str, amount: str) -> Ingredient:
    return Ingredient(
        id=identifier,
        recipe_id="recipe",
        food_id=f"food-{title}",
        title=title,
        portion_id="0",
        amount=Decimal(amount),
        portion_description="100г",
        remote_ingredient_id=f"remote-{identifier}",
        grams=Decimal(amount) * 100,
    )


def test_recipe_fingerprint_ignores_row_ids_and_ingredient_order_but_keeps_multiset() -> None:
    first = Recipe(
        id="local-a",
        title="Омлет",
        portions=Decimal("2.0"),
        steps=["Смешать", "Запечь"],
        ingredients=[_ingredient("a", "Яйцо", "1"), _ingredient("b", "Соль", "0.02")],
    )
    second = Recipe(
        id="remote-b",
        title="  омлет ",
        portions=Decimal("2"),
        steps=["Смешать", "Запечь"],
        ingredients=[_ingredient("x", "Соль", "0.02"), _ingredient("y", "Яйцо", "1")],
    )

    assert recipe_fingerprint(first).digest == recipe_fingerprint(second).digest

    second.ingredients.append(_ingredient("z", "Соль", "0.02"))
    assert recipe_fingerprint(first).digest != recipe_fingerprint(second).digest


def test_recipe_fingerprint_diff_reports_metadata_steps_and_ingredients() -> None:
    expected = Recipe(
        id="a",
        title="Омлет",
        portions=Decimal("2"),
        steps=["Смешать"],
        ingredients=[_ingredient("a", "Яйцо", "1")],
    )
    actual = Recipe(
        id="b",
        title="Омлет",
        portions=Decimal("4"),
        steps=["Приготовить", "Подать"],
        ingredients=[],
    )

    differences = recipe_fingerprint_diff(recipe_fingerprint(expected), recipe_fingerprint(actual))

    assert any(item.startswith("порции:") for item in differences)
    assert "шаги: ожидалось 1, получено 2" in differences
    assert "ингредиенты: ожидалось 1, получено 0" in differences


def test_cross_account_content_fingerprint_ignores_cloned_custom_food_id() -> None:
    first = Recipe(id="a", title="Мороженое", ingredients=[_ingredient("a", "Nina Farina", "0.75")])
    second_ingredient = _ingredient("b", "Nina Farina", "0.75")
    second_ingredient.food_id = "target-custom-food"
    second_ingredient.portion_id = "target-custom-portion"
    second = Recipe(id="b", title="Мороженое", ingredients=[second_ingredient])

    assert recipe_fingerprint(first).digest != recipe_fingerprint(second).digest
    assert recipe_content_fingerprint(first).digest == recipe_content_fingerprint(second).digest


def test_cross_account_content_fingerprint_uses_resolved_grams_not_portion_representation() -> None:
    first = Recipe(
        id="a",
        title="Омлет",
        ingredients=[
            Ingredient(
                "a",
                "a",
                "egg-a",
                "Яйцо",
                "unit-portion",
                Decimal("2"),
                "штуки",
                grams=Decimal("120"),
            )
        ],
    )
    second = Recipe(
        id="b",
        title="Омлет",
        ingredients=[
            Ingredient(
                "b",
                "b",
                "egg-b",
                "Яйцо",
                "gram-portion",
                Decimal("120"),
                "г",
                grams=Decimal("120"),
            )
        ],
    )

    assert recipe_fingerprint(first).digest != recipe_fingerprint(second).digest
    assert recipe_content_fingerprint(first).digest == recipe_content_fingerprint(second).digest
