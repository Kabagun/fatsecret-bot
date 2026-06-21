from __future__ import annotations

import datetime as dt
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from .models import (
    FatSecretAccountConfig,
    FatSecretDeviceConfig,
    FatSecretSession,
    FoodSearchResult,
    Ingredient,
    MAX_RECIPE_STEPS,
    Recipe,
    RecipeSummary,
)


class FatSecretError(RuntimeError):
    pass


logger = logging.getLogger(__name__)
FOOD_SEARCH_DATA_URL = "https://app.ftscrt.com/api/food/v1/search/data"
FOOD_SEARCH_PAGE_SIZE = 10
AUTH_RETRY_STATUS_CODES = {401, 403, 500}


def days_since_epoch(today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return str((today - dt.date(1970, 1, 1)).days)


def _decimal(text: str | None, default: Decimal | None = None) -> Decimal | None:
    if text is None or text == "":
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        return default


def _decimal_value(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return default


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    return normalized in {"1", "true", "yes", "y"}


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _int(text: str | None, default: int = 0) -> int:
    try:
        return int(Decimal(text or "0"))
    except (InvalidOperation, ValueError):
        return default


def _text(parent: ET.Element, tag: str, default: str = "") -> str:
    value = parent.findtext(tag)
    return value.strip() if value else default


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


_METADATA_SEPARATOR = "S#E{P<A*R*A>T}O!R"


def _metadata_value(value: Any, key: str) -> str:
    text = str(value or "").strip().replace("&lt;", "<").replace("&gt;", ">")
    if _METADATA_SEPARATOR not in text:
        return ""
    parts = text.split(_METADATA_SEPARATOR)
    normalized_key = key.casefold()
    for index, part in enumerate(parts[:-1]):
        if part.strip().casefold() == normalized_key:
            return parts[index + 1].strip()
    return ""


def _clean_food_text(value: Any) -> str:
    text = _strip_tags(str(value or ""))
    if "S#E{P<A*R*A>T}O" in text or "mtypeS#E" in text:
        return ""
    if text.casefold().startswith("includes:"):
        return ""
    return text


def _food_brand(data: dict[str, Any]) -> str:
    for key in (
        "brand",
        "brandName",
        "brand_name",
        "manufacturer",
        "manufacturerName",
        "manufacturername",
        "company",
        "owner",
    ):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("value")
        cleaned = _clean_food_text(value)
        if cleaned:
            return cleaned
    for value in data.values():
        brand = _metadata_value(value, "mname")
        if brand:
            return brand
    return ""


def _default_portion_description(data: dict[str, Any]) -> str:
    for key in (
        "defaultPortionDescription",
        "default_portion_description",
        "portionDescription",
        "portion_description",
        "servingSize",
        "serving_size",
    ):
        cleaned = _clean_food_text(data.get(key))
        if cleaned:
            return cleaned
    for value in data.values():
        description = _metadata_value(value, "ssize")
        if description:
            return description
    return ""


def _default_portion_id(data: dict[str, Any]) -> str:
    for key in ("defaultPortionID", "defaultPortionId", "default_portion_id", "defaultportionid"):
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "0"


def _form_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _scale_per_100g(value: Decimal | None, grams_per_portion: Decimal | None) -> Decimal | None:
    if value is None or grams_per_portion is None or grams_per_portion <= 0 or grams_per_portion == Decimal("100"):
        return value
    return value * Decimal("100") / grams_per_portion


def _bare_weight_portion_description(description: str) -> bool:
    normalized = description.strip().casefold()
    return normalized in {"г", "гр", "g", "gram", "grams", "грам", ""}


def _ingredient_portion_amount(ingredient: Ingredient) -> Decimal:
    if (ingredient.portion_id or "0") == "0" and _bare_weight_portion_description(ingredient.portion_description):
        return ingredient.amount / Decimal("100")
    return ingredient.amount


_STEP_TAG_RE = re.compile(r"^step(\d+)$")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _recipe_steps_from_xml(root: ET.Element) -> list[str]:
    numbered_steps: list[tuple[int, str]] = []
    for node in root.iter():
        match = _STEP_TAG_RE.fullmatch(_xml_local_name(node.tag))
        if match is None:
            continue
        step = (node.text or "").strip()
        if step:
            numbered_steps.append((int(match.group(1)), step))
    return [step for _, step in sorted(numbered_steps)[:MAX_RECIPE_STEPS]]


def _looks_like_true(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in {"true", "1", "ok", "yes"}:
        return True
    return normalized.isdigit() and int(normalized) > 0


def _should_retry_with_fresh_login(response: httpx.Response) -> bool:
    return response.status_code in AUTH_RETRY_STATUS_CODES or 300 <= response.status_code < 400


def parse_recipe_initial_save_response(text: str) -> str:
    normalized = text.strip()
    if normalized.upper().startswith("SUCCESS:"):
        remote_id = normalized.split(":", 1)[1].strip()
        if remote_id.isdigit():
            return remote_id
    if normalized.isdigit():
        return normalized
    if "невозможно сохранить" in normalized.casefold():
        raise FatSecretError(
            "FatSecret отклонил создание рецепта на первом шаге: "
            "«Невозможно сохранить». Чаще всего помогает другое имя рецепта или повтор позже."
        )
    raise FatSecretError(f"unexpected recipe create response: {normalized[:120]}")


def _stable_device_key(device_identifier: str, account_key: str) -> str:
    seed = uuid.uuid5(uuid.NAMESPACE_URL, f"fatsecret-bot:{account_key}:{device_identifier}")
    return f"|{device_identifier}|{seed.hex}"


class FatSecretClient:
    def __init__(
        self,
        account: FatSecretAccountConfig,
        device: FatSecretDeviceConfig,
        http: httpx.AsyncClient | None = None,
        session: FatSecretSession | None = None,
        session_saver: Callable[[FatSecretSession], None] | None = None,
    ) -> None:
        self.account = account
        self.device = device
        self._http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self._session: FatSecretSession | None = session
        self._session_from_cache = session is not None
        self._session_saver = session_saver

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def login(self) -> FatSecretSession:
        query = self._build_query(include_auth=False, include_build=True)
        url = f"https://app.ftscrt.com/api/authenticate/v1/fatsecret?{urlencode(query)}"
        device_key = _stable_device_key(self.device.device_identifier, self.account.key)
        headers = self._headers(content_type="application/json", device_key=device_key)
        payload = {
            "userName": self.account.username,
            "password": self.account.password,
            "deviceIdentifier": self.device.device_identifier,
        }
        response = await self._http.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            raise FatSecretError(f"{self.account.label}: login failed with HTTP {response.status_code}")
        data = response.json()
        try:
            self._session = FatSecretSession(
                server_id=str(data["serverId"]),
                device_key=str(data["deviceKey"]),
                secret_key=str(data["secretKey"]),
            )
            self._session_from_cache = False
            if self._session_saver is not None:
                self._session_saver(self._session)
        except KeyError as exc:
            raise FatSecretError(f"{self.account.label}: login response has no {exc.args[0]}") from exc
        return self._session

    async def ensure_logged_in(self) -> FatSecretSession:
        if self._session is None:
            return await self.login()
        return self._session

    async def cookbook(self) -> list[RecipeSummary]:
        response = await self._post_android("CookBookAndroidPage.aspx", {"fl": "4"})
        return self._parse_recipe_list(response.text)

    async def search_recipes(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        try:
            return await self.search_food(query, page=page)
        except (FatSecretError, ValueError, httpx.HTTPError):
            logger.debug("app food search failed for %s, falling back to legacy search", query, exc_info=True)
        return await self._search_recipes_legacy(query, page=page)

    async def search_food(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        """Search foods through the same JSON endpoint used by the FatSecret mobile app."""
        payload = {
            "searchExpression": query,
            "pageNumber": max(0, page),
            "pageSize": FOOD_SEARCH_PAGE_SIZE,
        }
        response = await self._post_app_json(FOOD_SEARCH_DATA_URL, payload, "food search")
        try:
            data = response.json()
        except ValueError as exc:
            raise FatSecretError(f"{self.account.label}: invalid food search JSON") from exc
        return self._parse_food_search_data(data)

    async def _search_recipes_legacy(self, query: str, page: int = 0) -> list[FoodSearchResult]:
        response = await self._post_android("RecipeSearch.aspx", {"fl": "2", "q": query, "pg": str(page)})
        summaries = self._parse_recipe_list(response.text)
        results: list[FoodSearchResult] = []
        for item in summaries:
            results.append(
                FoodSearchResult(
                    food_id=item.remote_id,
                    title=item.title,
                    description=item.description,
                    brand=item.brand,
                    default_portion_id=item.default_portion_id,
                    default_portion_description=item.default_portion_description,
                    energy_per_portion=item.energy_per_portion,
                    carbohydrate_per_portion=item.carbohydrate_per_portion,
                    protein_per_portion=item.protein_per_portion,
                    fat_per_portion=item.fat_per_portion,
                    raw={"_source_endpoint": "legacy_recipe_search"},
                )
            )
        return results

    async def autocomplete_food(self, query: str) -> list[FoodSearchResult]:
        session = await self.ensure_logged_in()
        params = {
            "mobile": "true",
            "c_id": session.server_id,
            "c_fl": "1",
            "c_s": session.secret_key,
            "c_d": session.device_key,
            "la": self.account.language,
            "ma": self.account.market,
            "query": query,
        }
        response = await self._http.get("https://auto.fatsecret.com/", params=params, headers=self._headers())
        if response.status_code != 200:
            raise FatSecretError(f"{self.account.label}: autocomplete failed with HTTP {response.status_code}")
        data = response.json()
        return self._parse_autocomplete(data)

    async def get_recipe(self, remote_id: str) -> Recipe:
        response = await self._post_android(
            "RecipeAndroidPage.aspx",
            {"rid": remote_id, "images": "true", "fl": "7"},
        )
        return self._parse_recipe(response.text)

    async def create_recipe(self, recipe: Recipe) -> str:
        form = {
            "action": "recipeinitialsave",
            "prid": "0",
            "title": recipe.title,
            "description": recipe.description,
            "portions": str(recipe.portions),
            "preptime": str(recipe.prep_time),
            "cooktime": str(recipe.cook_time),
            "fl": "7",
        }
        response = await self._post_android("RecipeActionAndroidPage.aspx", form)
        return parse_recipe_initial_save_response(response.text)

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        form = {
            "action": "recipesave",
            "prid": remote_id,
            "title": recipe.title,
            "description": recipe.description,
            "portions": str(recipe.portions),
            "preptime": str(recipe.prep_time),
            "cooktime": str(recipe.cook_time),
            "osharing": "false",
            "fl": "7",
        }
        for index, step in enumerate((step.strip() for step in recipe.steps if step.strip()), start=1):
            if index > MAX_RECIPE_STEPS:
                break
            form[f"step{index}"] = step
        response = await self._post_android("RecipeActionAndroidPage.aspx", form)
        return _looks_like_true(response.text)

    async def add_ingredient(self, remote_recipe_id: str, ingredient: Ingredient) -> bool:
        form = {
            "action": "ingredientsave",
            "fl": "5",
            "prid": remote_recipe_id,
            "rid": ingredient.food_id,
            "iid": ingredient.remote_ingredient_id or "0",
            "entryname": ingredient.title,
            "portionid": ingredient.portion_id or "0",
            "portionamount": _form_decimal(_ingredient_portion_amount(ingredient)),
        }
        response = await self._post_android("RecipeActionAndroidPage.aspx", form)
        return _looks_like_true(response.text)

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        """Delete a recipe from the current account's FatSecret cookbook."""
        form = {
            "action": "recipedelete",
            "fl": "5",
            "rid": remote_recipe_id,
        }
        response = await self._post_android("RecipeActionAndroidPage.aspx", form)
        return _looks_like_true(response.text)

    async def _post_android(self, page: str, fields: dict[str, str]) -> httpx.Response:
        session = await self.ensure_logged_in()
        common = self._common_form(session)
        common.update(fields)
        url = f"https://android.fatsecret.com/android/{page}"
        response = await self._http.post(url, data=common, headers=self._headers("application/x-www-form-urlencoded"))
        if _should_retry_with_fresh_login(response) and self._session_from_cache:
            self._session = None
            self._session_from_cache = False
            session = await self.login()
            common = self._common_form(session)
            common.update(fields)
            response = await self._http.post(url, data=common, headers=self._headers("application/x-www-form-urlencoded"))
        if response.status_code != 200:
            raise FatSecretError(f"{self.account.label}: {page} failed with HTTP {response.status_code}")
        return response

    async def _post_app_json(self, url: str, payload: dict[str, Any], label: str) -> httpx.Response:
        await self.ensure_logged_in()
        query = self._build_query(include_auth=True, include_build=True)
        full_url = f"{url}?{urlencode(query)}"
        response = await self._http.post(full_url, json=payload, headers=self._headers("application/json"))
        if _should_retry_with_fresh_login(response) and self._session_from_cache:
            self._session = None
            self._session_from_cache = False
            await self.login()
            query = self._build_query(include_auth=True, include_build=True)
            full_url = f"{url}?{urlencode(query)}"
            response = await self._http.post(full_url, json=payload, headers=self._headers("application/json"))
        if response.status_code != 200:
            raise FatSecretError(f"{self.account.label}: {label} failed with HTTP {response.status_code}")
        return response

    def _common_form(self, session: FatSecretSession) -> dict[str, str]:
        return {
            "c_id": session.server_id,
            "c_fl": "1",
            "c_s": session.secret_key,
            "c_d": session.device_key,
            "dt": days_since_epoch(),
            "app_version": self.device.app_version,
            "lang": self.account.language,
            "mkt": self.account.market,
            "device": self.device.device,
        }

    def _build_query(self, include_auth: bool, include_build: bool) -> dict[str, str]:
        query = {
            "dt": days_since_epoch(),
            "app_version": self.device.app_version,
            "lang": self.account.language,
            "mkt": self.account.market,
            "device": self.device.device,
        }
        if include_auth and self._session:
            query.update(
                {
                    "c_fl": "1",
                    "c_id": self._session.server_id,
                    "c_s": self._session.secret_key,
                    "c_d": self._session.device_key,
                }
            )
        if include_build:
            query.update(
                {
                    "build_sdk": self.device.build_sdk,
                    "build_api": self.device.build_api,
                    "build_model": self.device.build_model,
                    "build_resolution": self.device.build_resolution,
                }
            )
        return query

    def _headers(self, content_type: str | None = None, device_key: str | None = None) -> dict[str, str]:
        headers = {"User-Agent": self.device.user_agent}
        if content_type:
            headers["Content-Type"] = content_type
        if self.device.authorization:
            headers["Authorization"] = self.device.authorization
        if device_key:
            headers["c_d"] = device_key
        elif self._session:
            headers["c_d"] = self._session.device_key
        if self.device.c_desc:
            headers["c_desc"] = self.device.c_desc
        return headers

    def _parse_recipe_list(self, xml_text: str) -> list[RecipeSummary]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise FatSecretError(f"{self.account.label}: invalid recipe list XML") from exc

        recipes: list[RecipeSummary] = []
        for node in root.findall(".//recipe"):
            remote_id = _text(node, "id")
            title = _text(node, "title")
            if not remote_id or not title:
                continue
            short_description = _text(node, "shortDescription")
            recipes.append(
                RecipeSummary(
                    remote_id=remote_id,
                    title=title,
                    description=_clean_food_text(_text(node, "description") or short_description),
                    brand=(
                        _clean_food_text(_text(node, "brand"))
                        or _clean_food_text(_text(node, "brandName"))
                        or _clean_food_text(_text(node, "brand_name"))
                        or _clean_food_text(_text(node, "manufacturer"))
                        or _clean_food_text(_text(node, "manufacturerName"))
                        or _metadata_value(short_description, "mname")
                    ),
                    default_portion_id=_text(node, "defaultPortionID", "0"),
                    default_portion_description=(
                        _text(node, "defaultPortionDescription") or _metadata_value(short_description, "ssize")
                    ),
                    energy_per_portion=_decimal(_text(node, "energyPerPortion"), None),
                    carbohydrate_per_portion=_decimal(_text(node, "carbohydratePerPortion"), None),
                    protein_per_portion=_decimal(_text(node, "proteinPerPortion"), None),
                    fat_per_portion=_decimal(_text(node, "fatPerPortion"), None),
                )
            )
        return recipes

    def _parse_recipe(self, xml_text: str) -> Recipe:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise FatSecretError(f"{self.account.label}: invalid recipe XML") from exc

        remote_id = _text(root, "id")
        title = _text(root, "title")
        recipe = Recipe(
            id=remote_id,
            title=title,
            description=_text(root, "shortDescription"),
            portions=_decimal(_text(root, "servings"), Decimal("1")) or Decimal("1"),
            prep_time=_int(_text(root, "preparationtimemin")),
            cook_time=_int(_text(root, "cookingtimemin")),
            steps=_recipe_steps_from_xml(root),
            default_portion_id=_text(root, "defaultPortionID", "0"),
            default_portion_description=_text(root, "defaultPortionDescription"),
        )
        recipe.ingredients = []
        for node in root.findall(".//recipeingredient"):
            food_id = _text(node, "associatedrecipeid")
            name = _text(node, "name") or _text(node, "title")
            if not food_id or not name:
                continue
            recipe.ingredients.append(
                Ingredient(
                    id=_text(node, "id") or str(uuid.uuid4()),
                    recipe_id=remote_id,
                    food_id=food_id,
                    title=name,
                    portion_id=_text(node, "portionid", "0"),
                    amount=_decimal(_text(node, "portionamount"), Decimal("0")) or Decimal("0"),
                    portion_description=_text(node, "portiondescription"),
                    remote_ingredient_id=_text(node, "id") or None,
                )
            )
        return recipe

    def _parse_food_search_data(self, data: Any) -> list[FoodSearchResult]:
        if not isinstance(data, dict):
            return []
        raw_summaries = data.get("summaries") or data.get("summary") or data.get("items") or []
        if isinstance(raw_summaries, dict):
            raw_summaries = raw_summaries.get("summary") or raw_summaries.get("items") or []
        if not isinstance(raw_summaries, list):
            return []

        root_brand = _clean_food_text(data.get("manufacturername") or data.get("manufacturerName") or "")
        results: list[FoodSearchResult] = []
        for item in raw_summaries:
            if not isinstance(item, dict):
                continue
            raw_title = _first_present(item, "title", "name", "foodName", "food_name")
            title = _clean_food_text(raw_title)
            raw_id = _first_present(item, "id", "foodId", "food_id", "recipeId")
            if not raw_id or not title:
                continue

            grams_per_portion = _decimal_value(
                _first_present(item, "gramsPerPortion", "grams_per_portion", "gramsperportion"),
                None,
            )
            serving_amount = _decimal_value(_first_present(item, "servingAmount", "serving_amount"), None)
            serving_unit = _clean_food_text(_first_present(item, "servingAmountUnit", "serving_amount_unit") or "")
            portion_description = _default_portion_description(item)
            if not portion_description and serving_amount is not None and serving_unit:
                portion_description = f"{_form_decimal(serving_amount)}{serving_unit}"
            if not portion_description and grams_per_portion is not None:
                portion_description = f"{_form_decimal(grams_per_portion)}г"

            energy = _decimal_value(_first_present(item, "energyPerPortion", "energy", "calories"), None)
            carbohydrate = _decimal_value(
                _first_present(item, "carbohydratePerPortion", "carbohydrate", "carbs"),
                None,
            )
            protein = _decimal_value(_first_present(item, "proteinPerPortion", "protein"), None)
            fat = _decimal_value(_first_present(item, "fatPerPortion", "fat"), None)
            raw = dict(item)
            raw["_source_endpoint"] = "food_search_data"
            if root_brand:
                raw["_rootManufacturerName"] = root_brand

            results.append(
                FoodSearchResult(
                    food_id=str(raw_id),
                    title=title,
                    description=_clean_food_text(
                        item.get("shortDescription") or item.get("description") or item.get("pathName") or ""
                    ),
                    brand=_food_brand(item) or root_brand,
                    default_portion_id=_default_portion_id(item),
                    default_portion_description=portion_description,
                    source=str(item.get("source") or ""),
                    is_own=_bool_value(_first_present(item, "isOwn", "is_own", "isown")),
                    grams_per_portion=grams_per_portion,
                    energy_per_portion=_scale_per_100g(energy, grams_per_portion),
                    carbohydrate_per_portion=_scale_per_100g(carbohydrate, grams_per_portion),
                    protein_per_portion=_scale_per_100g(protein, grams_per_portion),
                    fat_per_portion=_scale_per_100g(fat, grams_per_portion),
                    raw=raw,
                )
            )
        return results

    def _parse_autocomplete(self, data: Any) -> list[FoodSearchResult]:
        suggestions = data.get("suggestions", []) if isinstance(data, dict) else []
        results: list[FoodSearchResult] = []
        for item in suggestions:
            if isinstance(item, str):
                results.append(FoodSearchResult(food_id="", title=item, raw={"value": item}))
                continue
            if not isinstance(item, dict):
                continue
            raw_title = _first_present(item, "title", "name", "value", "label")
            raw_id = _first_present(item, "id", "recipeId", "food_id", "foodId")
            grams_per_portion = _decimal_value(
                _first_present(item, "gramsPerPortion", "grams_per_portion", "gramsperportion"),
                None,
            )
            if raw_title:
                results.append(
                    FoodSearchResult(
                        food_id=str(raw_id or ""),
                        title=_strip_tags(str(raw_title)),
                        description=_clean_food_text(item.get("description") or item.get("subtitle") or ""),
                        brand=_food_brand(item),
                        default_portion_id=str(
                            item.get("defaultPortionID")
                            or item.get("defaultPortionId")
                            or item.get("default_portion_id")
                            or "0"
                        ),
                        default_portion_description=_default_portion_description(item),
                        energy_per_portion=_scale_per_100g(
                            _decimal_value(_first_present(item, "energyPerPortion", "energy", "calories", "kcal"), None),
                            grams_per_portion,
                        ),
                        carbohydrate_per_portion=_scale_per_100g(
                            _decimal_value(_first_present(item, "carbohydratePerPortion", "carbohydrate", "carbs"), None),
                            grams_per_portion,
                        ),
                        protein_per_portion=_scale_per_100g(
                            _decimal_value(_first_present(item, "proteinPerPortion", "protein"), None),
                            grams_per_portion,
                        ),
                        fat_per_portion=_scale_per_100g(
                            _decimal_value(_first_present(item, "fatPerPortion", "fat"), None),
                            grams_per_portion,
                        ),
                        source=str(item.get("source") or ""),
                        is_own=_bool_value(_first_present(item, "isOwn", "is_own", "isown")),
                        grams_per_portion=grams_per_portion,
                        raw=item,
                    )
                )
        return results

    async def resolve_food_detail(self, result: FoodSearchResult) -> FoodSearchResult:
        if not result.food_id:
            matches = await self.search_recipes(result.title, page=0)
            if not matches:
                raise FatSecretError(f"{self.account.label}: no recipe/food match for {result.title}")
            result = matches[0]
        if result.raw.get("_source_endpoint") == "food_search_data" or any(
            value is not None
            for value in (
                result.energy_per_portion,
                result.carbohydrate_per_portion,
                result.protein_per_portion,
                result.fat_per_portion,
            )
        ):
            return result
        recipe = await self.get_recipe(result.food_id)
        detail_brand = _metadata_value(recipe.description, "mname")
        detail_portion_description = recipe.default_portion_description or _metadata_value(recipe.description, "ssize")
        return FoodSearchResult(
            food_id=result.food_id,
            title=recipe.title or result.title,
            description=result.description,
            brand=result.brand or detail_brand,
            default_portion_id=(
                recipe.default_portion_id
                if recipe.default_portion_id and recipe.default_portion_id != "0"
                else result.default_portion_id or "0"
            ),
            default_portion_description=detail_portion_description or result.default_portion_description,
            source=result.source,
            is_own=result.is_own,
            grams_per_portion=result.grams_per_portion,
            energy_per_portion=result.energy_per_portion,
            carbohydrate_per_portion=result.carbohydrate_per_portion,
            protein_per_portion=result.protein_per_portion,
            fat_per_portion=result.fat_per_portion,
            raw=result.raw,
        )

    @staticmethod
    def dumps_safe_response(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)
