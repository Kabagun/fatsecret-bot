from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .fatsecret_client import FatSecretClient, FatSecretError
from .models import FatSecretAccountConfig, FatSecretDeviceConfig, FoodSearchResult, Ingredient, Recipe
from .storage import Storage


@dataclass(frozen=True)
class AccountSyncResult:
    account_key: str
    remote_recipe_id: str | None
    ok: bool
    message: str


@dataclass(frozen=True)
class IngredientSyncStats:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    extras: int = 0

    def message(self) -> str:
        """Return a compact user-facing summary of ingredient propagation."""
        parts: list[str] = []
        if self.added:
            parts.append(f"добавлено ингредиентов: {self.added}")
        if self.updated:
            parts.append(f"обновлено ингредиентов: {self.updated}")
        if self.unchanged:
            parts.append(f"без изменений: {self.unchanged}")
        if self.extras:
            parts.append(f"лишних ингредиентов в целевом рецепте: {self.extras} (не удалял)")
        return "; ".join(parts) if parts else "ингредиентов нет"


def _same_decimal(left: Decimal, right: Decimal) -> bool:
    return left.quantize(Decimal("0.001")) == right.quantize(Decimal("0.001"))


def _ingredient_identity(ingredient: Ingredient) -> str:
    return ingredient.remote_ingredient_id or ingredient.id


def _find_matching_ingredient(
    target_ingredients: list[Ingredient],
    source: Ingredient,
    used_target_ids: set[str],
) -> Ingredient | None:
    for target in target_ingredients:
        target_id = _ingredient_identity(target)
        if target_id in used_target_ids:
            continue
        if target.food_id and target.food_id == source.food_id:
            return target
    source_title = source.title.casefold()
    for target in target_ingredients:
        target_id = _ingredient_identity(target)
        if target_id in used_target_ids:
            continue
        if target.title.casefold() == source_title:
            return target
    return None


def _ingredient_needs_update(target: Ingredient, source: Ingredient) -> bool:
    return (
        target.food_id != source.food_id
        or target.title != source.title
        or (target.portion_id or "0") != (source.portion_id or "0")
        or not _same_decimal(target.amount, source.amount)
    )


def _copy_remote_ingredients(recipe_id: str, ingredients: list[Ingredient]) -> list[Ingredient]:
    return [
        Ingredient(
            id=f"{recipe_id}:{item.remote_ingredient_id or item.id}",
            recipe_id=recipe_id,
            food_id=item.food_id,
            title=item.title,
            portion_id=item.portion_id,
            amount=item.amount,
            portion_description=item.portion_description,
            remote_ingredient_id=item.remote_ingredient_id,
        )
        for item in ingredients
    ]


def _copy_recipe_from_remote(recipe_id: str, remote: Recipe) -> Recipe:
    recipe = Recipe(
        id=recipe_id,
        title=remote.title,
        description=remote.description,
        portions=remote.portions,
        prep_time=remote.prep_time,
        cook_time=remote.cook_time,
        default_portion_id=remote.default_portion_id,
    )
    recipe.ingredients = _copy_remote_ingredients(recipe_id, remote.ingredients)
    return recipe


class RecipeSyncEngine:
    def __init__(self, storage: Storage, device: FatSecretDeviceConfig) -> None:
        self.storage = storage
        self.device = device

    async def close(self) -> None:
        return None

    def _build_clients(self) -> dict[str, FatSecretClient]:
        accounts = self.storage.list_fatsecret_accounts()
        if not accounts:
            raise FatSecretError("Сначала подключи хотя бы один FatSecret аккаунт через кнопку «Аккаунты».")
        return {account.key: FatSecretClient(account, self.device) for account in accounts}

    async def _close_clients(self, clients: dict[str, FatSecretClient]) -> None:
        for client in clients.values():
            await client.close()

    async def validate_account(self, account: FatSecretAccountConfig) -> None:
        """Verify FatSecret credentials by performing a real mobile API login."""
        client = FatSecretClient(account, self.device)
        try:
            await client.login()
        finally:
            await client.close()

    async def refresh_account_recipes(self, account: FatSecretAccountConfig) -> int:
        """Import cookbook recipes for one connected FatSecret account."""
        client = FatSecretClient(account, self.device)
        imported = 0
        try:
            recipes = await client.cookbook()
            for summary in recipes:
                self.storage.import_remote_recipe(account.key, summary)
                imported += 1
        finally:
            await client.close()
        return imported

    async def refresh_remote_recipes(self) -> int:
        imported = 0
        clients = self._build_clients()
        try:
            for account_key, client in clients.items():
                recipes = await client.cookbook()
                for summary in recipes:
                    self.storage.import_remote_recipe(account_key, summary)
                    imported += 1
        finally:
            await self._close_clients(clients)
        return imported

    async def hydrate_recipe_from_remote(self, recipe_id: str) -> Recipe | None:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            return None
        if recipe.ingredients:
            return recipe

        clients = self._build_clients()
        try:
            for account_key, remote_id in recipe.remote_ids.items():
                client = clients.get(account_key)
                if client is None:
                    continue
                remote = await client.get_recipe(remote_id)
                remote.ingredients = _copy_remote_ingredients(recipe_id, remote.ingredients)
                self.storage.update_recipe_from_remote(
                    recipe_id=recipe_id,
                    title=remote.title or recipe.title,
                    description=remote.description,
                    portions=remote.portions,
                    prep_time=remote.prep_time,
                    cook_time=remote.cook_time,
                )
                self.storage.replace_ingredients(recipe_id, remote.ingredients)
                return self.storage.get_recipe(recipe_id)
        finally:
            await self._close_clients(clients)
        return recipe

    async def search_food(self, query: str, limit: int = 8) -> list[FoodSearchResult]:
        clients = self._build_clients()
        try:
            first_client = next(iter(clients.values()))
            autocomplete = await first_client.autocomplete_food(query)
            if autocomplete:
                return autocomplete[:limit]
            return (await first_client.search_recipes(query))[:limit]
        finally:
            await self._close_clients(clients)

    async def resolve_food(self, result: FoodSearchResult) -> FoodSearchResult:
        clients = self._build_clients()
        try:
            first_client = next(iter(clients.values()))
            return await first_client.resolve_food_detail(result)
        finally:
            await self._close_clients(clients)

    async def sync_recipe(self, recipe_id: str) -> list[AccountSyncResult]:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")
        if not recipe.remote_ids:
            raise FatSecretError("У рецепта нет привязки к FatSecret. Нажми «Обновить» и попробуй снова.")
        return await self.sync_recipe_from_source(recipe_id, next(iter(recipe.remote_ids)))

    async def sync_recipe_from_source(self, recipe_id: str, source_account_key: str) -> list[AccountSyncResult]:
        """Read a recipe from one FatSecret account and propagate it to every connected account."""
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")

        source_remote_id = recipe.remote_ids.get(source_account_key)
        if source_remote_id is None:
            raise FatSecretError("Выбранный аккаунт не содержит этот рецепт. Обнови список рецептов.")

        results: list[AccountSyncResult] = []
        clients = self._build_clients()
        try:
            source_client = clients.get(source_account_key)
            if source_client is None:
                raise FatSecretError("Аккаунт-источник больше не подключен.")

            source_remote = await source_client.get_recipe(source_remote_id)
            source_recipe = _copy_recipe_from_remote(recipe.id, source_remote)
            source_recipe.title = source_recipe.title or recipe.title
            source_recipe.remote_ids = dict(recipe.remote_ids)
            self.storage.update_recipe_from_remote(
                recipe_id=recipe.id,
                title=source_recipe.title,
                description=source_recipe.description,
                portions=source_recipe.portions,
                prep_time=source_recipe.prep_time,
                cook_time=source_recipe.cook_time,
            )
            self.storage.replace_ingredients(recipe.id, source_recipe.ingredients)
            recipe = self.storage.get_recipe(recipe.id) or source_recipe

            for account_key, client in clients.items():
                try:
                    remote_id = recipe.remote_ids.get(account_key)
                    if account_key == source_account_key:
                        self.storage.mark_synced(recipe.id, account_key, source_remote_id, recipe.version)
                        results.append(AccountSyncResult(account_key, source_remote_id, True, "источник"))
                        continue
                    remote_id = await self._ensure_remote_recipe(client, recipe, remote_id)
                    recipe.remote_ids[account_key] = remote_id
                    stats = await self._sync_ingredients(client, recipe, remote_id)
                    ok = await client.save_recipe_meta(recipe, remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe metadata save returned false")
                    self.storage.mark_synced(recipe.id, account_key, remote_id, recipe.version)
                    results.append(AccountSyncResult(account_key, remote_id, True, stats.message()))
                except Exception as exc:  # noqa: BLE001 - keep per-account sync isolated.
                    self.storage.record_sync(recipe.id, account_key, "error", str(exc))
                    results.append(AccountSyncResult(account_key, recipe.remote_ids.get(account_key), False, str(exc)))
        finally:
            await self._close_clients(clients)
        return results

    async def delete_recipe_everywhere(self, recipe_id: str) -> list[AccountSyncResult]:
        """Delete one recipe from every FatSecret account where it is mapped."""
        clients = self._build_clients()
        try:
            return await self._delete_recipe_with_clients(recipe_id, clients)
        finally:
            await self._close_clients(clients)

    async def delete_recipes_everywhere(self, recipe_ids: list[str]) -> dict[str, list[AccountSyncResult]]:
        """Delete several recipes from all mapped FatSecret accounts."""
        clients = self._build_clients()
        results: dict[str, list[AccountSyncResult]] = {}
        try:
            for recipe_id in recipe_ids:
                try:
                    results[recipe_id] = await self._delete_recipe_with_clients(recipe_id, clients)
                except Exception as exc:  # noqa: BLE001 - keep batch deletion moving.
                    results[recipe_id] = [AccountSyncResult("local", None, False, str(exc))]
        finally:
            await self._close_clients(clients)
        return results

    async def _delete_recipe_with_clients(
        self,
        recipe_id: str,
        clients: dict[str, FatSecretClient],
    ) -> list[AccountSyncResult]:
        recipe = self.storage.get_recipe(recipe_id)
        if recipe is None:
            raise FatSecretError(f"Unknown local recipe id: {recipe_id}")
        if not recipe.remote_ids:
            self.storage.delete_recipe(recipe.id)
            return [AccountSyncResult("local", None, True, "нет привязок к FatSecret; удалил локально")]

        results: list[AccountSyncResult] = []
        deleted_account_keys: list[str] = []
        for account_key, remote_id in list(recipe.remote_ids.items()):
            client = clients.get(account_key)
            if client is None:
                message = "FatSecret аккаунт больше не подключен"
                self.storage.record_sync(recipe.id, account_key, "error", message)
                results.append(AccountSyncResult(account_key, remote_id, False, message))
                continue
            try:
                ok = await client.delete_recipe(remote_id)
                if not ok:
                    raise FatSecretError(f"{client.account.label}: recipe delete returned false")
                self.storage.record_sync(recipe.id, account_key, "ok", f"deleted remote recipe {remote_id}")
                deleted_account_keys.append(account_key)
                results.append(AccountSyncResult(account_key, remote_id, True, "удален в FatSecret"))
            except Exception as exc:  # noqa: BLE001 - keep per-account deletion isolated.
                self.storage.record_sync(recipe.id, account_key, "error", str(exc))
                results.append(AccountSyncResult(account_key, remote_id, False, str(exc)))

        for account_key in deleted_account_keys:
            self.storage.delete_remote_recipe_id(recipe.id, account_key)
        if deleted_account_keys and not self.storage.remote_ids(recipe.id):
            self.storage.delete_recipe(recipe.id)
        return results

    async def _ensure_remote_recipe(
        self,
        client: FatSecretClient,
        recipe: Recipe,
        remote_id: str | None,
    ) -> str:
        if remote_id:
            return remote_id

        return await client.create_recipe(recipe)

    async def _sync_ingredients(self, client: FatSecretClient, recipe: Recipe, remote_id: str) -> IngredientSyncStats:
        remote = await client.get_recipe(remote_id)
        used_target_ids: set[str] = set()
        added = 0
        updated = 0
        unchanged = 0
        for ingredient in recipe.ingredients:
            target = _find_matching_ingredient(remote.ingredients, ingredient, used_target_ids)
            if target is None:
                ok = await client.add_ingredient(remote_id, ingredient)
                added += 1
            elif not _ingredient_needs_update(target, ingredient):
                used_target_ids.add(_ingredient_identity(target))
                unchanged += 1
                continue
            else:
                used_target_ids.add(_ingredient_identity(target))
                ok = await client.add_ingredient(
                    remote_id,
                    Ingredient(
                        id=target.id,
                        recipe_id=remote_id,
                        food_id=ingredient.food_id,
                        title=ingredient.title,
                        portion_id=ingredient.portion_id,
                        amount=ingredient.amount,
                        portion_description=ingredient.portion_description,
                        remote_ingredient_id=_ingredient_identity(target),
                    ),
                )
                updated += 1
            if not ok:
                raise FatSecretError(
                    f"{client.account.label}: FatSecret не принял ингредиент «{ingredient.title}». "
                    "Если это свой продукт, нужен capture API создания собственного продукта."
                )
        extras = sum(1 for target in remote.ingredients if _ingredient_identity(target) not in used_target_ids)
        return IngredientSyncStats(added=added, updated=updated, unchanged=unchanged, extras=extras)
