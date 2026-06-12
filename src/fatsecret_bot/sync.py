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


def _same_decimal(left: Decimal, right: Decimal) -> bool:
    return left.quantize(Decimal("0.001")) == right.quantize(Decimal("0.001"))


def _has_ingredient(remote_recipe: Recipe, ingredient: Ingredient) -> bool:
    for remote in remote_recipe.ingredients:
        if remote.food_id == ingredient.food_id and _same_decimal(remote.amount, ingredient.amount):
            return True
        if remote.title.casefold() == ingredient.title.casefold() and _same_decimal(remote.amount, ingredient.amount):
            return True
    return False


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
                remote.ingredients = [
                    Ingredient(
                        id=item.id,
                        recipe_id=recipe_id,
                        food_id=item.food_id,
                        title=item.title,
                        portion_id=item.portion_id,
                        amount=item.amount,
                        portion_description=item.portion_description,
                        remote_ingredient_id=item.remote_ingredient_id,
                    )
                    for item in remote.ingredients
                ]
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

        results: list[AccountSyncResult] = []
        clients = self._build_clients()
        try:
            for account_key, client in clients.items():
                try:
                    remote_id = recipe.remote_ids.get(account_key)
                    remote_id = await self._ensure_remote_recipe(client, recipe, remote_id)
                    await self._sync_ingredients(client, recipe, remote_id)
                    ok = await client.save_recipe_meta(recipe, remote_id)
                    if not ok:
                        raise FatSecretError(f"{client.account.label}: recipe metadata save returned false")
                    self.storage.mark_synced(recipe.id, account_key, remote_id, recipe.version)
                    results.append(AccountSyncResult(account_key, remote_id, True, "ok"))
                except Exception as exc:  # noqa: BLE001 - keep per-account sync isolated.
                    self.storage.record_sync(recipe.id, account_key, "error", str(exc))
                    results.append(AccountSyncResult(account_key, recipe.remote_ids.get(account_key), False, str(exc)))
        finally:
            await self._close_clients(clients)
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

    async def _sync_ingredients(self, client: FatSecretClient, recipe: Recipe, remote_id: str) -> None:
        remote = await client.get_recipe(remote_id)
        for ingredient in recipe.ingredients:
            if _has_ingredient(remote, ingredient):
                continue
            ok = await client.add_ingredient(remote_id, ingredient)
            if not ok:
                raise FatSecretError(
                    f"{client.account.label}: ingredient save returned false for {ingredient.title}"
                )
