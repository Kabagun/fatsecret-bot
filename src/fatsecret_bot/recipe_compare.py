from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal

from .models import Ingredient, Recipe, RecipeFingerprint


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _text(value: str) -> str:
    return " ".join(value.strip().split())


def _ingredient_key(
    ingredient: Ingredient,
    *,
    include_food_id: bool,
) -> tuple[str, str, str, str, str, str | None]:
    return (
        ingredient.food_id if include_food_id else "",
        _text(ingredient.title).casefold(),
        (ingredient.portion_id or "0") if include_food_id else "",
        _decimal_text(ingredient.amount) or "0",
        _text(ingredient.portion_description).casefold(),
        _decimal_text(ingredient.grams),
    )


def _recipe_fingerprint(recipe: Recipe, *, include_food_ids: bool) -> RecipeFingerprint:
    ingredient_counts = Counter(
        _ingredient_key(ingredient, include_food_id=include_food_ids)
        for ingredient in recipe.ingredients
    )
    ingredients = [
        {"row": list(key), "count": count}
        for key, count in sorted(ingredient_counts.items())
    ]
    payload = {
        "title": _text(recipe.title).casefold(),
        "description": _text(recipe.description),
        "portions": _decimal_text(recipe.portions),
        "prep_time": int(recipe.prep_time),
        "cook_time": int(recipe.cook_time),
        "steps": [_text(step) for step in recipe.steps if _text(step)],
        "ingredients": ingredients,
    }
    canonical_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return RecipeFingerprint(
        digest=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        canonical_json=canonical_json,
    )


def recipe_fingerprint(recipe: Recipe) -> RecipeFingerprint:
    """Return a strict target fingerprint while ignoring recipe and ingredient row ids."""
    return _recipe_fingerprint(recipe, include_food_ids=True)


def recipe_content_fingerprint(recipe: Recipe) -> RecipeFingerprint:
    """Return a cross-account fingerprint that ignores account-specific food and portion ids."""
    return _recipe_fingerprint(recipe, include_food_ids=False)


def recipe_fingerprint_diff(expected: RecipeFingerprint, actual: RecipeFingerprint) -> list[str]:
    """Return concise field-level differences between two canonical fingerprints."""
    if expected.digest == actual.digest:
        return []
    expected_data = json.loads(expected.canonical_json)
    actual_data = json.loads(actual.canonical_json)
    differences: list[str] = []
    labels = {
        "title": "название",
        "description": "описание",
        "portions": "порции",
        "prep_time": "подготовка",
        "cook_time": "готовка",
        "steps": "шаги",
        "ingredients": "ингредиенты",
    }
    for field, label in labels.items():
        if expected_data.get(field) == actual_data.get(field):
            continue
        if field in {"steps", "ingredients"}:
            differences.append(
                f"{label}: ожидалось {len(expected_data.get(field) or [])}, "
                f"получено {len(actual_data.get(field) or [])}"
            )
        else:
            differences.append(
                f"{label}: ожидалось {expected_data.get(field)!r}, получено {actual_data.get(field)!r}"
            )
    return differences
