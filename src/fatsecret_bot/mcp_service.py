"""Group-scoped chat workflows over the bot's existing, durable recipe engine."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .barcodes import normalize_barcode
from .fatsecret_client import FatSecretError
from .models import CustomFoodDefinition, Ingredient, MAX_RECIPE_STEPS, Recipe
from .portions import grams_from_portion
from .recipe_compare import recipe_fingerprint
from .recipe_text import recipe_export_payload
from .storage import Storage, normalize_title
from .sync import (
    RecipeSyncEngine, ResolvedRecipeListItem, recipe_cooked_weight_from_description,
)


class WorkflowError(ValueError):
    """A safe, actionable error that may be returned to a chat client."""


class IngredientInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    grams: Decimal = Field(gt=0, le=100000, allow_inf_nan=False)
    candidate_ref: str | None = None


def _number(value: object, label: str, *, zero: bool = False, maximum: str = "100000") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WorkflowError(f"{label}: нужно число.") from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0) or result > Decimal(maximum):
        raise WorkflowError(f"{label}: значение должно быть {'не меньше' if zero else 'больше'} 0 и не больше {maximum}.")
    return result


def _text(value: str, label: str, maximum: int = 200) -> str:
    result = value.strip()
    if not result or len(result) > maximum:
        raise WorkflowError(f"{label}: укажи от 1 до {maximum} символов.")
    return result


def _steps(values: list[str]) -> list[str]:
    if len(values) > MAX_RECIPE_STEPS:
        raise WorkflowError(f"Допускается не больше {MAX_RECIPE_STEPS} шагов.")
    return [_text(value, "Шаг приготовления", 4000) for value in values]


def _json(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def _resolved(data: dict) -> ResolvedRecipeListItem:
    values = dict(data)
    ingredient = dict(values.pop("ingredient"))
    ingredient["amount"] = Decimal(ingredient["amount"])
    if ingredient.get("grams") is not None:
        ingredient["grams"] = Decimal(ingredient["grams"])
    for key in ("grams", "energy_per_100g", "protein_per_100g", "fat_per_100g", "carbohydrate_per_100g"):
        if values.get(key) is not None:
            values[key] = Decimal(values[key])
    return ResolvedRecipeListItem(ingredient=Ingredient(**ingredient), **values)


def _recipe(data: dict) -> Recipe:
    values = dict(data)
    values["portions"] = Decimal(values["portions"])
    ingredients = values.pop("ingredients")
    result = Recipe(**values)
    for value in ingredients:
        value = dict(value)
        value["amount"] = Decimal(value["amount"])
        if value.get("grams") is not None:
            value["grams"] = Decimal(value["grams"])
        result.ingredients.append(Ingredient(**value))
    return result


def _definition(data: dict) -> CustomFoodDefinition:
    values = dict(data)
    values["nutrients"] = {key: Decimal(value) for key, value in values["nutrients"].items()}
    return CustomFoodDefinition(**values)


class FatSecretMCPService:
    def __init__(self, storage: Storage, engine: RecipeSyncEngine, group_id: str) -> None:
        self.storage, self.engine, self.group_id = storage, engine, group_id
        self._lock = asyncio.Lock()
        self.storage._conn.execute("""
            CREATE TABLE IF NOT EXISTS mcp_artifacts (
                group_id TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL,
                revision INTEGER NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL,
                PRIMARY KEY (group_id, ref)
            )
        """)
        self.storage._conn.commit()
        self._group()

    def _group(self) -> dict:
        row = self.storage._conn.execute(
            "SELECT id, name, created_by FROM recipe_groups WHERE id = ?", (self.group_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError("Настроенная группа не найдена. Выбери существующую группу в настройках MCP.")
        return dict(row)

    def _accounts(self) -> dict[str, str]:
        self._group()
        return {a.key: a.label for a in self.storage.list_fatsecret_accounts(self.group_id)}

    def _save(self, kind: str, payload: dict, ref: str | None = None, *, revision: int = 1, commit: bool = True) -> str:
        ref = ref or uuid.uuid4().hex
        now = time.time()
        with self.storage._conn if commit else nullcontext():
            self.storage._conn.execute("DELETE FROM mcp_artifacts WHERE expires_at < ?", (now,))
            self.storage._conn.execute(
                "INSERT INTO mcp_artifacts VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, ref) DO UPDATE SET revision=excluded.revision, "
                "payload=excluded.payload, expires_at=excluded.expires_at",
                (self.group_id, ref, kind, revision, _json(payload), now + 7 * 86400),
            )
        return ref

    def _get(self, ref: str, kind: str) -> tuple[dict, int]:
        self._group()
        row = self.storage._conn.execute(
            "SELECT * FROM mcp_artifacts WHERE group_id=? AND ref=? AND kind=? AND expires_at>?",
            (self.group_id, ref, kind, time.time()),
        ).fetchone()
        if row is None:
            raise WorkflowError("Объект недоступен или устарел. Выполни поиск или открой черновик заново.")
        return json.loads(row["payload"]), int(row["revision"])

    @staticmethod
    def _pending(data: dict) -> None:
        if data.get("status") not in {"pending", None}:
            raise WorkflowError("Действие уже отправлено на выполнение. Открой результат или создай новый черновик.")

    def _action(self, kind: str, values: dict) -> dict:
        values.update(type=kind, status="pending", accounts=sorted(self._accounts()))
        if not values["accounts"]:
            raise WorkflowError("Сначала подключи аккаунт FatSecret в боте.")
        ref = self._save("action", values)
        return self._preview(ref, values, 1)

    def _candidate(self, item: ResolvedRecipeListItem) -> dict:
        ref = self._save("candidate", asdict(item))
        return {"candidate_ref": ref, "title": item.ingredient.title, "brand": item.brand,
                "grams": str(item.grams), "source": item.source,
                "nutrition_per_100g": {"calories": item.energy_per_100g, "protein": item.protein_per_100g,
                                      "fat": item.fat_per_100g, "carbohydrate": item.carbohydrate_per_100g}}

    def _selected(self, ref: str, grams: Decimal, query: str, ingredient_id: str) -> dict:
        item = _resolved(self._get(ref, "candidate")[0])
        ingredient = replace(item.ingredient, id=ingredient_id, amount=item.ingredient.amount * grams / item.grams, grams=grams)
        return asdict(replace(item, requested_query=query, grams=grams, ingredient=ingredient))

    async def get_group(self) -> dict:
        """Show the connected group and account labels, without credentials or member data."""
        accounts = self._accounts()
        return {"group": self._group()["name"], "accounts": list(accounts.values()),
                "account_count": len(accounts), "account_management": "Подключение и отключение аккаунтов — в Telegram-боте."}

    async def list_recipes(self, query: str = "", limit: int = 20, offset: int = 0) -> dict:
        """Read fresh group recipes; search by title. Keep recipe_ref internal to the chat."""
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000 or len(query) > 200:
            raise WorkflowError("Некорректные параметры поиска.")
        recipes = await self.engine.load_remote_recipe_index(self.group_id)
        tokens = normalize_title(query).split()
        recipes = [r for r in recipes if all(t in normalize_title(r.title) for t in tokens)]
        accounts = self._accounts()
        return {"total": len(recipes), "offset": offset, "recipes": [
            {"recipe_ref": self._save("recipe", asdict(r)), "title": r.title,
             "accounts": [accounts[k] for k in r.remote_ids if k in accounts]}
            for r in recipes[offset:offset + limit]
        ]}

    async def _live(self, recipe_ref: str) -> Recipe:
        previous = _recipe(self._get(recipe_ref, "recipe")[0])
        if previous.group_id != self.group_id:
            raise WorkflowError("Рецепт недоступен.")
        recipes = await self.engine.load_remote_recipe_index(self.group_id)
        current = next((r for r in recipes if r.id == previous.id), None)
        if current is None or current.remote_ids_by_account != previous.remote_ids_by_account:
            raise WorkflowError("Рецепт изменился или удалён. Обнови список рецептов.")
        return current

    async def get_recipe(self, recipe_ref: str) -> dict:
        """Read every current account version, ingredients, steps and export text."""
        recipe = await self._live(recipe_ref)
        variants = await self.engine.hydrate_live_recipe_variants(recipe)
        labels = self._accounts()
        result = []
        for v in variants:
            snapshot = {"recipe_ref": recipe_ref, "recipe": asdict(v.recipe), "account_key": v.account_key,
                        "remote_id": v.remote_recipe_id,
                        "digest": (v.strict_fingerprint or recipe_fingerprint(v.recipe)).digest}
            ref = self._save("variant", snapshot)
            ingredients = [{"title": i.title, "grams": i.grams or grams_from_portion(i.amount, i.portion_description),
                            "amount": i.amount, "portion": i.portion_description} for i in v.recipe.ingredients]
            export, export_error = None, None
            try:
                export = recipe_export_payload(v.recipe)
            except ValueError as exc:
                export_error = str(exc)
            result.append({"variant_ref": ref, "account": labels.get(v.account_key, "Аккаунт"),
                           "title": v.recipe.title, "description": v.recipe.description,
                           "portions": v.recipe.portions, "ingredients": ingredients, "steps": v.recipe.steps,
                           "cooked_weight_grams": recipe_cooked_weight_from_description(v.recipe.description),
                           "export_text": export, "export_error": export_error})
        return {"title": recipe.title, "variants": result, "choose_version": len({v.fingerprint.digest for v in variants}) > 1}

    async def search_foods(self, query: str, grams: Decimal = Decimal(100), limit: int = 6, offset: int = 0) -> dict:
        """Search foods in all group accounts. Present candidates and ask the user to choose; never pick silently."""
        query = _text(query, "Название продукта")
        grams = _number(grams, "Масса")
        if not 1 <= limit <= 20 or not 0 <= offset <= 100:
            raise WorkflowError("Некорректная страница поиска.")
        candidates = await self.engine.recipe_list_candidates(self.group_id, query, grams=grams, limit=limit, offset=offset)
        return {"query": query, "candidates": [self._candidate(i) for i in candidates],
                "next_step": "Предложи варианты пользователю. Если подходящего нет, уточни КБЖУ и создай личный продукт."}

    async def lookup_barcode(self, barcode: str, grams: Decimal = Decimal(100)) -> dict:
        """Look up a retail barcode; return a selectable existing product instead of duplicating it."""
        code = normalize_barcode(barcode)
        grams = _number(grams, "Масса")
        result = await self.engine.lookup_barcode(self.group_id, code.code)
        if not result.found:
            return {"found": False, "barcode": code.code}
        item = await self.engine.barcode_recipe_list_item(self.group_id, result, grams, result.food_name or "Продукт")
        return {"found": True, "candidate": self._candidate(item)}

    async def search_brands(self, query: str) -> dict:
        """Find canonical manufacturer names for a custom food; free-text brands are also supported."""
        return {"brands": await self.engine.suggest_custom_food_brands(self.group_id, _text(query, "Бренд"))}

    async def prepare_custom_food(
        self, title: str, calories: Decimal, protein: Decimal, fat: Decimal, carbohydrate: Decimal,
        basis: Literal["100g", "serving"] = "100g", brand: str = "", barcode: str = "",
        serving_name: str = "", serving_weight_grams: Decimal | None = None,
        recipe_draft_ref: str | None = None, ingredient_ref: str | None = None,
    ) -> dict:
        """Preview a personal food. Optionally bind it to a recipe ingredient after approval and creation.

        Ask for missing label nutrition; never invent it. For a recipe, serving-based nutrition needs a known weight.
        No FatSecret mutation happens here. Confirm this action only after the user approves this exact preview.
        """
        if basis not in {"100g", "serving"}:
            raise WorkflowError("Выбери КБЖУ на 100 г или на порцию.")
        weight = _number(serving_weight_grams, "Вес порции") if serving_weight_grams is not None else None
        if basis == "serving":
            serving_name = _text(serving_name, "Название порции")
        if bool(recipe_draft_ref) != bool(ingredient_ref):
            raise WorkflowError("Для ингредиента нужен его черновик рецепта.")
        if recipe_draft_ref:
            draft, draft_revision = self._get(recipe_draft_ref, "action")
            self._pending(draft)
            self._entry(draft, ingredient_ref)
            if basis == "serving" and weight is None:
                raise WorkflowError("Чтобы добавить продукт в рецепт, уточни вес одной порции в граммах.")
        decoded = normalize_barcode(barcode) if barcode else None
        if decoded:
            existing = await self.lookup_barcode(decoded.code)
            if existing["found"]:
                return {"existing_product": existing["candidate"], "needs_confirmation": False,
                        "message": "Этот штрих-код уже есть в FatSecret. Предложи существующий продукт."}
        definition = CustomFoodDefinition(
            source_recipe_id="0", title=_text(title, "Название продукта"), manufacturer_name=brand.strip(),
            serving_type="Per100g" if basis == "100g" else "PerServing",
            serving_size="" if basis == "100g" else serving_name,
            metric_serving_size="100g" if basis == "100g" else (f"{weight}g" if weight is not None else ""),
            nutrients={"calories": _number(calories, "Ккал", zero=True, maximum="10000"),
                       "protein": _number(protein, "Белки", zero=True, maximum="1000"),
                       "totalFat": _number(fat, "Жиры", zero=True, maximum="1000"),
                       "carbohydrate": _number(carbohydrate, "Углеводы", zero=True, maximum="1000")},
            barcode=decoded.code if decoded else "", barcode_type=decoded.barcode_type if decoded else "",
        )
        self.engine._validate_custom_food_definition(definition)
        return self._action("food", {"definition": asdict(definition), "recipe_draft_ref": recipe_draft_ref,
                                     "ingredient_ref": ingredient_ref,
                                     "recipe_draft_revision": draft_revision if recipe_draft_ref else None})

    async def prepare_recipe(
        self, title: str | None = None, ingredients: list[IngredientInput] | None = None,
        portions: Decimal | None = None, steps: list[str] | None = None,
        cooked_weight_grams: Decimal | None = None, source_variant_ref: str | None = None,
    ) -> dict:
        """Start a durable recipe draft, or edit an exact version from get_recipe.

        New ingredient queries return candidates for user approval. For edits, omitted fields retain existing values.
        Use set_recipe_ingredient to add/change/select an ingredient. Technical food and account IDs stay server-side.
        """
        source = None
        entries = []
        if source_variant_ref:
            source, _ = self._get(source_variant_ref, "variant")
            recipe = _recipe(source["recipe"])
            if title is not None and title.strip() != recipe.title:
                raise WorkflowError("Название существующего рецепта меняется отдельным действием rename.")
            title = recipe.title
            portions = recipe.portions if portions is None else portions
            steps = recipe.steps if steps is None else steps
            if cooked_weight_grams is None:
                cooked_weight_grams = recipe_cooked_weight_from_description(recipe.description)
            if ingredients is None:
                for item in recipe.ingredients:
                    grams = item.grams if item.grams is not None else grams_from_portion(item.amount, item.portion_description)
                    if grams is None or grams <= 0:
                        raise WorkflowError("У ингредиента нет известного веса. Передай новый список ингредиентов с массой в граммах.")
                    mappings = {source["account_key"]: item.food_id}
                    for account in self._accounts():
                        mapped = self.storage.custom_food_mapping(source["account_key"], item.food_id, account)
                        if mapped:
                            mappings[account] = mapped
                    resolved = ResolvedRecipeListItem(item.title, grams, item, "recipe-edit",
                                                       custom_food_ids=mappings if len(mappings) > 1 else {})
                    entries.append({"ref": item.id, "query": item.title, "grams": str(grams), "selected": asdict(resolved), "candidates": []})
                hydrated = await self.engine.hydrate_recipe_edit_source_items(
                    self.group_id, source["account_key"], [_resolved(e["selected"]) for e in entries])
                for entry, item in zip(entries, hydrated, strict=True):
                    entry["selected"] = asdict(item)
        if ingredients is not None:
            if len(ingredients) > 100:
                raise WorkflowError("В рецепте допускается не больше 100 ингредиентов.")
            for item in ingredients:
                item = IngredientInput.model_validate(item)
                entry_ref = uuid.uuid4().hex
                query = _text(item.query, "Название ингредиента")
                entry = {"ref": entry_ref, "query": query, "grams": str(item.grams), "selected": None, "candidates": []}
                if item.candidate_ref:
                    entry["selected"] = self._selected(item.candidate_ref, item.grams, query, entry_ref)
                else:
                    entry["candidates"] = (await self.search_foods(query, item.grams))["candidates"]
                entries.append(entry)
        values = {"title": _text(title or "", "Название рецепта"), "entries": entries,
                  "portions": str(_number(portions if portions is not None else 1, "Порции", maximum="1000")),
                  "steps": _steps(steps or []), "source": source,
                  "cooked_weight_grams": str(_number(cooked_weight_grams, "Готовый вес")) if cooked_weight_grams is not None else None}
        return self._action("recipe", values)

    @staticmethod
    def _entry(data: dict, ref: str | None) -> dict:
        if data.get("type") != "recipe":
            raise WorkflowError("Это не черновик рецепта.")
        entry = next((e for e in data["entries"] if e["ref"] == ref), None)
        if entry is None:
            raise WorkflowError("Ингредиент не найден в этом черновике.")
        return entry

    async def set_recipe_ingredient(
        self, draft_ref: str, ingredient_ref: str | None = None, query: str | None = None,
        grams: Decimal | None = None, candidate_ref: str | None = None,
    ) -> dict:
        """Add/change an ingredient or apply a candidate approved by the user. Opaque references never need user input."""
        data, revision = self._get(draft_ref, "action")
        self._pending(data)
        if ingredient_ref:
            entry = self._entry(data, ingredient_ref)
        else:
            if data["type"] != "recipe" or len(data["entries"]) >= 100 or not query or grams is None:
                raise WorkflowError("Для нового ингредиента нужны название и масса; максимум 100 ингредиентов.")
            entry = {"ref": uuid.uuid4().hex, "query": "", "grams": "0", "selected": None, "candidates": []}
            data["entries"].append(entry)
        final_query = _text(query if query is not None else entry["query"], "Название ингредиента")
        final_grams = _number(grams if grams is not None else entry["grams"], "Масса")
        if candidate_ref:
            entry["selected"] = self._selected(candidate_ref, final_grams, final_query, entry["ref"])
            entry["candidates"] = []
        elif query is not None and final_query != entry["query"]:
            entry["selected"] = None
            entry["candidates"] = (await self.search_foods(final_query, final_grams))["candidates"]
        elif entry["selected"] is not None:
            old = _resolved(entry["selected"])
            entry["selected"] = asdict(replace(old, grams=final_grams, ingredient=replace(
                old.ingredient, grams=final_grams, amount=old.ingredient.amount * final_grams / old.grams)))
        entry.update(query=final_query, grams=str(final_grams))
        self._save("action", data, draft_ref, revision=revision + 1)
        return self._preview(draft_ref, data, revision + 1)

    async def remove_recipe_ingredient(self, draft_ref: str, ingredient_ref: str) -> dict:
        """Remove an ingredient from a local recipe draft."""
        data, revision = self._get(draft_ref, "action")
        self._pending(data)
        self._entry(data, ingredient_ref)
        data["entries"] = [e for e in data["entries"] if e["ref"] != ingredient_ref]
        self._save("action", data, draft_ref, revision=revision + 1)
        return self._preview(draft_ref, data, revision + 1)

    async def update_recipe_draft(
        self, draft_ref: str, title: str | None = None, portions: Decimal | None = None,
        steps: list[str] | None = None, cooked_weight_grams: Decimal | None = None,
        clear_cooked_weight: bool = False,
    ) -> dict:
        """Change recipe draft metadata. Show the updated preview before confirmation."""
        data, revision = self._get(draft_ref, "action")
        self._pending(data)
        if data["type"] != "recipe":
            raise WorkflowError("Это не черновик рецепта.")
        if title is not None:
            title = _text(title, "Название рецепта")
            if data.get("source") and title != data["title"]:
                raise WorkflowError("Для переименования существующего рецепта используй rename.")
            data["title"] = title
        if portions is not None:
            data["portions"] = str(_number(portions, "Порции", maximum="1000"))
        if steps is not None:
            data["steps"] = _steps(steps)
        if clear_cooked_weight and cooked_weight_grams is not None:
            raise WorkflowError("Нельзя одновременно указать и удалить готовый вес.")
        if cooked_weight_grams is not None:
            data["cooked_weight_grams"] = str(_number(cooked_weight_grams, "Готовый вес"))
        elif clear_cooked_weight:
            data["cooked_weight_grams"] = None
        self._save("action", data, draft_ref, revision=revision + 1)
        return self._preview(draft_ref, data, revision + 1)

    def _preview(self, ref: str, data: dict, revision: int) -> dict:
        result = {"action_ref": ref, "revision": revision, "action": data["type"], "status": data["status"],
                  "needs_confirmation": data["status"] != "completed", "accounts": [self._accounts().get(k, "Аккаунт") for k in data["accounts"]]}
        if data["type"] == "recipe":
            result.update(title=data["title"], portions=data["portions"], steps=data["steps"],
                          cooked_weight_grams=data["cooked_weight_grams"], editing=bool(data.get("source")))
            result["ingredients"] = [{"ingredient_ref": e["ref"], "query": e["query"], "grams": e["grams"],
                                      "selected": self._selected_summary(e["selected"]), "candidates": e["candidates"]} for e in data["entries"]]
            result["ready"] = bool(data["entries"]) and all(e["selected"] is not None for e in data["entries"])
        elif data["type"] == "food":
            definition = data["definition"]
            result.update(title=definition["title"], brand=definition["manufacturer_name"],
                          basis=definition["serving_type"], serving_name=definition["serving_size"],
                          serving_weight=definition["metric_serving_size"], nutrition=definition["nutrients"],
                          barcode=definition["barcode"], adds_to_recipe=bool(data.get("recipe_draft_ref")))
        else:
            result.update(titles=[r["recipe"]["title"] for r in data["recipes"]], new_title=data.get("title"))
            if data.get("source"):
                result["source_account"] = self._accounts().get(data["source"]["account_key"], "Аккаунт")
        if data.get("result") is not None:
            result["result"] = data["result"]
        return json.loads(_json(result))

    @staticmethod
    def _selected_summary(data: dict | None) -> dict | None:
        if data is None:
            return None
        return {"title": data["ingredient"]["title"], "brand": data["brand"], "grams": data["grams"],
                "nutrition_per_100g": {key: data[key] for key in ("energy_per_100g", "protein_per_100g", "fat_per_100g", "carbohydrate_per_100g")}}

    async def get_action(self, action_ref: str) -> dict:
        """Resume a persisted draft or read its result, including after a reconnect/restart."""
        data, revision = self._get(action_ref, "action")
        return self._preview(action_ref, data, revision)

    async def list_drafts(self) -> dict:
        """Find the group's unfinished chat drafts after reconnecting."""
        rows = self.storage._conn.execute(
            "SELECT ref, revision, payload FROM mcp_artifacts WHERE group_id=? AND kind='action' AND expires_at>? ORDER BY rowid DESC LIMIT 100",
            (self.group_id, time.time()),
        ).fetchall()
        return {"drafts": [self._preview(r["ref"], json.loads(r["payload"]), r["revision"])
                           for r in rows if json.loads(r["payload"])["status"] not in {"completed", "cancelled"}]}

    async def discard_action(self, action_ref: str) -> dict:
        """Cancel an unexecuted local draft. Already created foods remain available in FatSecret."""
        data, revision = self._get(action_ref, "action")
        self._pending(data)
        data["status"] = "cancelled"
        self._save("action", data, action_ref, revision=revision + 1)
        return {"cancelled": True}

    async def prepare_recipe_action(
        self, action: Literal["rename", "sync", "delete"], recipe_refs: list[str],
        title: str | None = None, source_variant_ref: str | None = None,
    ) -> dict:
        """Preview rename, synchronize a chosen account version, or delete recipes everywhere. Ask for approval."""
        if action not in {"rename", "sync", "delete"} or not recipe_refs or len(recipe_refs) > 100:
            raise WorkflowError("Укажи действие и от 1 до 100 рецептов.")
        if action != "delete" and len(recipe_refs) != 1:
            raise WorkflowError("Для этого действия выбери один рецепт.")
        values = []
        seen = set()
        for ref in recipe_refs:
            recipe = await self._live(ref)
            if recipe.id in seen:
                continue
            seen.add(recipe.id)
            variants = await self.engine.hydrate_live_recipe_variants(recipe)
            values.append({"ref": ref, "recipe": asdict(recipe), "fingerprints": {
                f"{v.account_key}:{v.remote_recipe_id}": (v.strict_fingerprint or recipe_fingerprint(v.recipe)).digest for v in variants}})
        if action == "rename":
            title = _text(title or "", "Новое название")
            await self._check_title(title, exclude_id=values[0]["recipe"]["id"])
        source = self._get(source_variant_ref, "variant")[0] if source_variant_ref else None
        if action == "sync":
            if source is None or source["recipe"]["id"] != values[0]["recipe"]["id"]:
                raise WorkflowError("Открой рецепт и выбери версию, которую нужно применить ко всем аккаунтам.")
            identity = f"{source['account_key']}:{source['remote_id']}"
            if values[0]["fingerprints"].get(identity) != source["digest"]:
                raise WorkflowError("Версия изменилась. Открой рецепт заново.")
        return self._action(action, {"recipes": values, "title": title, "source": source})

    async def _check_title(self, title: str, exclude_id: str | None = None) -> None:
        recipes = await self.engine.load_remote_recipe_index(self.group_id)
        if any(normalize_title(r.title) == normalize_title(title) and r.id != exclude_id for r in recipes):
            raise WorkflowError("Рецепт с таким названием уже существует. Выбери другое название или открой существующий рецепт для изменения.")

    def _results(self, values: list) -> dict:
        labels = self._accounts()
        return {"ok": bool(values) and all(v.ok for v in values), "accounts": [
            {"account": labels.get(v.account_key, "Аккаунт"), "ok": v.ok, "message": v.message} for v in values]}

    async def confirm_action(self, action_ref: str, revision: int, confirmed: bool = False) -> dict:
        """Execute ONLY after the user explicitly approves the latest exact preview.

        Pass its revision. Resolve every recipe ingredient first. Reusing the same action resumes a failed durable
        write or returns its saved result; never start a new draft merely because a write timed out.
        """
        if confirmed is not True:
            raise WorkflowError("Сначала покажи предпросмотр и получи явное подтверждение пользователя.")
        async with self._lock:
            data, current = self._get(action_ref, "action")
            if current != revision:
                raise WorkflowError("Черновик изменился. Покажи актуальный предпросмотр и подтверди его новую версию.")
            if data["status"] == "completed":
                return data["result"]
            if data["status"] == "cancelled":
                raise WorkflowError("Этот черновик отменён. Создай новый.")
            if sorted(self._accounts()) != data["accounts"]:
                raise WorkflowError("Состав аккаунтов группы изменился. Подготовь действие заново.")
            actor = int(self._group()["created_by"])
            kind = data["type"]
            bound_draft = None
            if kind == "food":
                definition = _definition(data["definition"])
                draft_ref = data.get("recipe_draft_ref")
                if draft_ref:
                    draft, draft_revision = self._get(draft_ref, "action")
                    self._pending(draft)
                    if draft_revision != data["recipe_draft_revision"]:
                        raise WorkflowError("Черновик рецепта изменился. Подготовь продукт для актуального ингредиента заново.")
                    entry = self._entry(draft, data["ingredient_ref"])
                    # Check weight before any remote mutation.
                    definition.nutrients_per_100g()
                created = await self.engine.create_custom_food_for_group(self.group_id, definition, actor)
                result = {"ok": True, "title": created.title, "accounts": list(self._accounts().values())}
                if draft_ref:
                    item = self.engine.custom_food_recipe_list_item(definition, created, Decimal(entry["grams"]), entry["query"])
                    item = replace(item, ingredient=replace(item.ingredient, id=entry["ref"]))
                    entry["selected"], entry["candidates"] = asdict(item), []
                    bound_draft = (draft, draft_ref, draft_revision + 1)
                    result["recipe_draft"] = self._preview(draft_ref, draft, draft_revision + 1)
            elif kind == "recipe":
                if not data["entries"] or any(e["selected"] is None for e in data["entries"]):
                    raise WorkflowError("Подбери и подтверди каждый ингредиент перед сохранением рецепта.")
                items = [_resolved(e["selected"]) for e in data["entries"]]
                kwargs = {"portions": Decimal(data["portions"]), "steps": data["steps"],
                          "cooked_weight_grams": Decimal(data["cooked_weight_grams"]) if data["cooked_weight_grams"] is not None else None}
                source = data.get("source")
                if source:
                    created = await self.engine.edit_recipe_from_list(
                        self.group_id, source["account_key"], source["remote_id"], source["digest"], items, actor, **kwargs)
                else:
                    # An already started engine journal must remain resumable, even when the new recipe is visible.
                    if data["status"] == "pending":
                        await self._check_title(data["title"])
                    data["status"] = "executing"
                    self._save("action", data, action_ref, revision=current)
                    created = await self.engine.create_recipe_from_list(self.group_id, data["title"], items, actor, **kwargs)
                result = self._results(created.results + created.replacement_results + created.rename_results)
                result["title"] = created.title or data["title"]
            else:
                refs = []
                for saved in data["recipes"]:
                    recipe = await self._live(saved["ref"])
                    variants = await self.engine.hydrate_live_recipe_variants(recipe)
                    current_fingerprints = {f"{v.account_key}:{v.remote_recipe_id}":
                                            (v.strict_fingerprint or recipe_fingerprint(v.recipe)).digest for v in variants}
                    if current_fingerprints != saved["fingerprints"]:
                        raise WorkflowError("Рецепт изменился после предпросмотра. Подготовь действие заново.")
                    refs.append(recipe)
                if kind == "rename":
                    await self._check_title(data["title"], exclude_id=refs[0].id)
                    result = self._results(await self.engine.rename_live_recipe_everywhere(refs[0], data["title"]))
                elif kind == "sync":
                    source = data["source"]
                    _, results = await self.engine.sync_live_recipe_from_source(refs[0], source["account_key"],
                        expected_source_remote_id=source["remote_id"], expected_source_strict_digest=source["digest"])
                    result = self._results(results)
                else:
                    results = await self.engine.delete_live_recipes_everywhere(refs)
                    result = {"ok": bool(results) and all(values and all(v.ok for v in values) for values in results.values()),
                              "recipes": [{"title": r.title, **self._results(results.get(r.id, []))} for r in refs]}
            result = json.loads(_json(result))
            if result["ok"]:
                data["status"] = "completed"
            data["result"] = result
            with self.storage._conn:
                if bound_draft is not None:
                    draft, draft_ref, draft_revision = bound_draft
                    self._save("action", draft, draft_ref, revision=draft_revision, commit=False)
                self._save("action", data, action_ref, revision=current, commit=False)
            return result
