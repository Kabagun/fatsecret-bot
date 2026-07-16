from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


PORTION_UNIT_RE = re.compile(
    r"^\s*(\d+(?:[\.,]\d+)?)\s*(?:г|гр|g|gram|грам|мл|ml)\b",
    re.IGNORECASE,
)
BARE_WEIGHT_UNITS = frozenset({"г", "гр", "g", "gram", "grams", "грам"})


def is_bare_weight_portion(description: str, *, allow_empty: bool = True) -> bool:
    """Return whether a portion description represents direct grams."""
    normalized = description.strip().casefold()
    return normalized in BARE_WEIGHT_UNITS or (allow_empty and not normalized)


def portion_unit_size(description: str) -> Decimal | None:
    """Extract an explicit gram or millilitre unit size from a portion description."""
    match = PORTION_UNIT_RE.search(description.replace("\xa0", " "))
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None


def is_explicit_weight_portion(description: str) -> bool:
    """Return whether a non-empty description explicitly represents a weight portion."""
    return is_bare_weight_portion(description, allow_empty=False) or portion_unit_size(description) is not None


def grams_from_portion(
    amount: Decimal,
    description: str,
    *,
    explicit_grams: Decimal | None = None,
    grams_per_portion: Decimal | None = None,
) -> Decimal | None:
    """Resolve grams using FatSecret's strongest available weight signal."""
    if explicit_grams is not None:
        return explicit_grams
    if grams_per_portion is not None and grams_per_portion > 0:
        return amount * grams_per_portion
    unit_size = portion_unit_size(description)
    if unit_size is not None and unit_size > 0:
        return amount * unit_size
    if is_bare_weight_portion(description):
        return amount
    return None
