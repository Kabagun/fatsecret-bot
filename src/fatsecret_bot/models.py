from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


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


@dataclass
class Recipe:
    id: str
    title: str
    description: str = ""
    portions: Decimal = Decimal("1")
    prep_time: int = 0
    cook_time: int = 0
    default_portion_id: str = "0"
    version: int = 1
    ingredients: list[Ingredient] = field(default_factory=list)
    remote_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class FoodSearchResult:
    food_id: str
    title: str
    description: str = ""
    default_portion_id: str = "0"
    energy_per_portion: Decimal | None = None
    carbohydrate_per_portion: Decimal | None = None
    protein_per_portion: Decimal | None = None
    fat_per_portion: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)
