from __future__ import annotations

from decimal import Decimal

import pytest

from fatsecret_bot.models import CustomFoodDefinition, Ingredient
from fatsecret_bot.sync import RecipeListItem, ResolvedRecipeListItem
from fatsecret_bot.telegram_bot import (
    _custom_food_barcode_keyboard,
    _custom_food_brand_keyboard,
    _custom_food_brand_suggestions_keyboard,
    _custom_food_confirm_keyboard,
    _format_custom_food_created,
    _format_custom_food_draft,
    _format_recipe_list_draft,
    _format_resolved_item,
    _parse_custom_food_macros,
    _parse_recipe_list_lines,
    _parse_recipe_list_payload,
    _parse_recipe_steps,
    _recipe_list_candidate_keyboard,
    _recipe_list_draft_keyboard,
    _recipe_list_input_error_keyboard,
)


def test_custom_food_macro_parser_uses_compact_per_100g_order() -> None:
    assert _parse_custom_food_macros("250 12,5 8 30") == {
        "calories": Decimal("250"),
        "protein": Decimal("12.5"),
        "totalFat": Decimal("8"),
        "carbohydrate": Decimal("30"),
    }


def test_custom_food_macro_parser_rejects_impossible_calories_for_fat() -> None:
    with pytest.raises(ValueError, match=r"примерно 900 ккал, но указано 100 ккал"):
        _parse_custom_food_macros("100 0 100 0")


def test_custom_food_macro_parser_allows_normal_label_rounding() -> None:
    assert _parse_custom_food_macros("250 12 8 30")["calories"] == Decimal("250")


def test_custom_food_macro_parser_rejects_impossible_macro_mass() -> None:
    with pytest.raises(ValueError, match=r"сумма белков, жиров и углеводов равна 120 г"):
        _parse_custom_food_macros("480 40 40 40")


def test_custom_food_confirmation_shows_brand_barcode_and_group_scope() -> None:
    definition = CustomFoodDefinition(
        source_recipe_id="",
        title="QA product",
        manufacturer_name="Burger King",
        serving_type="Per100g",
        serving_size="",
        metric_serving_size="100g",
        nutrients={
            "calories": Decimal("250"),
            "protein": Decimal("12"),
            "totalFat": Decimal("8"),
            "carbohydrate": Decimal("30"),
        },
        barcode="4006381333931",
        barcode_type="EAN_13",
    )

    text = _format_custom_food_draft(definition)

    assert "QA product" in text
    assert "Burger King" in text
    assert "4006381333931" in text
    assert "во всех FatSecret аккаунтах" in text


def test_custom_food_created_message_explains_search_index_delay() -> None:
    text = _format_custom_food_created(
        "Стрипсы куриные",
        {"a2": "134894994", "a1": "134894999"},
        {"a1": "Святичек", "a2": "Анечка"},
    )

    assert "Святичек: <code>134894999</code>" in text
    assert "Анечка: <code>134894994</code>" in text
    assert "с задержкой в несколько минут" in text
    assert "по точному названию" in text


def test_custom_food_optional_steps_have_explicit_skip_buttons() -> None:
    barcode_buttons = [
        (button.text, button.callback_data)
        for row in _custom_food_barcode_keyboard().inline_keyboard
        for button in row
    ]
    brand_buttons = [
        (button.text, button.callback_data)
        for row in _custom_food_brand_keyboard().inline_keyboard
        for button in row
    ]

    assert ("⏭️ Без штрих-кода", "food_skip_barcode:0") in barcode_buttons
    assert ("⏭️ Без бренда", "food_skip_brand:0") in brand_buttons
    assert all(callback != "food_cancel:0" for _, callback in barcode_buttons + brand_buttons)


def test_custom_food_brand_suggestions_use_index_callbacks_and_keep_fallbacks() -> None:
    buttons = [
        (button.text, button.callback_data)
        for row in _custom_food_brand_suggestions_keyboard(
            ["Санта", "Санта Бремор"],
            "санта",
            "token",
        ).inline_keyboard
        for button in row
    ]

    assert ("Санта", "food_brand_pick:token:0") in buttons
    assert ("Санта Бремор", "food_brand_pick:token:1") in buttons
    assert ("Использовать введённое: санта", "food_brand_custom:token") in buttons
    assert ("⏭️ Без бренда", "food_skip_brand:0") in buttons
    assert all(callback != "food_cancel:0" for _, callback in buttons)


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
    parsed = _parse_recipe_list_payload(
        """
        Порций: 4
        Готовый вес: 415,5 г
        Филе 300
        Куркума 5

        Шаги:
        1. Нарезать филе
        2. Запечь
        - Подать
        4. Лишнее
        """
    )

    assert parsed.portions == Decimal("4")
    assert parsed.cooked_weight_grams == Decimal("415.5")
    assert parsed.bad_lines == []
    assert [(item.query, item.grams) for item in parsed.items] == [
        ("Филе", Decimal("300")),
        ("Куркума", Decimal("5")),
    ]
    assert parsed.steps == ["Нарезать филе", "Запечь", "Подать", "Лишнее"]


@pytest.mark.parametrize(
    ("line", "expected_bad_lines"),
    [
        ("Готовый вес: 0", ["Готовый вес: 0"]),
        ("Готовый вес: -10", ["Готовый вес: -10"]),
        ("Готовый вес: много", ["Готовый вес: много"]),
        (
            "Готовый вес: 415\nГотовый вес: 400 г",
            ["Готовый вес: 400 г"],
        ),
    ],
)
def test_parse_recipe_list_payload_rejects_invalid_or_duplicate_cooked_weight(
    line: str,
    expected_bad_lines: list[str],
) -> None:
    parsed = _parse_recipe_list_payload(f"Порций: 2\n{line}\nФиле 300")

    assert parsed.bad_lines == expected_bad_lines


def test_parse_recipe_list_payload_keeps_legacy_format_without_cooked_weight() -> None:
    parsed = _parse_recipe_list_payload("Порций: 2\nФиле 300\nШаги:\nЗапечь")

    assert parsed.portions == Decimal("2")
    assert parsed.cooked_weight_grams is None
    assert parsed.bad_lines == []
    assert parsed.steps == ["Запечь"]


def test_list_validation_errors_do_not_duplicate_reply_mode_cancel() -> None:
    assert _recipe_list_input_error_keyboard() is None


def test_parse_recipe_list_payload_requires_portions_separately() -> None:
    parsed = _parse_recipe_list_payload(
        """
        Филе 300
        Шаги:
        Запечь
        """
    )

    assert parsed.portions is None
    assert parsed.bad_lines == []
    assert [(item.query, item.grams) for item in parsed.items] == [("Филе", Decimal("300"))]
    assert parsed.steps == ["Запечь"]


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


def test_format_resolved_item_preserves_integer_trailing_zeroes() -> None:
    item = ResolvedRecipeListItem(
        requested_query="фарш",
        grams=Decimal("631"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="food-mince",
            title="Фарш Сочный",
            portion_id="0",
            amount=Decimal("6.31"),
            portion_description="100г",
        ),
        source="FatSecret",
        brand="Green",
        energy_per_100g=Decimal("320"),
        protein_per_100g=Decimal("15"),
        fat_per_100g=Decimal("29"),
        carbohydrate_per_100g=Decimal("0"),
    )

    assert _format_resolved_item(item) == "- Фарш Сочный (Green) | 100г: 320/15/29/0 | масса: 631г"


def test_recipe_list_candidate_keyboard_shows_brand_in_button_text() -> None:
    item = ResolvedRecipeListItem(
        requested_query="филе куриное",
        grams=Decimal("366"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="food-chicken",
            title="Филе Куриное",
            portion_id="p1",
            amount=Decimal("366"),
            portion_description="г",
        ),
        source="FatSecret",
        brand="Витконпродукт",
    )

    keyboard = _recipe_list_candidate_keyboard([item], page=1, has_next=False)
    flat_texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "11. Филе Куриное (Витконпродукт)" in flat_texts


def test_recipe_list_candidate_keyboard_keeps_objects_single_and_navigation_separate() -> None:
    candidates = [
        ResolvedRecipeListItem(
            requested_query=f"ingredient {index}",
            grams=Decimal("100"),
            ingredient=Ingredient(
                id=f"i{index}",
                recipe_id="",
                food_id=f"f{index}",
                title=f"Ингредиент {index}",
                portion_id="0",
                amount=Decimal("1"),
                portion_description="100г",
            ),
            source="FatSecret",
        )
        for index in range(2)
    ]

    rows = _recipe_list_candidate_keyboard(candidates, page=1, has_next=True).inline_keyboard

    assert [len(row) for row in rows] == [1, 1, 2, 1]
    assert [button.callback_data for row in rows[:2] for button in row] == [
        "recipe_list_pick:0",
        "recipe_list_pick:1",
    ]
    assert [button.callback_data for button in rows[-2]] == [
        "recipe_list_cpage:0",
        "recipe_list_cpage:2",
    ]
    assert [(button.text, button.callback_data) for button in rows[-1]] == [
        ("⬅️ К проверке", "recipe_list_back:0")
    ]


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


def test_recipe_list_draft_shows_cooked_weight_math_without_rescaling_nutrition() -> None:
    item = ResolvedRecipeListItem(
        requested_query="филе",
        grams=Decimal("300"),
        ingredient=Ingredient(
            id="i1",
            recipe_id="",
            food_id="f1",
            title="Куриное Филе",
            portion_id="p1",
            amount=Decimal("300"),
            portion_description="г",
        ),
        source="FatSecret",
        energy_per_100g=Decimal("100"),
        protein_per_100g=Decimal("20"),
        fat_per_100g=Decimal("5"),
        carbohydrate_per_100g=Decimal("0"),
    )
    unresolved = [RecipeListItem(query="Маринад", grams=Decimal("200"))]

    text = _format_recipe_list_draft(
        "Тест",
        [item],
        unresolved=unresolved,
        cooked_weight_grams=Decimal("400"),
    )

    assert "Вес ингредиентов: 500 г" in text
    assert "Готовый вес: 400 г" in text
    assert "Коэффициент: 1.250" in text
    assert "Пока учтено ккал/Б/Ж/У: 300/60/15/0" in text
    assert "масса: 300г" in text
    lines = text.splitlines()
    assert lines[2:5] == [
        "Вес ингредиентов: 500 г",
        "Готовый вес: 400 г",
        "Коэффициент: 1.250",
    ]
    assert lines[5].startswith("Пока учтено ккал/Б/Ж/У:")
    assert lines[6].startswith("⚠️ Расчёт неполный:")


def test_recipe_list_draft_labels_partial_totals_when_macros_are_missing() -> None:
    known = ResolvedRecipeListItem(
        requested_query="хлеб",
        grams=Decimal("100"),
        ingredient=Ingredient(
            id="known",
            recipe_id="",
            food_id="food-known",
            title="Хлеб",
            portion_id="0",
            amount=Decimal("1"),
            portion_description="100г",
        ),
        source="FatSecret",
        energy_per_100g=Decimal("250"),
        protein_per_100g=Decimal("8"),
        fat_per_100g=Decimal("3"),
        carbohydrate_per_100g=Decimal("48"),
    )
    missing = ResolvedRecipeListItem(
        requested_query="сыр",
        grams=Decimal("50"),
        ingredient=Ingredient(
            id="missing",
            recipe_id="",
            food_id="food-missing",
            title="Сыр Лёгкий",
            portion_id="0",
            amount=Decimal("0.5"),
            portion_description="100г",
        ),
        source="recipe-edit",
    )

    text = _format_recipe_list_draft("Сэндвич", [known, missing])

    assert "Пока учтено ккал/Б/Ж/У: 250/8/3/48" in text
    assert "Итого ккал/Б/Ж/У" not in text
    assert "⚠️ Расчёт неполный: нет полного КБЖУ: Сыр Лёгкий." in text
    assert "1. Хлеб" in text
    assert "2. Сыр Лёгкий" in text


def test_recipe_list_draft_labels_partial_when_only_one_macro_is_missing() -> None:
    item = ResolvedRecipeListItem(
        requested_query="сыр",
        grams=Decimal("100"),
        ingredient=Ingredient(
            id="cheese",
            recipe_id="",
            food_id="food-cheese",
            title="Сыр",
            portion_id="0",
            amount=Decimal("1"),
            portion_description="100г",
        ),
        source="FatSecret",
        energy_per_100g=Decimal("200"),
        protein_per_100g=Decimal("25"),
        fat_per_100g=Decimal("10"),
        carbohydrate_per_100g=None,
    )

    text = _format_recipe_list_draft("Тест", [item])

    assert "Пока учтено ккал/Б/Ж/У: 200/25/10/-" in text
    assert "⚠️ Расчёт неполный: нет полного КБЖУ: Сыр." in text
    assert "Итого ккал/Б/Ж/У" not in text


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
    rows = [[(button.text, button.callback_data) for button in row] for row in keyboard.inline_keyboard]
    flat_buttons = [text for row in rows for text, _ in row]

    assert "<b>Нужно заполнить или удалить</b>" in text
    assert "Пока учтено ккал/Б/Ж/У" in text
    assert "⚠️ Расчёт неполный" in text
    assert "2. ? Приправа для фарша Green | масса: 3г" in text
    assert any(button.startswith("🔎 2. Подобрать: Приправа") for button in flat_buttons)
    assert "➕ Создать продукт" in flat_buttons
    assert "🗑️ Убрать ингредиент" in flat_buttons
    assert "✅ Создать рецепт" not in flat_buttons
    assert rows[0][0][1] == "recipe_list_replace:0"
    assert rows[1][0][1] == "recipe_list_resolve:0"
    assert rows[2] == [("➕ Создать продукт", "recipe_list_create_food:0")]
    assert rows[3] == [("🗑️ Убрать ингредиент", "recipe_list_drop:0")]
    assert all(callback != "recipe_list_cancel:0" for row in rows for _, callback in row)


def test_recipe_list_draft_keyboard_keeps_every_object_and_action_on_a_wide_row() -> None:
    items = [
        ResolvedRecipeListItem(
            requested_query=f"ingredient {index}",
            grams=Decimal("100"),
            ingredient=Ingredient(
                id=f"i{index}",
                recipe_id="",
                food_id=f"f{index}",
                title=f"Очень длинное название ингредиента {index}",
                portion_id="0",
                amount=Decimal("1"),
                portion_description="100г",
            ),
            source="FatSecret",
        )
        for index in range(5)
    ]

    keyboard = _recipe_list_draft_keyboard(items)
    rows = keyboard.inline_keyboard

    assert [len(row) for row in rows] == [1] * len(rows)
    assert [button.callback_data for row in rows[:5] for button in row] == [
        f"recipe_list_replace:{index}" for index in range(5)
    ]
    assert all(len(button.text) <= 24 for row in rows[:5] for button in row)
    assert [button.text for button in rows[5]] == ["✏️ Изменить название"]
    assert [button.text for button in rows[6]] == ["✏️ Изменить шаги"]
    assert [(button.text, button.callback_data) for button in rows[-1]] == [
        ("✅ Создать", "recipe_list_confirm:0")
    ]


def test_custom_food_and_recipe_edit_confirmation_use_exact_ctas_without_inline_cancel() -> None:
    custom_rows = _custom_food_confirm_keyboard().inline_keyboard
    assert [[(button.text, button.callback_data) for button in row] for row in custom_rows] == [
        [("✅ Создать", "food_create:0")],
        [("✏️ Изменить название", "food_change_title:0")],
    ]

    edit_rows = _recipe_list_draft_keyboard(
        [
            ResolvedRecipeListItem(
                requested_query="ingredient",
                grams=Decimal("100"),
                ingredient=Ingredient(
                    id="i1",
                    recipe_id="",
                    food_id="f1",
                    title="Ингредиент",
                    portion_id="0",
                    amount=Decimal("1"),
                    portion_description="100г",
                ),
                source="FatSecret",
            )
        ],
        editing=True,
        edit_token="edit-token",
    ).inline_keyboard
    edit_buttons = [(button.text, button.callback_data) for row in edit_rows for button in row]
    assert ("✅ Сохранить", "recipe_edit_confirm:edit-token") in edit_buttons
    assert all(callback != "recipe_edit_cancel:edit-token" for _, callback in edit_buttons)


def test_recipe_list_draft_keyboard_labels_absent_and_present_cooked_weight() -> None:
    absent = _recipe_list_draft_keyboard([])
    present = _recipe_list_draft_keyboard([], cooked_weight_grams=Decimal("415"))

    absent_buttons = [button for row in absent.inline_keyboard for button in row]
    present_buttons = [button for row in present.inline_keyboard for button in row]
    assert ("⚖️ Готовый вес", "recipe_list_cooked_weight:0") in [
        (button.text, button.callback_data) for button in absent_buttons
    ]
    assert ("⚖️ Готовый вес: 415 г", "recipe_list_cooked_weight:0") in [
        (button.text, button.callback_data) for button in present_buttons
    ]
