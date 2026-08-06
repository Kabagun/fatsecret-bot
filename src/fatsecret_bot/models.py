from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


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
class FoodDiaryEntry:
    """One food row recorded in a FatSecret diary."""

    entry_id: str
    recipe_id: str
    meal: int
    name: str
    recipe_source: str
    recipe_portion_id: str
    portion_amount: Decimal
    serving_description: str = ""


@dataclass(frozen=True)
class FoodDiaryDay:
    """Authoritative FatSecret diary snapshot for one calendar date."""

    date: dt.date
    guid: str
    entries: list[FoodDiaryEntry]


@dataclass(frozen=True)
class FoodDiaryWriteEntry:
    """Food row prepared for the mobile bulk diary endpoint."""

    reference: str
    recipe_id: str
    name: str
    recipe_portion_id: str
    portion_amount: Decimal
    meal: int
    serving_description: str = ""


@dataclass(frozen=True)
class FoodDiaryBulkResult:
    """Result returned by one FatSecret bulk diary update."""

    inserted_entries: dict[str, str]
    failed_entries: dict[str, str]
    previous_guid: str = ""
    new_guid: str = ""


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


@dataclass(frozen=True)
class DiaryCopyPreview:
    """Preview of a diary copy run before the user confirms writes."""

    run_id: str
    source_account_key: str
    source_date: dt.date
    target_start: dt.date
    target_end: dt.date
    source_entries: list[FoodDiaryEntry]
    target_operations: int
    skipped_source_day: bool


@dataclass(frozen=True)
class DiaryCopyDateResult:
    """Outcome for one target account and one target date."""

    account_key: str
    date: dt.date
    inserted: int
    failed: int
    message: str


@dataclass(frozen=True)
class DiaryCopyResult:
    """Persisted result of a confirmed diary copy run."""

    run_id: str
    status: str
    dates: list[DiaryCopyDateResult]
