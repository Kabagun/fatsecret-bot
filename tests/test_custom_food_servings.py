from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fatsecret_bot.fatsecret_client import FatSecretClient, FatSecretError
from fatsecret_bot.models import CustomFoodDefinition, FatSecretAccountConfig, FatSecretDeviceConfig, FoodSearchResult
from fatsecret_bot.sync import CustomFoodCreateResult, RecipeSyncEngine, _custom_food_definition_matches, _ingredient_from_food_result
from fatsecret_bot.telegram_bot import (
    TelegramRecipeBot,
    _custom_food_basis_keyboard,
    _format_custom_food_draft,
    _parse_custom_food_macros,
)


def serving(weight: str = "50g") -> CustomFoodDefinition:
    return CustomFoodDefinition(
        "", "Блин", "", "PerServing", "порция", weight,
        {"calories": Decimal("120"), "protein": Decimal("4"),
         "totalFat": Decimal("4"), "carbohydrate": Decimal("17")},
    )


def test_only_100g_and_one_serving_are_offered() -> None:
    assert [button.text for row in _custom_food_basis_keyboard().inline_keyboard for button in row] == [
        "На 100 г", "На 1 порцию",
    ]


def test_serving_macros_are_not_limited_to_100g_mass() -> None:
    nutrients = _parse_custom_food_macros("800 40 40 80", grams=Decimal("300"))
    assert nutrients["protein"] == Decimal("40")
    _parse_custom_food_macros("800 40 40 80", grams=None)
    with pytest.raises(ValueError, match="сумма"):
        _parse_custom_food_macros("800 40 40 80")
    with pytest.raises(ValueError, match="сумма"):
        _parse_custom_food_macros("120 4 4 17", grams=Decimal("20"))


def test_recipe_from_known_serving_weight_normalizes_macros() -> None:
    item = RecipeSyncEngine.custom_food_recipe_list_item(
        serving(), CustomFoodCreateResult("run", "Блин", {"qa": "123"}), Decimal("75"), "Блин"
    )
    assert item.grams == Decimal("75")
    assert item.energy_per_100g == Decimal("240")
    assert item.protein_per_100g == Decimal("8")
    assert item.fat_per_100g == Decimal("8")
    assert item.carbohydrate_per_100g == Decimal("34")
    assert item.custom_food_ids == {"qa": "123"}
    assert item.ingredient.amount == Decimal("1.5")
    assert item.ingredient.portion_description == "порция"


def test_recipe_cannot_invent_grams_for_weightless_serving() -> None:
    RecipeSyncEngine._validate_custom_food_definition(serving(""))
    with pytest.raises(FatSecretError, match="вес одной порции"):
        RecipeSyncEngine.custom_food_recipe_list_item(
            serving(""), CustomFoodCreateResult("run", "Блин", {"qa": "123"}), Decimal("75"), "Блин"
        )


@pytest.mark.parametrize("weight", ["0g", "-1g", "NaNg", "Infinityg", "100001g", "50ml", "1 tsp"])
def test_invalid_or_unsupported_serving_weight_is_rejected(weight: str) -> None:
    with pytest.raises(FatSecretError, match="Вес порции"):
        RecipeSyncEngine._validate_custom_food_definition(serving(weight))


@pytest.mark.parametrize("basis,weight", [("grams", None), ("serving", "50,5"), ("serving", None)])
def test_wizard_keeps_selected_nutrition_basis(basis: str, weight: str | None) -> None:
    context = SimpleNamespace(user_data={
        "mode": "custom_food_basis", "custom_food_title": "Блин", "custom_food_origin": "standalone",
    })
    query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
    bot = object.__new__(TelegramRecipeBot)
    asyncio.run(bot._pick_custom_food_basis(query, context, basis))
    if basis == "serving":
        assert context.user_data["mode"] == "custom_food_weight"
        if weight is None:
            asyncio.run(bot._skip_custom_food_weight(query, context))
        else:
            asyncio.run(bot._handle_custom_food_weight(update, context, weight))
    assert context.user_data["mode"] == "custom_food_macros"
    asyncio.run(bot._handle_custom_food_macros(update, context, "120 4 4 17"))
    definition = context.user_data["custom_food_definition"]
    assert definition.serving_type == ("PerServing" if basis == "serving" else "Per100g")
    assert definition.nutrients["calories"] == Decimal("120")
    assert definition.metric_serving_size == ("100g" if basis == "grams" else "50.5g" if weight else "")
    assert ("на 1 порцию" if basis == "serving" else "на 100 г") in _format_custom_food_draft(definition)


def test_recipe_wizard_requires_serving_weight_and_rejects_stale_skip() -> None:
    context = SimpleNamespace(user_data={
        "mode": "custom_food_basis", "custom_food_title": "Блин", "custom_food_origin": "recipe",
    })
    query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock())
    bot = object.__new__(TelegramRecipeBot)
    asyncio.run(bot._pick_custom_food_basis(query, context, "serving"))
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is None
    asyncio.run(bot._skip_custom_food_weight(query, context))
    assert context.user_data["mode"] == "custom_food_weight"
    assert "нужен вес" in query.answer.await_args.args[0]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", "100001", "50 мл"])
def test_invalid_weight_keeps_wizard_on_weight_step(value: str) -> None:
    context = SimpleNamespace(user_data={"mode": "custom_food_weight"})
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
    bot = object.__new__(TelegramRecipeBot)
    asyncio.run(bot._handle_custom_food_weight(update, context, value))
    assert context.user_data["mode"] == "custom_food_weight"
    assert "custom_food_weight" not in context.user_data


def _client() -> FatSecretClient:
    return FatSecretClient(
        FatSecretAccountConfig("qa", "QA", "qa", "unused", "BY", "ru"),
        FatSecretDeviceConfig("11.5.0.4", "6", "30", "11", "QA", "1920x1080", "qa"),
    )


def _serving_xml(weight: str = "50.000") -> str:
    return f"""<recipe><id>1</id><title>Блин</title><isOwn>True</isOwn>
    <servingSize>порция</servingSize><servingAmount>{weight}</servingAmount><servingAmountUnit>g</servingAmountUnit>
    <gramsPerPortion>0.0</gramsPerPortion><defaultPortionID>0</defaultPortionID>
    <energyPerPortion>120.000</energyPerPortion><proteinPerPortion>4.000</proteinPerPortion>
    <fatPerPortion>4.000</fatPerPortion><carbohydratePerPortion>17.000</carbohydratePerPortion>
    <recipeportion><id>10</id><description>порция</description><gramWeight>{weight}</gramWeight>
    <defaultAmount>1.000</defaultAmount></recipeportion></recipe>"""


def test_android_readback_preserves_serving_despite_zero_grams_per_portion() -> None:
    client = _client()
    try:
        actual = client._parse_custom_food_definition(_serving_xml(), "1")
        assert _custom_food_definition_matches(serving(), actual)
        food = client._parse_food_detail(_serving_xml(), FoodSearchResult("1", "Блин"))
        assert food.grams_per_portion == Decimal("50")
        assert food.energy_per_portion == Decimal("240")
        ingredient = _ingredient_from_food_result(food, Decimal("75"))
        assert ingredient.amount == Decimal("1.5")
        assert ingredient.grams == Decimal("75")
    finally:
        asyncio.run(client.close())


def test_unknown_weight_sentinel_never_becomes_100_grams() -> None:
    client = _client()
    try:
        xml = _serving_xml("-79228162514264337593543950335")
        actual = client._parse_custom_food_definition(xml, "1")
        assert _custom_food_definition_matches(serving(""), actual)
        food = client._parse_food_detail(xml, FoodSearchResult("1", "Блин"))
        with pytest.raises(FatSecretError, match="не указан вес порции"):
            _ingredient_from_food_result(food, Decimal("75"))
    finally:
        asyncio.run(client.close())


def test_new_nutrition_steps_edit_one_bound_form() -> None:
    form = SimpleNamespace(edit_text=AsyncMock())
    context = SimpleNamespace(user_data={
        "mode": "custom_food_basis", "custom_food_title": "Блин", "custom_food_origin": "standalone",
    })
    query = SimpleNamespace(edit_message_text=AsyncMock(), answer=AsyncMock(), message=form)
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
    bot = object.__new__(TelegramRecipeBot)
    asyncio.run(bot._pick_custom_food_basis(query, context, "serving"))
    asyncio.run(bot._handle_custom_food_weight(update, context, "50"))
    asyncio.run(bot._handle_custom_food_macros(update, context, "120 4 4 17"))
    assert form.edit_text.await_count == 2
    update.effective_message.reply_text.assert_not_awaited()


def test_default_android_headers_include_public_protocol_and_device_descriptor() -> None:
    client = _client()
    try:
        headers = client._headers(content_type="application/json", device_key="qa-device")
        assert headers["Authorization"] == "FatSecret"
        assert headers["c_d"] == headers["c_desc"] == "qa-device"
    finally:
        asyncio.run(client.close())
