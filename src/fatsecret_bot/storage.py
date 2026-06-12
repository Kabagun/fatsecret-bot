from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path

from .models import FatSecretAccountConfig, Ingredient, Recipe, RecipeSummary


def normalize_title(title: str) -> str:
    return " ".join(title.casefold().strip().split())


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self.migrate()

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_users (
                telegram_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fatsecret_accounts (
                account_key TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL UNIQUE,
                label TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                market TEXT NOT NULL,
                language TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recipes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                portions TEXT NOT NULL DEFAULT '1',
                prep_time INTEGER NOT NULL DEFAULT 0,
                cook_time INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_normalized_title
                ON recipes(normalized_title);

            CREATE TABLE IF NOT EXISTS ingredients (
                id TEXT PRIMARY KEY,
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                food_id TEXT NOT NULL,
                title TEXT NOT NULL,
                portion_id TEXT NOT NULL DEFAULT '0',
                amount TEXT NOT NULL DEFAULT '0',
                portion_description TEXT NOT NULL DEFAULT '',
                remote_ingredient_id TEXT,
                position INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS account_recipes (
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                account_key TEXT NOT NULL,
                remote_recipe_id TEXT NOT NULL,
                last_synced_version INTEGER NOT NULL DEFAULT 0,
                synced_at TEXT,
                PRIMARY KEY (recipe_id, account_key)
            );

            CREATE TABLE IF NOT EXISTS sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id TEXT NOT NULL,
                account_key TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def fatsecret_account_count(self) -> int:
        """Return how many FatSecret accounts are connected to the bot."""
        row = self._conn.execute("SELECT COUNT(*) AS c FROM fatsecret_accounts").fetchone()
        return int(row["c"])

    def list_fatsecret_accounts(self) -> list[FatSecretAccountConfig]:
        """Return connected FatSecret accounts for runtime API clients."""
        rows = self._conn.execute(
            """
            SELECT account_key, label, username, password, market, language
            FROM fatsecret_accounts
            ORDER BY label ASC, account_key ASC
            """
        ).fetchall()
        return [
            FatSecretAccountConfig(
                key=row["account_key"],
                label=row["label"],
                username=row["username"],
                password=row["password"],
                market=row["market"],
                language=row["language"],
            )
            for row in rows
        ]

    def get_fatsecret_account_by_telegram_id(self, telegram_id: int) -> FatSecretAccountConfig | None:
        """Return the FatSecret account connected by a Telegram user, if any."""
        row = self._conn.execute(
            """
            SELECT account_key, label, username, password, market, language
            FROM fatsecret_accounts
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
        if row is None:
            return None
        return FatSecretAccountConfig(
            key=row["account_key"],
            label=row["label"],
            username=row["username"],
            password=row["password"],
            market=row["market"],
            language=row["language"],
        )

    def upsert_fatsecret_account(
        self,
        telegram_id: int,
        label: str,
        username: str,
        password: str,
        market: str,
        language: str,
    ) -> str:
        """Create or replace the FatSecret account owned by a Telegram user."""
        existing = self._conn.execute(
            "SELECT account_key FROM fatsecret_accounts WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        account_key = existing["account_key"] if existing else f"tg{telegram_id}"
        now = _now()
        self._conn.execute(
            """
            INSERT INTO fatsecret_accounts(
                account_key, telegram_id, label, username, password, market,
                language, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_key) DO UPDATE SET
                label = excluded.label,
                username = excluded.username,
                password = excluded.password,
                market = excluded.market,
                language = excluded.language,
                updated_at = excluded.updated_at
            """,
            (account_key, telegram_id, label, username, password, market, language, now, now),
        )
        self._conn.commit()
        return account_key

    def delete_fatsecret_account_for_user(self, telegram_id: int) -> bool:
        """Delete a user's FatSecret account and stale remote recipe mappings."""
        row = self._conn.execute(
            "SELECT account_key FROM fatsecret_accounts WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM account_recipes WHERE account_key = ?", (row["account_key"],))
        self._conn.execute("DELETE FROM fatsecret_accounts WHERE telegram_id = ?", (telegram_id,))
        self._conn.commit()
        return True

    def registered_user_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM telegram_users").fetchone()
        return int(row["c"])

    def is_registered_user(self, telegram_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM telegram_users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return row is not None

    def register_user(self, telegram_id: int, display_name: str) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO telegram_users(telegram_id, display_name, created_at)
            VALUES (?, ?, ?)
            """,
            (telegram_id, display_name, _now()),
        )
        self._conn.commit()

    def import_remote_recipe(self, account_key: str, summary: RecipeSummary) -> str:
        normalized = normalize_title(summary.title)
        row = self._conn.execute(
            """
            SELECT r.id
            FROM recipes r
            LEFT JOIN account_recipes ar
                ON ar.recipe_id = r.id AND ar.account_key = ? AND ar.remote_recipe_id = ?
            WHERE ar.recipe_id IS NOT NULL OR r.normalized_title = ?
            LIMIT 1
            """,
            (account_key, summary.remote_id, normalized),
        ).fetchone()
        recipe_id = row["id"] if row else str(uuid.uuid4())
        if row is None:
            self._conn.execute(
                """
                INSERT INTO recipes(id, title, normalized_title, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (recipe_id, summary.title, normalized, _now()),
            )
        self.set_remote_recipe_id(recipe_id, account_key, summary.remote_id, last_synced_version=0)
        self._conn.commit()
        return recipe_id

    def create_recipe(
        self,
        title: str,
        description: str,
        portions: Decimal,
        prep_time: int,
        cook_time: int,
        updated_by: int | None,
    ) -> str:
        recipe_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO recipes(
                id, title, normalized_title, description, portions, prep_time,
                cook_time, version, updated_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                recipe_id,
                title,
                normalize_title(title),
                description,
                str(portions),
                prep_time,
                cook_time,
                updated_by,
                _now(),
            ),
        )
        self._conn.commit()
        return recipe_id

    def update_recipe_meta(
        self,
        recipe_id: str,
        title: str,
        description: str,
        portions: Decimal,
        prep_time: int,
        cook_time: int,
        updated_by: int | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE recipes
            SET title = ?, normalized_title = ?, description = ?, portions = ?,
                prep_time = ?, cook_time = ?, version = version + 1,
                updated_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                normalize_title(title),
                description,
                str(portions),
                prep_time,
                cook_time,
                updated_by,
                _now(),
                recipe_id,
            ),
        )
        self._conn.commit()

    def update_recipe_from_remote(
        self,
        recipe_id: str,
        title: str,
        description: str,
        portions: Decimal,
        prep_time: int,
        cook_time: int,
    ) -> None:
        self._conn.execute(
            """
            UPDATE recipes
            SET title = ?, normalized_title = ?, description = ?, portions = ?,
                prep_time = ?, cook_time = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                normalize_title(title),
                description,
                str(portions),
                prep_time,
                cook_time,
                _now(),
                recipe_id,
            ),
        )
        self._conn.commit()

    def get_recipe(self, recipe_id: str) -> Recipe | None:
        row = self._conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if row is None:
            return None
        recipe = Recipe(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            portions=Decimal(row["portions"]),
            prep_time=int(row["prep_time"]),
            cook_time=int(row["cook_time"]),
            default_portion_id="0",
            version=int(row["version"]),
        )
        recipe.ingredients = self.list_ingredients(recipe.id)
        recipe.remote_ids = self.remote_ids(recipe.id)
        return recipe

    def list_recipes(self) -> list[Recipe]:
        rows = self._conn.execute(
            "SELECT id FROM recipes ORDER BY normalized_title ASC"
        ).fetchall()
        return [r for row in rows if (r := self.get_recipe(row["id"])) is not None]

    def list_ingredients(self, recipe_id: str) -> list[Ingredient]:
        rows = self._conn.execute(
            """
            SELECT * FROM ingredients
            WHERE recipe_id = ?
            ORDER BY position ASC, title ASC
            """,
            (recipe_id,),
        ).fetchall()
        return [
            Ingredient(
                id=row["id"],
                recipe_id=row["recipe_id"],
                food_id=row["food_id"],
                title=row["title"],
                portion_id=row["portion_id"],
                amount=Decimal(row["amount"]),
                portion_description=row["portion_description"],
                remote_ingredient_id=row["remote_ingredient_id"],
            )
            for row in rows
        ]

    def replace_ingredients(self, recipe_id: str, ingredients: list[Ingredient]) -> None:
        self._conn.execute("DELETE FROM ingredients WHERE recipe_id = ?", (recipe_id,))
        for index, ingredient in enumerate(ingredients):
            self._insert_ingredient(recipe_id, ingredient, index)
        self._conn.commit()

    def delete_recipe(self, recipe_id: str) -> bool:
        """Delete a local recipe cache entry and all bot-side sync metadata."""
        row = self._conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM ingredients WHERE recipe_id = ?", (recipe_id,))
        self._conn.execute("DELETE FROM account_recipes WHERE recipe_id = ?", (recipe_id,))
        self._conn.execute("DELETE FROM sync_events WHERE recipe_id = ?", (recipe_id,))
        self._conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        self._conn.commit()
        return True

    def add_ingredient(
        self,
        recipe_id: str,
        food_id: str,
        title: str,
        portion_id: str,
        amount: Decimal,
        portion_description: str = "",
    ) -> str:
        ingredient = Ingredient(
            id=str(uuid.uuid4()),
            recipe_id=recipe_id,
            food_id=food_id,
            title=title,
            portion_id=portion_id or "0",
            amount=amount,
            portion_description=portion_description,
        )
        position_row = self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM ingredients WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        self._insert_ingredient(recipe_id, ingredient, int(position_row["next_position"]))
        self._conn.execute(
            "UPDATE recipes SET version = version + 1, updated_at = ? WHERE id = ?",
            (_now(), recipe_id),
        )
        self._conn.commit()
        return ingredient.id

    def _insert_ingredient(self, recipe_id: str, ingredient: Ingredient, position: int) -> None:
        self._conn.execute(
            """
            INSERT INTO ingredients(
                id, recipe_id, food_id, title, portion_id, amount,
                portion_description, remote_ingredient_id, position
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ingredient.id,
                recipe_id,
                ingredient.food_id,
                ingredient.title,
                ingredient.portion_id or "0",
                str(ingredient.amount),
                ingredient.portion_description,
                ingredient.remote_ingredient_id,
                position,
            ),
        )

    def remote_ids(self, recipe_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT account_key, remote_recipe_id FROM account_recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchall()
        return {row["account_key"]: row["remote_recipe_id"] for row in rows}

    def set_remote_recipe_id(
        self,
        recipe_id: str,
        account_key: str,
        remote_recipe_id: str,
        last_synced_version: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO account_recipes(recipe_id, account_key, remote_recipe_id, last_synced_version, synced_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(recipe_id, account_key) DO UPDATE SET
                remote_recipe_id = excluded.remote_recipe_id,
                last_synced_version = excluded.last_synced_version,
                synced_at = excluded.synced_at
            """,
            (recipe_id, account_key, remote_recipe_id, last_synced_version, _now()),
        )

    def delete_remote_recipe_id(self, recipe_id: str, account_key: str) -> bool:
        """Remove one FatSecret account mapping for a local recipe."""
        cursor = self._conn.execute(
            "DELETE FROM account_recipes WHERE recipe_id = ? AND account_key = ?",
            (recipe_id, account_key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_synced(self, recipe_id: str, account_key: str, remote_recipe_id: str, version: int) -> None:
        self.set_remote_recipe_id(recipe_id, account_key, remote_recipe_id, version)
        self.record_sync(recipe_id, account_key, "ok", f"synced remote recipe {remote_recipe_id}")
        self._conn.commit()

    def record_sync(self, recipe_id: str, account_key: str, status: str, message: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_events(recipe_id, account_key, status, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (recipe_id, account_key, status, message, _now()),
        )
        self._conn.commit()
