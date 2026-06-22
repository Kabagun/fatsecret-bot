from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sqlite3
import string
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import (
    CachedFoodUsage,
    MAX_RECIPE_STEPS,
    FatSecretAccountConfig,
    FatSecretSession,
    Ingredient,
    Recipe,
    RecipeGroup,
    RecipeGroupMember,
    RecipeSummary,
)


INVITE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
PORTION_UNIT_RE = re.compile(r"^\s*(\d+(?:[\.,]\d+)?)\s*(?:г|гр|g|gram|грам|мл|ml)\b", re.IGNORECASE)


def normalize_title(title: str) -> str:
    return " ".join(title.casefold().strip().split())


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _steps_to_json(steps: list[str] | None) -> str:
    clean_steps = [step.strip() for step in steps or [] if step.strip()]
    return json.dumps(clean_steps[:MAX_RECIPE_STEPS], ensure_ascii=False)


def _steps_from_json(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(step).strip() for step in data if str(step).strip()][:MAX_RECIPE_STEPS]


def _decimal_to_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _bare_weight_portion_description(description: str) -> bool:
    normalized = description.strip().casefold()
    return normalized in {"г", "гр", "g", "gram", "grams", "грам", ""}


def _portion_unit_size(description: str) -> Decimal | None:
    match = PORTION_UNIT_RE.search(description.replace("\xa0", " "))
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None


def _ingredient_grams(amount: Decimal, portion_description: str) -> Decimal | None:
    unit_size = _portion_unit_size(portion_description)
    if unit_size is not None and unit_size > 0:
        return amount * unit_size
    if _bare_weight_portion_description(portion_description):
        return amount
    return None


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _new_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))


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
                active_group_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recipe_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                invite_code TEXT NOT NULL UNIQUE,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL REFERENCES recipe_groups(id) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL REFERENCES telegram_users(telegram_id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (group_id, telegram_id)
            );

            CREATE TABLE IF NOT EXISTS fatsecret_accounts (
                account_key TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL UNIQUE,
                label TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                market TEXT NOT NULL,
                language TEXT NOT NULL,
                session_server_id TEXT,
                session_device_key TEXT,
                session_secret_key TEXT,
                session_updated_at TEXT,
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
                group_id TEXT,
                updated_by INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id TEXT PRIMARY KEY,
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                food_id TEXT NOT NULL,
                title TEXT NOT NULL,
                portion_id TEXT NOT NULL DEFAULT '0',
                amount TEXT NOT NULL DEFAULT '0',
                portion_description TEXT NOT NULL DEFAULT '',
                remote_ingredient_id TEXT,
                grams TEXT,
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

            CREATE TABLE IF NOT EXISTS food_usage_cache (
                group_id TEXT NOT NULL,
                food_id TEXT NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                portion_id TEXT NOT NULL DEFAULT '0',
                portion_description TEXT NOT NULL DEFAULT '',
                use_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, food_id, normalized_title)
            );

            CREATE TABLE IF NOT EXISTS food_usage_refreshes (
                group_id TEXT PRIMARY KEY,
                refreshed_at TEXT NOT NULL
            );
            """
        )
        self._ensure_column("telegram_users", "active_group_id", "TEXT")
        self._ensure_column("fatsecret_accounts", "session_server_id", "TEXT")
        self._ensure_column("fatsecret_accounts", "session_device_key", "TEXT")
        self._ensure_column("fatsecret_accounts", "session_secret_key", "TEXT")
        self._ensure_column("fatsecret_accounts", "session_updated_at", "TEXT")
        self._ensure_column("recipes", "group_id", "TEXT")
        self._ensure_column("recipes", "steps", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("ingredients", "grams", "TEXT")
        self._conn.execute("DROP INDEX IF EXISTS idx_recipes_normalized_title")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_group_title ON recipes(group_id, normalized_title)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_food_usage_cache_group_count "
            "ON food_usage_cache(group_id, use_count DESC, normalized_title ASC)"
        )
        self._backfill_default_group()
        self._normalize_zero_portion_gram_ingredients()
        self._backfill_ingredient_grams()
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _backfill_default_group(self) -> None:
        group_count = int(self._conn.execute("SELECT COUNT(*) AS c FROM recipe_groups").fetchone()["c"])
        user_count = int(self._conn.execute("SELECT COUNT(*) AS c FROM telegram_users").fetchone()["c"])
        recipe_count = int(self._conn.execute("SELECT COUNT(*) AS c FROM recipes").fetchone()["c"])
        if group_count == 0 and (user_count or recipe_count):
            group_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO recipe_groups(id, name, invite_code, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, "Основная группа", self._unique_invite_code(), 0, _now()),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO group_members(group_id, telegram_id, joined_at)
                SELECT ?, telegram_id, ? FROM telegram_users
                """,
                (group_id, _now()),
            )
            self._conn.execute(
                "UPDATE telegram_users SET active_group_id = ? WHERE active_group_id IS NULL",
                (group_id,),
            )
            self._conn.execute(
                "UPDATE recipes SET group_id = ? WHERE group_id IS NULL",
                (group_id,),
            )
            return

        first_group = self._conn.execute(
            "SELECT id FROM recipe_groups ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if first_group is None:
            return
        self._conn.execute(
            "UPDATE recipes SET group_id = ? WHERE group_id IS NULL",
            (first_group["id"],),
        )
        self._conn.execute(
            """
            UPDATE telegram_users
            SET active_group_id = COALESCE(
                (
                    SELECT gm.group_id
                    FROM group_members gm
                    WHERE gm.telegram_id = telegram_users.telegram_id
                    ORDER BY gm.joined_at ASC
                    LIMIT 1
                ),
                active_group_id
            )
            WHERE active_group_id IS NULL
            """
        )

    def _normalize_zero_portion_gram_ingredients(self) -> None:
        rows = self._conn.execute(
            """
            SELECT id, amount, portion_description
            FROM ingredients
            WHERE portion_id = '0'
            """
        ).fetchall()
        for row in rows:
            if not _bare_weight_portion_description(row["portion_description"]):
                continue
            try:
                amount = Decimal(row["amount"])
            except InvalidOperation:
                continue
            self._conn.execute(
                """
                UPDATE ingredients
                SET amount = ?, portion_description = '100г'
                WHERE id = ?
                """,
                (_decimal_to_text(amount / Decimal("100")), row["id"]),
            )

    def _backfill_ingredient_grams(self) -> None:
        rows = self._conn.execute(
            """
            SELECT id, amount, portion_description, grams
            FROM ingredients
            WHERE grams IS NULL OR grams = ''
            """
        ).fetchall()
        for row in rows:
            amount = _decimal_or_none(row["amount"])
            if amount is None:
                continue
            grams = _ingredient_grams(amount, row["portion_description"])
            if grams is None:
                continue
            self._conn.execute(
                "UPDATE ingredients SET grams = ? WHERE id = ?",
                (_decimal_to_text(grams), row["id"]),
            )

    def _unique_invite_code(self) -> str:
        while True:
            code = _new_invite_code()
            row = self._conn.execute(
                "SELECT 1 FROM recipe_groups WHERE invite_code = ?",
                (code,),
            ).fetchone()
            if row is None:
                return code

    def fatsecret_account_count(self, group_id: str | None = None) -> int:
        """Return how many FatSecret accounts are connected to the bot."""
        if group_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM fatsecret_accounts").fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM fatsecret_accounts fa
                JOIN group_members gm ON gm.telegram_id = fa.telegram_id
                WHERE gm.group_id = ?
                """,
                (group_id,),
            ).fetchone()
        return int(row["c"])

    def list_fatsecret_accounts(self, group_id: str | None = None) -> list[FatSecretAccountConfig]:
        """Return connected FatSecret accounts for runtime API clients."""
        if group_id is None:
            rows = self._conn.execute(
                """
                SELECT account_key, label, username, password, market, language
                FROM fatsecret_accounts
                ORDER BY label ASC, account_key ASC
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT fa.account_key, fa.label, fa.username, fa.password, fa.market, fa.language
                FROM fatsecret_accounts fa
                JOIN group_members gm ON gm.telegram_id = fa.telegram_id
                WHERE gm.group_id = ?
                ORDER BY fa.label ASC, fa.account_key ASC
                """,
                (group_id,),
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

    def get_fatsecret_account(self, account_key: str) -> FatSecretAccountConfig | None:
        """Return one connected FatSecret account by storage key."""
        row = self._conn.execute(
            """
            SELECT account_key, label, username, password, market, language
            FROM fatsecret_accounts
            WHERE account_key = ?
            """,
            (account_key,),
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

    def get_fatsecret_session(self, account_key: str) -> FatSecretSession | None:
        """Return a cached FatSecret mobile session for an account, if one is stored."""
        row = self._conn.execute(
            """
            SELECT session_server_id, session_device_key, session_secret_key
            FROM fatsecret_accounts
            WHERE account_key = ?
            """,
            (account_key,),
        ).fetchone()
        if row is None:
            return None
        server_id = row["session_server_id"]
        device_key = row["session_device_key"]
        secret_key = row["session_secret_key"]
        if not server_id or not device_key or not secret_key:
            return None
        return FatSecretSession(server_id=server_id, device_key=device_key, secret_key=secret_key)

    def update_fatsecret_session(self, account_key: str, session: FatSecretSession) -> bool:
        """Persist the latest FatSecret mobile session for reuse by future API clients."""
        cursor = self._conn.execute(
            """
            UPDATE fatsecret_accounts
            SET session_server_id = ?,
                session_device_key = ?,
                session_secret_key = ?,
                session_updated_at = ?,
                updated_at = ?
            WHERE account_key = ?
            """,
            (session.server_id, session.device_key, session.secret_key, _now(), _now(), account_key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def active_group_for_user(self, telegram_id: int) -> RecipeGroup | None:
        """Return the active recipe group for a Telegram user."""
        row = self._conn.execute(
            """
            SELECT g.id, g.name, g.invite_code
            FROM telegram_users u
            JOIN recipe_groups g ON g.id = u.active_group_id
            JOIN group_members gm ON gm.group_id = g.id AND gm.telegram_id = u.telegram_id
            WHERE u.telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
        if row is None:
            return None
        return RecipeGroup(id=row["id"], name=row["name"], invite_code=row["invite_code"])

    def list_group_ids(self) -> list[str]:
        """Return all recipe group ids known to the bot."""
        rows = self._conn.execute(
            "SELECT id FROM recipe_groups ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [row["id"] for row in rows]

    def list_groups_for_user(self, telegram_id: int) -> list[RecipeGroup]:
        """Return groups that a Telegram user belongs to."""
        rows = self._conn.execute(
            """
            SELECT g.id, g.name, g.invite_code
            FROM recipe_groups g
            JOIN group_members gm ON gm.group_id = g.id
            WHERE gm.telegram_id = ?
            ORDER BY g.name ASC, g.created_at ASC
            """,
            (telegram_id,),
        ).fetchall()
        return [RecipeGroup(id=row["id"], name=row["name"], invite_code=row["invite_code"]) for row in rows]

    def group_members(self, group_id: str) -> list[RecipeGroupMember]:
        """Return Telegram users joined to a recipe group with their FatSecret account, if connected."""
        rows = self._conn.execute(
            """
            SELECT
                u.telegram_id,
                u.display_name,
                fa.label AS fatsecret_label,
                fa.username AS fatsecret_username
            FROM group_members gm
            JOIN telegram_users u ON u.telegram_id = gm.telegram_id
            LEFT JOIN fatsecret_accounts fa ON fa.telegram_id = u.telegram_id
            WHERE gm.group_id = ?
            ORDER BY u.display_name ASC, u.telegram_id ASC
            """,
            (group_id,),
        ).fetchall()
        return [
            RecipeGroupMember(
                telegram_id=int(row["telegram_id"]),
                display_name=row["display_name"],
                fatsecret_label=row["fatsecret_label"],
                fatsecret_username=row["fatsecret_username"],
            )
            for row in rows
        ]

    def create_group(self, telegram_id: int, name: str) -> RecipeGroup:
        """Create a recipe sync group and make it active for the creator."""
        group = RecipeGroup(id=str(uuid.uuid4()), name=name.strip() or "Группа", invite_code=self._unique_invite_code())
        now = _now()
        self._conn.execute(
            """
            INSERT INTO recipe_groups(id, name, invite_code, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group.id, group.name, group.invite_code, telegram_id, now),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO group_members(group_id, telegram_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (group.id, telegram_id, now),
        )
        self._conn.execute(
            "UPDATE telegram_users SET active_group_id = ? WHERE telegram_id = ?",
            (group.id, telegram_id),
        )
        self._conn.commit()
        return group

    def join_group_by_code(self, telegram_id: int, invite_code: str) -> RecipeGroup | None:
        """Join a group by invite code and make it active for the user."""
        normalized = invite_code.strip().upper().replace(" ", "")
        row = self._conn.execute(
            "SELECT id, name, invite_code FROM recipe_groups WHERE invite_code = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            """
            INSERT OR IGNORE INTO group_members(group_id, telegram_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (row["id"], telegram_id, _now()),
        )
        self._conn.execute(
            "UPDATE telegram_users SET active_group_id = ? WHERE telegram_id = ?",
            (row["id"], telegram_id),
        )
        self._conn.commit()
        return RecipeGroup(id=row["id"], name=row["name"], invite_code=row["invite_code"])

    def set_active_group_for_user(self, telegram_id: int, group_id: str) -> bool:
        """Switch the active group if the Telegram user is a group member."""
        row = self._conn.execute(
            "SELECT 1 FROM group_members WHERE telegram_id = ? AND group_id = ?",
            (telegram_id, group_id),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute(
            "UPDATE telegram_users SET active_group_id = ? WHERE telegram_id = ?",
            (group_id, telegram_id),
        )
        self._conn.commit()
        return True

    def active_group_created_by(self, telegram_id: int) -> bool:
        """Return whether the Telegram user created their active recipe group."""
        row = self._conn.execute(
            """
            SELECT 1
            FROM telegram_users u
            JOIN recipe_groups g ON g.id = u.active_group_id
            WHERE u.telegram_id = ? AND g.created_by = ?
            """,
            (telegram_id, telegram_id),
        ).fetchone()
        return row is not None

    def rename_active_group(self, telegram_id: int, name: str) -> RecipeGroup | None:
        """Rename the active recipe group when the Telegram user is its creator."""
        clean_name = name.strip()
        if not clean_name:
            return None
        row = self._conn.execute(
            """
            SELECT g.id, g.invite_code
            FROM telegram_users u
            JOIN recipe_groups g ON g.id = u.active_group_id
            WHERE u.telegram_id = ? AND g.created_by = ?
            """,
            (telegram_id, telegram_id),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE recipe_groups SET name = ? WHERE id = ?",
            (clean_name, row["id"]),
        )
        self._conn.commit()
        return RecipeGroup(id=row["id"], name=clean_name, invite_code=row["invite_code"])

    def leave_active_group(self, telegram_id: int) -> RecipeGroup | None:
        """Remove a Telegram user from their active group and switch to another joined group if one exists."""
        group = self.active_group_for_user(telegram_id)
        if group is None:
            return None
        self._conn.execute(
            "DELETE FROM group_members WHERE telegram_id = ? AND group_id = ?",
            (telegram_id, group.id),
        )
        next_group = self._conn.execute(
            """
            SELECT group_id
            FROM group_members
            WHERE telegram_id = ?
            ORDER BY joined_at ASC
            LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()
        self._conn.execute(
            "UPDATE telegram_users SET active_group_id = ? WHERE telegram_id = ?",
            (next_group["group_id"] if next_group else None, telegram_id),
        )
        self._conn.commit()
        return group

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
                session_server_id = NULL,
                session_device_key = NULL,
                session_secret_key = NULL,
                session_updated_at = NULL,
                updated_at = excluded.updated_at
            """,
            (account_key, telegram_id, label, username, password, market, language, now, now),
        )
        self._conn.commit()
        return account_key

    def update_fatsecret_account_label(self, account_key: str, label: str) -> bool:
        """Update the bot-facing nickname for one connected FatSecret account."""
        clean_label = label.strip()[:32]
        if not clean_label:
            return False
        cursor = self._conn.execute(
            """
            UPDATE fatsecret_accounts
            SET label = ?, updated_at = ?
            WHERE account_key = ?
            """,
            (clean_label, _now(), account_key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

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

    def delete_fatsecret_account(self, account_key: str) -> bool:
        """Delete a selected FatSecret account and stale remote recipe mappings."""
        row = self._conn.execute(
            "SELECT 1 FROM fatsecret_accounts WHERE account_key = ?",
            (account_key,),
        ).fetchone()
        if row is None:
            return False
        self._conn.execute("DELETE FROM account_recipes WHERE account_key = ?", (account_key,))
        self._conn.execute("DELETE FROM fatsecret_accounts WHERE account_key = ?", (account_key,))
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

    def import_remote_recipe(self, account_key: str, summary: RecipeSummary, group_id: str | None = None) -> str:
        normalized = normalize_title(summary.title)
        row = self._conn.execute(
            """
            SELECT r.id
            FROM recipes r
            LEFT JOIN account_recipes ar
                ON ar.recipe_id = r.id AND ar.account_key = ? AND ar.remote_recipe_id = ?
            WHERE ar.recipe_id IS NOT NULL
                OR (
                    r.normalized_title = ?
                    AND (r.group_id = ? OR (r.group_id IS NULL AND ? IS NULL))
                )
            LIMIT 1
            """,
            (account_key, summary.remote_id, normalized, group_id, group_id),
        ).fetchone()
        recipe_id = row["id"] if row else str(uuid.uuid4())
        if row is None:
            self._conn.execute(
                """
                INSERT INTO recipes(id, title, normalized_title, group_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (recipe_id, summary.title, normalized, group_id, _now()),
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
        group_id: str | None = None,
        steps: list[str] | None = None,
    ) -> str:
        recipe_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO recipes(
                id, title, normalized_title, description, portions, prep_time,
                cook_time, version, group_id, updated_by, updated_at, steps
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                recipe_id,
                title,
                normalize_title(title),
                description,
                str(portions),
                prep_time,
                cook_time,
                group_id,
                updated_by,
                _now(),
                _steps_to_json(steps),
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
        steps: list[str] | None = None,
    ) -> None:
        if steps is None:
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
        else:
            self._conn.execute(
                """
                UPDATE recipes
                SET title = ?, normalized_title = ?, description = ?, portions = ?,
                    prep_time = ?, cook_time = ?, steps = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    normalize_title(title),
                    description,
                    str(portions),
                    prep_time,
                    cook_time,
                    _steps_to_json(steps),
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
            steps=_steps_from_json(row["steps"]),
            default_portion_id="0",
            version=int(row["version"]),
            group_id=row["group_id"],
        )
        recipe.ingredients = self.list_ingredients(recipe.id)
        recipe.remote_ids = self.remote_ids(recipe.id)
        return recipe

    def list_recipes(self, group_id: str | None = None) -> list[Recipe]:
        if group_id is None:
            rows = self._conn.execute(
                "SELECT id FROM recipes ORDER BY normalized_title ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id FROM recipes WHERE group_id = ? ORDER BY normalized_title ASC",
                (group_id,),
            ).fetchall()
        return [r for row in rows if (r := self.get_recipe(row["id"])) is not None]

    def count_recipes(self, group_id: str | None = None) -> int:
        """Return the number of locally cached recipes, optionally limited to one group."""
        if group_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM recipes").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM recipes WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        return int(row["c"])

    def list_recipe_page(self, group_id: str | None, page: int, page_size: int) -> list[Recipe]:
        """Return one ordered page of locally cached recipes."""
        page = max(0, page)
        page_size = max(1, page_size)
        offset = page * page_size
        if group_id is None:
            rows = self._conn.execute(
                """
                SELECT id FROM recipes
                ORDER BY normalized_title ASC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id FROM recipes
                WHERE group_id = ?
                ORDER BY normalized_title ASC
                LIMIT ? OFFSET ?
                """,
                (group_id, page_size, offset),
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
                grams=_decimal_or_none(row["grams"]),
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

    def delete_unlinked_recipes(self, group_id: str | None = None) -> int:
        """Delete local recipes that are not mapped to any FatSecret account."""
        if group_id is None:
            rows = self._conn.execute(
                """
                SELECT r.id
                FROM recipes r
                WHERE NOT EXISTS (
                    SELECT 1 FROM account_recipes ar WHERE ar.recipe_id = r.id
                )
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT r.id
                FROM recipes r
                WHERE r.group_id = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM account_recipes ar WHERE ar.recipe_id = r.id
                    )
                """,
                (group_id,),
            ).fetchall()
        deleted = 0
        for row in rows:
            deleted += int(self.delete_recipe(row["id"]))
        return deleted

    def food_usage_cache_is_fresh(
        self,
        group_id: str,
        max_age: dt.timedelta = dt.timedelta(days=1),
        now: dt.datetime | None = None,
    ) -> bool:
        """Return whether the FatSecret-derived food usage cache is recent enough."""
        row = self._conn.execute(
            "SELECT refreshed_at FROM food_usage_refreshes WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            refreshed_at = dt.datetime.fromisoformat(row["refreshed_at"])
        except ValueError:
            return False
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=dt.UTC)
        current = now or dt.datetime.now(dt.UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.UTC)
        return current - refreshed_at < max_age

    def replace_food_usage_cache(self, group_id: str, ingredients: list[Ingredient]) -> int:
        """Replace cached frequently used foods for a group from live FatSecret recipes."""
        aggregated: dict[tuple[str, str], tuple[Ingredient, int]] = {}
        for ingredient in ingredients:
            normalized_title = normalize_title(ingredient.title)
            if not ingredient.food_id or not normalized_title:
                continue
            key = (ingredient.food_id, normalized_title)
            stored, count = aggregated.get(key, (ingredient, 0))
            aggregated[key] = (stored, count + 1)

        now = _now()
        self._conn.execute("DELETE FROM food_usage_cache WHERE group_id = ?", (group_id,))
        for (food_id, normalized_title), (ingredient, count) in aggregated.items():
            self._conn.execute(
                """
                INSERT INTO food_usage_cache(
                    group_id, food_id, title, normalized_title, portion_id,
                    portion_description, use_count, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    food_id,
                    ingredient.title,
                    normalized_title,
                    ingredient.portion_id or "0",
                    ingredient.portion_description,
                    count,
                    now,
                ),
            )
        self._conn.execute(
            """
            INSERT INTO food_usage_refreshes(group_id, refreshed_at)
            VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET refreshed_at = excluded.refreshed_at
            """,
            (group_id, now),
        )
        self._conn.commit()
        return len(aggregated)

    def list_food_usage_cache(self, group_id: str) -> list[CachedFoodUsage]:
        """Return cached foods used in real FatSecret recipes for one group."""
        rows = self._conn.execute(
            """
            SELECT group_id, food_id, title, portion_id, portion_description, use_count
            FROM food_usage_cache
            WHERE group_id = ?
            ORDER BY use_count DESC, normalized_title ASC, food_id ASC
            """,
            (group_id,),
        ).fetchall()
        return [
            CachedFoodUsage(
                group_id=row["group_id"],
                food_id=row["food_id"],
                title=row["title"],
                portion_id=row["portion_id"],
                portion_description=row["portion_description"],
                use_count=int(row["use_count"]),
            )
            for row in rows
        ]

    def add_ingredient(
        self,
        recipe_id: str,
        food_id: str,
        title: str,
        portion_id: str,
        amount: Decimal,
        portion_description: str = "",
        grams: Decimal | None = None,
    ) -> str:
        ingredient = Ingredient(
            id=str(uuid.uuid4()),
            recipe_id=recipe_id,
            food_id=food_id,
            title=title,
            portion_id=portion_id or "0",
            amount=amount,
            portion_description=portion_description,
            grams=grams if grams is not None else _ingredient_grams(amount, portion_description),
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
                portion_description, remote_ingredient_id, grams, position
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _decimal_to_text(ingredient.grams) if ingredient.grams is not None else None,
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
