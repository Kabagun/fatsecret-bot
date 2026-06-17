from __future__ import annotations

import datetime as dt
import json
import re
import uuid
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx

from .models import (
    FatSecretAccountConfig,
    FatSecretDeviceConfig,
    FatSecretSession,
    FoodSearchResult,
    Ingredient,
    Recipe,
    RecipeSummary,
)


class FatSecretError(RuntimeError):
    pass


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


def _clean_food_text(value: Any) -> str:
    text = _strip_tags(str(value or ""))
    if "S#E{P<A*R*A>T}O" in text or "mtypeS#E" in text:
        return ""
    if text.casefold().startswith("includes:"):
        return ""
    return text


def _food_brand(data: dict[str, Any]) -> str:
    for key in ("brand", "brandName", "brand_name", "manufacturer", "manufacturerName", "company", "owner"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("value")
        cleaned = _clean_food_text(value)
        if cleaned:
            return cleaned
    return ""


def _looks_like_true(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in {"true", "1", "ok", "yes"}:
        return True
    return normalized.isdigit() and int(normalized) > 0


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
    ) -> None:
        self.account = account
        self.device = device
        self._http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self._session: FatSecretSession | None = None

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
                    energy_per_portion=item.energy_per_portion,
                    carbohydrate_per_portion=item.carbohydrate_per_portion,
                    protein_per_portion=item.protein_per_portion,
                    fat_per_portion=item.fat_per_portion,
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
            "step1": "",
            "step2": "",
            "step3": "",
            "fl": "7",
        }
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
            "portionamount": str(ingredient.amount),
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
        if response.status_code != 200:
            raise FatSecretError(f"{self.account.label}: {page} failed with HTTP {response.status_code}")
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
            recipes.append(
                RecipeSummary(
                    remote_id=remote_id,
                    title=title,
                    description=_clean_food_text(_text(node, "description") or _text(node, "shortDescription")),
                    brand=(
                        _clean_food_text(_text(node, "brand"))
                        or _clean_food_text(_text(node, "brandName"))
                        or _clean_food_text(_text(node, "brand_name"))
                        or _clean_food_text(_text(node, "manufacturer"))
                        or _clean_food_text(_text(node, "manufacturerName"))
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
            default_portion_id=_text(root, "defaultPortionID", "0"),
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

    def _parse_autocomplete(self, data: Any) -> list[FoodSearchResult]:
        suggestions = data.get("suggestions", []) if isinstance(data, dict) else []
        results: list[FoodSearchResult] = []
        for item in suggestions:
            if isinstance(item, str):
                results.append(FoodSearchResult(food_id="", title=item, raw={"value": item}))
                continue
            if not isinstance(item, dict):
                continue
            raw_title = item.get("title") or item.get("name") or item.get("value") or item.get("label")
            raw_id = item.get("id") or item.get("recipeId") or item.get("food_id") or item.get("foodId")
            if raw_title:
                results.append(
                    FoodSearchResult(
                        food_id=str(raw_id or ""),
                        title=_strip_tags(str(raw_title)),
                        description=_clean_food_text(item.get("description") or item.get("subtitle") or ""),
                        brand=_food_brand(item),
                        energy_per_portion=_decimal(
                            str(
                                item.get("energyPerPortion")
                                or item.get("energy")
                                or item.get("calories")
                                or item.get("kcal")
                                or ""
                            ),
                            None,
                        ),
                        carbohydrate_per_portion=_decimal(
                            str(item.get("carbohydratePerPortion") or item.get("carbohydrate") or item.get("carbs") or ""),
                            None,
                        ),
                        protein_per_portion=_decimal(
                            str(item.get("proteinPerPortion") or item.get("protein") or ""),
                            None,
                        ),
                        fat_per_portion=_decimal(
                            str(item.get("fatPerPortion") or item.get("fat") or ""),
                            None,
                        ),
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
        recipe = await self.get_recipe(result.food_id)
        return FoodSearchResult(
            food_id=result.food_id,
            title=recipe.title or result.title,
            description=result.description,
            brand=result.brand,
            default_portion_id=recipe.default_portion_id or "0",
            energy_per_portion=result.energy_per_portion,
            carbohydrate_per_portion=result.carbohydrate_per_portion,
            protein_per_portion=result.protein_per_portion,
            fat_per_portion=result.fat_per_portion,
            raw=result.raw,
        )

    @staticmethod
    def dumps_safe_response(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)
