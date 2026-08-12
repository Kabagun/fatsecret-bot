from __future__ import annotations

from decimal import Decimal


MAX_MACRO_TOTAL_PER_100G = Decimal("105")
MIN_ENERGY_MISMATCH_KCAL = Decimal("30")
MIN_ENERGY_RATIO = Decimal("0.5")
MAX_ENERGY_RATIO = Decimal("2")


def estimated_macro_energy(protein: Decimal, fat: Decimal, carbohydrate: Decimal) -> Decimal:
    """Estimate kcal from macros with the conventional 4/9/4 factors."""
    return protein * Decimal("4") + fat * Decimal("9") + carbohydrate * Decimal("4")


def custom_food_macro_error(
    calories: Decimal,
    protein: Decimal,
    fat: Decimal,
    carbohydrate: Decimal,
) -> str | None:
    """Describe only gross per-100g КБЖУ contradictions, allowing normal label variance."""
    macro_total = protein + fat + carbohydrate
    if macro_total > MAX_MACRO_TOTAL_PER_100G:
        return (
            "КБЖУ выглядят несогласованно: сумма белков, жиров и углеводов "
            f"равна {_format_decimal(macro_total)} г на 100 г. Проверь значения и их порядок: "
            "ккал, белки, жиры, углеводы."
        )

    estimated = estimated_macro_energy(protein, fat, carbohydrate)
    difference = abs(calories - estimated)
    if difference <= MIN_ENERGY_MISMATCH_KCAL:
        return None
    if estimated == 0:
        mismatched = calories > MIN_ENERGY_MISMATCH_KCAL
    else:
        mismatched = calories < estimated * MIN_ENERGY_RATIO or calories > estimated * MAX_ENERGY_RATIO
    if not mismatched:
        return None
    return (
        "КБЖУ выглядят несогласованно: по формуле 4×Б + 9×Ж + 4×У получается примерно "
        f"{_format_decimal(estimated)} ккал, но указано {_format_decimal(calories)} ккал. "
        "Проверь значения и их порядок: ккал, белки, жиры, углеводы."
    )


def _format_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.1")).normalize(), "f")
