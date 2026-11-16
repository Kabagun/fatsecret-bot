"""The shared, import-compatible recipe export for Telegram and MCP."""
from __future__ import annotations

from decimal import Decimal

from .models import Recipe
from .portions import grams_from_portion
from .sync import recipe_cooked_weight_from_description


def _decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def recipe_export_payload(recipe: Recipe) -> str:
    ingredient_lines, missing_weights = [], []
    for ingredient in recipe.ingredients:
        grams = grams_from_portion(ingredient.amount, ingredient.portion_description, explicit_grams=ingredient.grams)
        if grams is None or grams <= 0:
            missing_weights.append(ingredient.title.strip() or "Без названия")
            continue
        ingredient_lines.append(f"{ingredient.title.strip() or 'Без названия'} {_decimal(grams)}")
    if missing_weights:
        raise ValueError(f"Не удалось определить массу в граммах: {', '.join(missing_weights)}.")
    if not ingredient_lines:
        raise ValueError("В рецепте нет ингредиентов для экспорта.")
    lines = [f"Порций: {_decimal(recipe.portions)}"]
    cooked_weight = recipe_cooked_weight_from_description(recipe.description)
    if cooked_weight is not None:
        lines.append(f"Готовый вес: {_decimal(cooked_weight)}")
    lines.extend(ingredient_lines)
    steps = [step.strip() for step in recipe.steps if step.strip()]
    if steps:
        lines.extend(["", "Шаги:", *steps])
    return "\n".join(lines)
