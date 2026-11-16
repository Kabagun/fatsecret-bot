from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .portions import portion_unit_size


MAX_RECIPE_STEPS = 100


@dataclass(frozen=True)
class FatSecretAccountConfig:
    key: str
    label: str
    username: str
    password: str
    market: str
    language: str


@dataclass(frozen=True)
class FatSecretDeviceConfig:
    app_version: str
    device: str
    build_sdk: str
    build_api: str
    build_model: str
    build_resolution: str
    device_identifier: str
    authorization: str = ""
    c_desc: str = ""
    user_agent: str = "FatSecretBot/0.1"


@dataclass
class FatSecretSession:
    server_id: str
    device_key: str
    secret_key: str


@dataclass
class RecipeSummary:
    remote_id: str
    title: str
    description: str = ""
    brand: str = ""
    default_portion_id: str = "0"
    default_portion_description: str = ""
    energy_per_portion: Decimal | None = None
    carbohydrate_per_portion: Decimal | None = None
    protein_per_portion: Decimal | None = None
    fat_per_portion: Decimal | None = None


@dataclass
class Ingredient:
    id: str
    recipe_id: str
    food_id: str
    title: str
    portion_id: str
    amount: Decimal
    portion_description: str = ""
    remote_ingredient_id: str | None = None
    grams: Decimal | None = None


@dataclass
class Recipe:
    id: str
    title: str
    description: str = ""
    portions: Decimal = Decimal("1")
    prep_time: int = 0
    cook_time: int = 0
    steps: list[str] = field(default_factory=list)
    default_portion_id: str = "0"
    default_portion_description: str = ""
    version: int = 1
    group_id: str | None = None
    ingredients: list[Ingredient] = field(default_factory=list)
    remote_ids: dict[str, str] = field(default_factory=dict)
    remote_ids_by_account: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RecipeFingerprint:
    """Canonical account-specific recipe content used for verified synchronization."""

    digest: str
    canonical_json: str


@dataclass(frozen=True)
class RemoteRecipeVariant:
    """One fully hydrated recipe version owned by one FatSecret account."""

    account_key: str
    remote_recipe_id: str
    recipe: Recipe
    fingerprint: RecipeFingerprint
    strict_fingerprint: RecipeFingerprint | None = None


@dataclass(frozen=True)
class RecipeConflict:
    """A normalized recipe title whose account-specific contents differ."""

    title: str
    variants: list[RemoteRecipeVariant]


@dataclass(frozen=True)
class RecipeGroup:
    id: str
    name: str
    invite_code: str


@dataclass(frozen=True)
class RecipeGroupMember:
    telegram_id: int
    display_name: str
    fatsecret_label: str | None = None
    fatsecret_username: str | None = None


@dataclass(frozen=True)
class CachedFoodUsage:
    group_id: str
    food_id: str
    title: str
    portion_id: str = "0"
    portion_description: str = ""
    use_count: int = 0


@dataclass
class FoodSearchResult:
    food_id: str
    title: str
    description: str = ""
    brand: str = ""
    default_portion_id: str = "0"
    default_portion_description: str = ""
    source: str = ""
    is_own: bool = False
    grams_per_portion: Decimal | None = None
    energy_per_portion: Decimal | None = None
    carbohydrate_per_portion: Decimal | None = None
    protein_per_portion: Decimal | None = None
    fat_per_portion: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CustomFoodDefinition:
    """Portable definition of a user-created FatSecret food."""

    source_recipe_id: str
    title: str
    manufacturer_name: str
    serving_type: str
    serving_size: str
    metric_serving_size: str
    nutrients: dict[str, Decimal]
    barcode: str = ""
    barcode_type: str = ""

    @property
    def nutrition_grams(self) -> Decimal | None:
        """Return the gram amount the entered nutrients describe, when known."""
        if self.serving_type == "Per100g":
            return Decimal("100")
        return portion_unit_size(self.metric_serving_size)

    def nutrients_per_100g(self) -> dict[str, Decimal]:
        """Normalize known serving weights without inventing grams for a serving."""
        grams = self.nutrition_grams
        if grams is None or not grams.is_finite() or grams <= 0:
            raise ValueError("Для пересчёта в граммы нужен вес одной порции.")
        return {key: value * Decimal("100") / grams for key, value in self.nutrients.items()}


@dataclass(frozen=True)
class BarcodeLookupResult:
    """FatSecret's current mapping for one decoded retail barcode."""

    barcode: str
    food_id: str | None = None
    barcode_id: str | None = None
    food_name: str = ""
    brand_name: str = ""
    should_prompt: bool = False

    @property
    def found(self) -> bool:
        """Return whether FatSecret mapped this barcode to an existing food."""
        return bool(self.food_id)
