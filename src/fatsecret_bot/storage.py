from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sqlite3
import string
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import (
    CachedFoodUsage,
    MAX_RECIPE_STEPS,
    FatSecretAccountConfig,
    FatSecretSession,
    Ingredient,
    Recipe,
    RecipeFingerprint,
    RecipeGroup,
    RecipeGroupMember,
    RecipeSummary,
)
from .portions import grams_from_portion, is_bare_weight_portion


INVITE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
STORAGE_SCHEMA_VERSION = 2
DIARY_COPY_STALE_AFTER = dt.timedelta(minutes=30)


def normalize_title(title: str) -> str:
    return " ".join(title.casefold().strip().split())


def _now() -> str:
    return _timestamp()


def _timestamp(value: dt.datetime | None = None) -> str:
    current = value or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.UTC)
    return current.astimezone(dt.UTC).isoformat()


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


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _recipe_snapshot_json(recipe: Recipe) -> str:
    payload: dict[str, Any] = {
        "title": recipe.title,
        "description": recipe.description,
        "portions": str(recipe.portions),
        "prep_time": recipe.prep_time,
        "cook_time": recipe.cook_time,
        "steps": list(recipe.steps),
        "default_portion_id": recipe.default_portion_id,
        "default_portion_description": recipe.default_portion_description,
        "ingredients": [
            {
                "id": ingredient.id,
                "food_id": ingredient.food_id,
                "title": ingredient.title,
                "portion_id": ingredient.portion_id,
                "amount": str(ingredient.amount),
                "portion_description": ingredient.portion_description,
                "remote_ingredient_id": ingredient.remote_ingredient_id,
                "grams": str(ingredient.grams) if ingredient.grams is not None else None,
            }
            for ingredient in recipe.ingredients
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _recipe_from_snapshot_json(
    snapshot_json: str,
    account_key: str,
    remote_recipe_id: str,
) -> Recipe | None:
    try:
        payload = json.loads(snapshot_json)
        if not isinstance(payload, dict):
            return None
        recipe = Recipe(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fatsecret-snapshot:{account_key}:{remote_recipe_id}")),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            portions=Decimal(str(payload.get("portions") or "1")),
            prep_time=int(payload.get("prep_time") or 0),
            cook_time=int(payload.get("cook_time") or 0),
            steps=[str(step) for step in payload.get("steps") or [] if str(step).strip()],
            default_portion_id=str(payload.get("default_portion_id") or "0"),
            default_portion_description=str(payload.get("default_portion_description") or ""),
            remote_ids={account_key: remote_recipe_id},
            remote_ids_by_account={account_key: [remote_recipe_id]},
        )
        for position, item in enumerate(payload.get("ingredients") or []):
            if not isinstance(item, dict):
                continue
            recipe.ingredients.append(
                Ingredient(
                    id=str(item.get("id") or f"snapshot-{position}"),
                    recipe_id=recipe.id,
                    food_id=str(item.get("food_id") or ""),
                    title=str(item.get("title") or ""),
                    portion_id=str(item.get("portion_id") or "0"),
                    amount=Decimal(str(item.get("amount") or "0")),
                    portion_description=str(item.get("portion_description") or ""),
                    remote_ingredient_id=(
                        str(item["remote_ingredient_id"])
                        if item.get("remote_ingredient_id") is not None
                        else None
                    ),
                    grams=(Decimal(str(item["grams"])) if item.get("grams") is not None else None),
                )
            )
        return recipe
    except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
        return None
def _new_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        if os.name != "nt":
            self.path.chmod(0o600)
        # Storage is intentionally used synchronously from PTB's single event-loop
        # thread. Every public mutation commits before returning and must never
        # retain a transaction across an await boundary.
        self._conn = sqlite3.connect(self.path, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
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

            CREATE TABLE IF NOT EXISTS group_accounts (
                group_id TEXT NOT NULL REFERENCES recipe_groups(id) ON DELETE CASCADE,
                account_key TEXT NOT NULL UNIQUE,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (group_id, account_key)
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

            CREATE TABLE IF NOT EXISTS custom_food_mappings (
                source_account_key TEXT NOT NULL,
                source_food_id TEXT NOT NULL,
                target_account_key TEXT NOT NULL,
                target_food_id TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_account_key, source_food_id, target_account_key)
            );

            CREATE TABLE IF NOT EXISTS remote_recipe_snapshots (
                account_key TEXT NOT NULL,
                remote_recipe_id TEXT NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                snapshot_json TEXT,
                fingerprint TEXT,
                seen_at TEXT NOT NULL,
                fetched_at TEXT,
                PRIMARY KEY (account_key, remote_recipe_id)
            );

            CREATE TABLE IF NOT EXISTS recipe_swap_runs (
                id TEXT PRIMARY KEY,
                recipe_id TEXT NOT NULL,
                account_key TEXT NOT NULL,
                old_remote_id TEXT NOT NULL,
                new_remote_id TEXT,
                temporary_title TEXT NOT NULL,
                final_title TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS diary_copy_runs (
                id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                initiated_by INTEGER NOT NULL,
                source_account_key TEXT NOT NULL,
                source_date TEXT NOT NULL,
                target_start TEXT NOT NULL,
                target_end TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_diary_copy_runs_group_created "
            "ON diary_copy_runs(group_id, created_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_recipe_snapshots_title "
            "ON remote_recipe_snapshots(normalized_title, account_key)"
        )
        self._backfill_default_group()
        current_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version < 1:
            self._normalize_zero_portion_gram_ingredients()
            self._backfill_ingredient_grams()
        if current_version < 2:
            self._migrate_multi_account_schema()
        self._conn.execute(f"PRAGMA user_version = {STORAGE_SCHEMA_VERSION}")
        self._conn.commit()

    def _migrate_multi_account_schema(self) -> None:
        """Replace the one-account-per-user table and attach accounts to their active groups."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(fatsecret_accounts)").fetchall()
        }
        if "owner_telegram_id" not in columns:
            self._conn.executescript(
                """
                CREATE TABLE fatsecret_accounts_v2 (
                    account_key TEXT PRIMARY KEY,
                    owner_telegram_id INTEGER NOT NULL,
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
                INSERT INTO fatsecret_accounts_v2(
                    account_key, owner_telegram_id, label, username, password, market, language,
                    session_server_id, session_device_key, session_secret_key, session_updated_at,
                    created_at, updated_at
                )
                SELECT
                    account_key, telegram_id, label, username, password, market, language,
                    session_server_id, session_device_key, session_secret_key, session_updated_at,
                    created_at, updated_at
                FROM fatsecret_accounts;
                DROP TABLE fatsecret_accounts;
                ALTER TABLE fatsecret_accounts_v2 RENAME TO fatsecret_accounts;
                """
            )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO group_accounts(group_id, account_key, added_by, added_at)
            SELECT u.active_group_id, fa.account_key, fa.owner_telegram_id, ?
            FROM fatsecret_accounts fa
            JOIN telegram_users u ON u.telegram_id = fa.owner_telegram_id
            JOIN group_members gm
                ON gm.telegram_id = fa.owner_telegram_id AND gm.group_id = u.active_group_id
            WHERE u.active_group_id IS NOT NULL
            """,
            (_now(),),
        )

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
            if not is_bare_weight_portion(row["portion_description"]):
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
            grams = grams_from_portion(amount, row["portion_description"])
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
                FROM group_accounts
                WHERE group_id = ?
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
                JOIN group_accounts ga ON ga.account_key = fa.account_key
                WHERE ga.group_id = ?
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
        """Return the first FatSecret account owned by a Telegram user, if any."""
        row = self._conn.execute(
            """
            SELECT account_key, label, username, password, market, language
            FROM fatsecret_accounts
            WHERE owner_telegram_id = ?
            ORDER BY created_at ASC, account_key ASC
            LIMIT 1
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

    def list_fatsecret_accounts_for_owner(self, telegram_id: int) -> list[FatSecretAccountConfig]:
        """Return every FatSecret account whose credentials are owned by one Telegram user."""
        rows = self._conn.execute(
            """
            SELECT account_key, label, username, password, market, language
            FROM fatsecret_accounts
            WHERE owner_telegram_id = ?
            ORDER BY created_at ASC, account_key ASC
            """,
            (telegram_id,),
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

    def fatsecret_account_owner(self, account_key: str) -> int | None:
        """Return the Telegram owner of one FatSecret account."""
        row = self._conn.execute(
            "SELECT owner_telegram_id FROM fatsecret_accounts WHERE account_key = ?",
            (account_key,),
        ).fetchone()
        return int(row["owner_telegram_id"]) if row is not None else None

    def fatsecret_account_group_id(self, account_key: str) -> str | None:
        """Return the single group to which a FatSecret account is attached."""
        row = self._conn.execute(
            "SELECT group_id FROM group_accounts WHERE account_key = ?",
            (account_key,),
        ).fetchone()
        return str(row["group_id"]) if row is not None else None

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
        """Return Telegram members with labels of their accounts attached to this group."""
        rows = self._conn.execute(
            """
            SELECT
                u.telegram_id,
                u.display_name,
                GROUP_CONCAT(CASE WHEN ga.account_key IS NOT NULL THEN fa.label END, ', ') AS fatsecret_label,
                GROUP_CONCAT(CASE WHEN ga.account_key IS NOT NULL THEN fa.username END, ', ') AS fatsecret_username
            FROM group_members gm
            JOIN telegram_users u ON u.telegram_id = gm.telegram_id
            LEFT JOIN fatsecret_accounts fa ON fa.owner_telegram_id = u.telegram_id
            LEFT JOIN group_accounts ga
                ON ga.account_key = fa.account_key AND ga.group_id = gm.group_id
            WHERE gm.group_id = ?
            GROUP BY u.telegram_id, u.display_name
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
        """Create the first owned account or update that legacy account in place."""
        existing = self._conn.execute(
            """
            SELECT account_key FROM fatsecret_accounts
            WHERE owner_telegram_id = ?
            ORDER BY created_at ASC, account_key ASC
            LIMIT 1
            """,
            (telegram_id,),
        ).fetchone()
        if existing is None:
            return self.create_fatsecret_account(
                telegram_id,
                label,
                username,
                password,
                market,
                language,
            )
        account_key = str(existing["account_key"])
        now = _now()
        self._conn.execute(
            """
            UPDATE fatsecret_accounts
            SET label = ?, username = ?, password = ?, market = ?, language = ?,
                session_server_id = NULL,
                session_device_key = NULL,
                session_secret_key = NULL,
                session_updated_at = NULL,
                updated_at = ?
            WHERE account_key = ?
            """,
            (label, username, password, market, language, now, account_key),
        )
        self._conn.commit()
        return account_key

    def create_fatsecret_account(
        self,
        telegram_id: int,
        label: str,
        username: str,
        password: str,
        market: str,
        language: str,
        *,
        group_id: str | None = None,
    ) -> str:
        """Create another owned FatSecret account and attach it to at most one group."""
        owned_count = int(
            self._conn.execute(
                "SELECT COUNT(*) AS c FROM fatsecret_accounts WHERE owner_telegram_id = ?",
                (telegram_id,),
            ).fetchone()["c"]
        )
        account_key = f"tg{telegram_id}" if owned_count == 0 else f"tg{telegram_id}-{uuid.uuid4().hex[:8]}"
        now = _now()
        self._conn.execute(
            """
            INSERT INTO fatsecret_accounts(
                account_key, owner_telegram_id, label, username, password, market,
                language, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (account_key, telegram_id, label, username, password, market, language, now, now),
        )
        target_group_id = group_id
        if target_group_id is None:
            row = self._conn.execute(
                "SELECT active_group_id FROM telegram_users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            target_group_id = str(row["active_group_id"]) if row is not None and row["active_group_id"] else None
        if target_group_id is not None:
            member = self._conn.execute(
                "SELECT 1 FROM group_members WHERE group_id = ? AND telegram_id = ?",
                (target_group_id, telegram_id),
            ).fetchone()
            if member is None:
                self._conn.rollback()
                raise ValueError("FatSecret account owner is not a member of the target group")
            self._conn.execute(
                """
                INSERT INTO group_accounts(group_id, account_key, added_by, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (target_group_id, account_key, telegram_id, now),
            )
        self._conn.commit()
        return account_key

    def attach_fatsecret_account_to_group(
        self,
        account_key: str,
        group_id: str,
        telegram_id: int,
    ) -> bool:
        """Attach an owned account to one joined group; an account cannot belong to two groups."""
        if self.fatsecret_account_owner(account_key) != telegram_id:
            return False
        member = self._conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND telegram_id = ?",
            (group_id, telegram_id),
        ).fetchone()
        if member is None or self.fatsecret_account_group_id(account_key) is not None:
            return False
        self._conn.execute(
            """
            INSERT INTO group_accounts(group_id, account_key, added_by, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, account_key, telegram_id, _now()),
        )
        self._conn.commit()
        return True

    def detach_fatsecret_account_from_group(
        self,
        account_key: str,
        group_id: str,
        telegram_id: int,
    ) -> bool:
        """Detach an account when requested by its owner or the group creator."""
        owner = self.fatsecret_account_owner(account_key)
        creator = self._conn.execute(
            "SELECT created_by FROM recipe_groups WHERE id = ?",
            (group_id,),
        ).fetchone()
        if owner != telegram_id and (creator is None or int(creator["created_by"]) != telegram_id):
            return False
        cursor = self._conn.execute(
            "DELETE FROM group_accounts WHERE group_id = ? AND account_key = ?",
            (group_id, account_key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def update_fatsecret_account_label(
        self,
        account_key: str,
        label: str,
        *,
        owner_telegram_id: int,
    ) -> bool:
        """Update an owned account nickname after enforcing credential ownership."""
        clean_label = label.strip()[:32]
        if not clean_label:
            return False
        cursor = self._conn.execute(
            """
            UPDATE fatsecret_accounts
            SET label = ?, updated_at = ?
            WHERE account_key = ? AND owner_telegram_id = ?
            """,
            (clean_label, _now(), account_key, owner_telegram_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_fatsecret_account_for_user(self, telegram_id: int) -> bool:
        """Delete the only account owned by a user; refuse an ambiguous multi-account delete."""
        rows = self._conn.execute(
            "SELECT account_key FROM fatsecret_accounts WHERE owner_telegram_id = ?",
            (telegram_id,),
        ).fetchall()
        if len(rows) != 1:
            return False
        return self.delete_fatsecret_account(str(rows[0]["account_key"]), owner_telegram_id=telegram_id)

    def delete_fatsecret_account(self, account_key: str, *, owner_telegram_id: int | None = None) -> bool:
        """Delete a selected owned account and all bot-side account metadata."""
        row = self._conn.execute(
            "SELECT owner_telegram_id FROM fatsecret_accounts WHERE account_key = ?",
            (account_key,),
        ).fetchone()
        if row is None or (owner_telegram_id is not None and int(row["owner_telegram_id"]) != owner_telegram_id):
            return False
        self._conn.execute("DELETE FROM group_accounts WHERE account_key = ?", (account_key,))
        self._conn.execute("DELETE FROM account_recipes WHERE account_key = ?", (account_key,))
        self._conn.execute("DELETE FROM remote_recipe_snapshots WHERE account_key = ?", (account_key,))
        self._conn.execute("DELETE FROM recipe_swap_runs WHERE account_key = ?", (account_key,))
        self._conn.execute(
            "DELETE FROM custom_food_mappings WHERE source_account_key = ? OR target_account_key = ?",
            (account_key, account_key),
        )
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
        recipe.remote_ids_by_account = {account_key: [remote_id] for account_key, remote_id in recipe.remote_ids.items()}
        return recipe

    def find_recipe_by_title(self, group_id: str | None, title: str) -> Recipe | None:
        """Return the local recipe with the same normalized title inside one group."""
        normalized = normalize_title(title)
        row = self._conn.execute(
            """
            SELECT id
            FROM recipes
            WHERE normalized_title = ?
                AND (group_id = ? OR (group_id IS NULL AND ? IS NULL))
            LIMIT 1
            """,
            (normalized, group_id, group_id),
        ).fetchone()
        if row is None:
            return None
        return self.get_recipe(row["id"])

    def next_available_recipe_title(self, group_id: str | None, title: str, *, include_base: bool = True) -> str:
        """Return title or title with a numeric suffix that does not collide locally."""
        base_title = title.strip() or "Рецепт"
        suffix = 2
        candidate = base_title if include_base else f"{base_title} {suffix}"
        if not include_base:
            suffix += 1
        while self.find_recipe_by_title(group_id, candidate) is not None:
            candidate = f"{base_title} {suffix}"
            suffix += 1
        return candidate

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
        self._delete_recipe_rows(recipe_id)
        self._conn.commit()
        return True

    def _delete_recipe_rows(self, recipe_id: str) -> None:
        self._conn.execute("DELETE FROM ingredients WHERE recipe_id = ?", (recipe_id,))
        self._conn.execute("DELETE FROM account_recipes WHERE recipe_id = ?", (recipe_id,))
        self._conn.execute("DELETE FROM sync_events WHERE recipe_id = ?", (recipe_id,))
        self._conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))

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
        normalized_portion_id = portion_id or "0"
        resolved_grams = grams if grams is not None else grams_from_portion(amount, portion_description)
        if normalized_portion_id == "0" and is_bare_weight_portion(portion_description):
            amount = (resolved_grams if resolved_grams is not None else amount) / Decimal("100")
            portion_description = "100г"
        ingredient = Ingredient(
            id=str(uuid.uuid4()),
            recipe_id=recipe_id,
            food_id=food_id,
            title=title,
            portion_id=normalized_portion_id,
            amount=amount,
            portion_description=portion_description,
            grams=resolved_grams,
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

    def remove_remote_recipe_mapping(self, account_key: str, remote_recipe_id: str) -> bool:
        """Remove a mapping by its FatSecret identity and delete a recipe that becomes unlinked."""
        rows = self._conn.execute(
            "SELECT recipe_id FROM account_recipes WHERE account_key = ? AND remote_recipe_id = ?",
            (account_key, remote_recipe_id),
        ).fetchall()
        if not rows:
            return False
        recipe_ids = {row["recipe_id"] for row in rows}
        self._conn.execute(
            "DELETE FROM account_recipes WHERE account_key = ? AND remote_recipe_id = ?",
            (account_key, remote_recipe_id),
        )
        for recipe_id in recipe_ids:
            remaining = self._conn.execute(
                "SELECT 1 FROM account_recipes WHERE recipe_id = ? LIMIT 1",
                (recipe_id,),
            ).fetchone()
            if remaining is None:
                self._delete_recipe_rows(recipe_id)
        self._conn.commit()
        return True

    def reconcile_group_remote_recipes(
        self,
        group_id: str,
        live_remote_ids_by_account: dict[str, set[str]],
    ) -> int:
        """Remove stale mappings using one complete live cookbook snapshot for a group."""
        if not live_remote_ids_by_account:
            return 0
        rows = self._conn.execute(
            """
            SELECT ar.recipe_id, ar.account_key, ar.remote_recipe_id
            FROM account_recipes ar
            JOIN recipes r ON r.id = ar.recipe_id
            WHERE r.group_id = ?
            """,
            (group_id,),
        ).fetchall()
        stale = [
            row
            for row in rows
            if row["account_key"] in live_remote_ids_by_account
            and row["remote_recipe_id"] not in live_remote_ids_by_account[row["account_key"]]
        ]
        if not stale:
            return 0
        affected_recipe_ids = {row["recipe_id"] for row in stale}
        self._conn.executemany(
            "DELETE FROM account_recipes WHERE recipe_id = ? AND account_key = ? AND remote_recipe_id = ?",
            [(row["recipe_id"], row["account_key"], row["remote_recipe_id"]) for row in stale],
        )
        for recipe_id in affected_recipe_ids:
            remaining = self._conn.execute(
                "SELECT 1 FROM account_recipes WHERE recipe_id = ? LIMIT 1",
                (recipe_id,),
            ).fetchone()
            if remaining is None:
                self._delete_recipe_rows(recipe_id)
        self._conn.commit()
        return len(stale)

    def upsert_remote_recipe_summary(
        self,
        account_key: str,
        remote_recipe_id: str,
        title: str,
        *,
        seen_at: dt.datetime | None = None,
    ) -> None:
        """Record that one remote recipe identity appeared in an authoritative cookbook."""
        self._conn.execute(
            """
            INSERT INTO remote_recipe_snapshots(
                account_key, remote_recipe_id, title, normalized_title,
                snapshot_json, fingerprint, seen_at, fetched_at
            )
            VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)
            ON CONFLICT(account_key, remote_recipe_id) DO UPDATE SET
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                seen_at = excluded.seen_at
            """,
            (
                account_key,
                remote_recipe_id,
                title,
                normalize_title(title),
                _timestamp(seen_at),
            ),
        )
        self._conn.commit()

    def upsert_remote_recipe_snapshot(
        self,
        account_key: str,
        remote_recipe_id: str,
        recipe: Recipe,
        fingerprint: RecipeFingerprint,
        *,
        fetched_at: dt.datetime | None = None,
    ) -> None:
        """Persist one fully hydrated account-specific remote recipe version."""
        timestamp = _timestamp(fetched_at)
        self._conn.execute(
            """
            INSERT INTO remote_recipe_snapshots(
                account_key, remote_recipe_id, title, normalized_title,
                snapshot_json, fingerprint, seen_at, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_key, remote_recipe_id) DO UPDATE SET
                title = excluded.title,
                normalized_title = excluded.normalized_title,
                snapshot_json = excluded.snapshot_json,
                fingerprint = excluded.fingerprint,
                seen_at = excluded.seen_at,
                fetched_at = excluded.fetched_at
            """,
            (
                account_key,
                remote_recipe_id,
                recipe.title,
                normalize_title(recipe.title),
                _recipe_snapshot_json(recipe),
                fingerprint.digest,
                timestamp,
                timestamp,
            ),
        )
        self._conn.commit()

    def remote_recipe_snapshot(
        self,
        account_key: str,
        remote_recipe_id: str,
    ) -> tuple[Recipe, str] | None:
        """Return a hydrated stored snapshot and its fingerprint digest, if available."""
        row = self._conn.execute(
            """
            SELECT snapshot_json, fingerprint
            FROM remote_recipe_snapshots
            WHERE account_key = ? AND remote_recipe_id = ?
            """,
            (account_key, remote_recipe_id),
        ).fetchone()
        if row is None or not row["snapshot_json"] or not row["fingerprint"]:
            return None
        recipe = _recipe_from_snapshot_json(
            str(row["snapshot_json"]),
            account_key,
            remote_recipe_id,
        )
        if recipe is None:
            return None
        return recipe, str(row["fingerprint"])

    def remote_recipe_snapshots_by_title(
        self,
        title: str,
        *,
        account_keys: set[str] | None = None,
    ) -> list[tuple[str, str, Recipe, str]]:
        """Return every hydrated account-specific snapshot for one normalized title."""
        rows = self._conn.execute(
            """
            SELECT account_key, remote_recipe_id, snapshot_json, fingerprint
            FROM remote_recipe_snapshots
            WHERE normalized_title = ? AND snapshot_json IS NOT NULL AND fingerprint IS NOT NULL
            ORDER BY account_key, remote_recipe_id
            """,
            (normalize_title(title),),
        ).fetchall()
        result: list[tuple[str, str, Recipe, str]] = []
        for row in rows:
            account_key = str(row["account_key"])
            if account_keys is not None and account_key not in account_keys:
                continue
            recipe = _recipe_from_snapshot_json(
                str(row["snapshot_json"]),
                account_key,
                str(row["remote_recipe_id"]),
            )
            if recipe is not None:
                result.append(
                    (
                        account_key,
                        str(row["remote_recipe_id"]),
                        recipe,
                        str(row["fingerprint"]),
                    )
                )
        return result

    def reconcile_remote_recipe_snapshots(
        self,
        account_key: str,
        live_remote_ids: set[str],
    ) -> int:
        """Delete snapshot rows absent from one successfully loaded authoritative cookbook."""
        rows = self._conn.execute(
            "SELECT remote_recipe_id FROM remote_recipe_snapshots WHERE account_key = ?",
            (account_key,),
        ).fetchall()
        stale_ids = [
            str(row["remote_recipe_id"])
            for row in rows
            if str(row["remote_recipe_id"]) not in live_remote_ids
        ]
        if stale_ids:
            self._conn.executemany(
                "DELETE FROM remote_recipe_snapshots WHERE account_key = ? AND remote_recipe_id = ?",
                [(account_key, remote_id) for remote_id in stale_ids],
            )
            self._conn.commit()
        return len(stale_ids)

    def create_recipe_swap_run(
        self,
        recipe_id: str,
        account_key: str,
        old_remote_id: str,
        temporary_title: str,
        final_title: str,
    ) -> str:
        """Persist the start of a durable create-and-swap recipe replacement."""
        run_id = uuid.uuid4().hex
        now = _now()
        self._conn.execute(
            """
            INSERT INTO recipe_swap_runs(
                id, recipe_id, account_key, old_remote_id, new_remote_id,
                temporary_title, final_title, status, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, 'creating', NULL, ?, ?)
            """,
            (
                run_id,
                recipe_id,
                account_key,
                old_remote_id,
                temporary_title,
                final_title,
                now,
                now,
            ),
        )
        self._conn.commit()
        return run_id

    def update_recipe_swap_run(
        self,
        run_id: str,
        status: str,
        *,
        new_remote_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Update the recoverable progress marker for one recipe swap."""
        cursor = self._conn.execute(
            """
            UPDATE recipe_swap_runs
            SET status = ?,
                new_remote_id = COALESCE(?, new_remote_id),
                error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, new_remote_id, error, _now(), run_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def incomplete_recipe_swap_runs(self) -> list[dict[str, str | None]]:
        """Return non-terminal recipe swaps that startup recovery must inspect."""
        rows = self._conn.execute(
            """
            SELECT * FROM recipe_swap_runs
            WHERE status NOT IN ('completed', 'rolled_back', 'failed')
            ORDER BY created_at ASC
            """
        ).fetchall()
        return [{key: (str(row[key]) if row[key] is not None else None) for key in row.keys()} for row in rows]

    def complete_recipe_swap(
        self,
        run_id: str,
        recipe_id: str,
        account_key: str,
        remote_recipe_id: str,
        version: int,
        recipe: Recipe,
        fingerprint: RecipeFingerprint,
    ) -> None:
        """Atomically replace a mapping, store its verified snapshot, and finish the swap."""
        now = _now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self.set_remote_recipe_id(recipe_id, account_key, remote_recipe_id, version)
            self._conn.execute(
                """
                INSERT INTO remote_recipe_snapshots(
                    account_key, remote_recipe_id, title, normalized_title,
                    snapshot_json, fingerprint, seen_at, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key, remote_recipe_id) DO UPDATE SET
                    title = excluded.title,
                    normalized_title = excluded.normalized_title,
                    snapshot_json = excluded.snapshot_json,
                    fingerprint = excluded.fingerprint,
                    seen_at = excluded.seen_at,
                    fetched_at = excluded.fetched_at
                """,
                (
                    account_key,
                    remote_recipe_id,
                    recipe.title,
                    normalize_title(recipe.title),
                    _recipe_snapshot_json(recipe),
                    fingerprint.digest,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                UPDATE recipe_swap_runs
                SET status = 'completed', new_remote_id = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (remote_recipe_id, now, run_id),
            )
            old_row = self._conn.execute(
                "SELECT old_remote_id FROM recipe_swap_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if old_row is not None and str(old_row["old_remote_id"]) != remote_recipe_id:
                self._conn.execute(
                    "DELETE FROM remote_recipe_snapshots WHERE account_key = ? AND remote_recipe_id = ?",
                    (account_key, str(old_row["old_remote_id"])),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def local_recipe_id_for_remote(self, account_key: str, remote_recipe_id: str) -> str | None:
        """Return the local recipe id mapped to one account-specific FatSecret id."""
        row = self._conn.execute(
            "SELECT recipe_id FROM account_recipes WHERE account_key = ? AND remote_recipe_id = ? LIMIT 1",
            (account_key, remote_recipe_id),
        ).fetchone()
        return str(row["recipe_id"]) if row is not None else None

    def custom_food_mapping(
        self,
        source_account_key: str,
        source_food_id: str,
        target_account_key: str,
    ) -> str | None:
        """Return a personal-food counterpart in either mapping direction."""
        row = self._conn.execute(
            """
            SELECT target_food_id
            FROM custom_food_mappings
            WHERE source_account_key = ? AND source_food_id = ? AND target_account_key = ?
            """,
            (source_account_key, source_food_id, target_account_key),
        ).fetchone()
        if row is not None:
            return str(row["target_food_id"])
        reverse = self._conn.execute(
            """
            SELECT source_food_id
            FROM custom_food_mappings
            WHERE source_account_key = ? AND target_account_key = ? AND target_food_id = ?
            LIMIT 1
            """,
            (target_account_key, source_account_key, source_food_id),
        ).fetchone()
        return str(reverse["source_food_id"]) if reverse is not None else None

    def set_custom_food_mapping(
        self,
        source_account_key: str,
        source_food_id: str,
        target_account_key: str,
        target_food_id: str,
        content_hash: str = "",
    ) -> None:
        """Persist the account-to-account mapping for one cloned personal food."""
        now = _now()
        self._conn.execute(
            """
            INSERT INTO custom_food_mappings(
                source_account_key, source_food_id, target_account_key, target_food_id,
                content_hash, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_account_key, source_food_id, target_account_key) DO UPDATE SET
                target_food_id = excluded.target_food_id,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at
            """,
            (
                source_account_key,
                source_food_id,
                target_account_key,
                target_food_id,
                content_hash,
                now,
                now,
            ),
        )
        self._conn.commit()

    def delete_custom_food_mapping(
        self,
        source_account_key: str,
        source_food_id: str,
        target_account_key: str,
    ) -> bool:
        """Remove one stale personal-food mapping before recreating its target food."""
        cursor = self._conn.execute(
            """
            DELETE FROM custom_food_mappings
            WHERE source_account_key = ? AND source_food_id = ? AND target_account_key = ?
            """,
            (source_account_key, source_food_id, target_account_key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def create_diary_copy_run(
        self,
        group_id: str,
        initiated_by: int,
        source_account_key: str,
        source_date: dt.date,
        target_start: dt.date,
        target_end: dt.date,
        request: dict[str, object],
    ) -> str:
        """Create a pending, persistent diary copy operation and return its id."""
        run_id = uuid.uuid4().hex
        now = _now()
        self._conn.execute(
            """
            INSERT INTO diary_copy_runs(
                id, group_id, initiated_by, source_account_key, source_date,
                target_start, target_end, request_json, status, result_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
            """,
            (
                run_id,
                group_id,
                initiated_by,
                source_account_key,
                source_date.isoformat(),
                target_start.isoformat(),
                target_end.isoformat(),
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )
        self._conn.commit()
        return run_id

    def diary_copy_run(self, run_id: str) -> dict[str, object] | None:
        """Load one diary copy run including its immutable request and stored result."""
        row = self._conn.execute("SELECT * FROM diary_copy_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "group_id": str(row["group_id"]),
            "initiated_by": int(row["initiated_by"]),
            "source_account_key": str(row["source_account_key"]),
            "source_date": str(row["source_date"]),
            "target_start": str(row["target_start"]),
            "target_end": str(row["target_end"]),
            "request": json.loads(row["request_json"]),
            "status": str(row["status"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def claim_diary_copy_run(
        self,
        run_id: str,
        *,
        stale_after: dt.timedelta = DIARY_COPY_STALE_AFTER,
        now: dt.datetime | None = None,
    ) -> bool:
        """Claim a pending run or atomically reclaim a stale running operation."""
        if stale_after <= dt.timedelta(0):
            raise ValueError("stale_after must be positive")
        current = now or dt.datetime.now(dt.UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.UTC)
        current = current.astimezone(dt.UTC)
        stale_before = current - stale_after
        cursor = self._conn.execute(
            """
            UPDATE diary_copy_runs
            SET status = 'running', updated_at = ?
            WHERE id = ?
                AND (
                    status = 'pending'
                    OR (status = 'running' AND updated_at <= ?)
                )
            """,
            (_timestamp(current), run_id, _timestamp(stale_before)),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def touch_diary_copy_run(self, run_id: str, *, now: dt.datetime | None = None) -> bool:
        """Refresh the heartbeat of a currently running diary copy operation."""
        cursor = self._conn.execute(
            "UPDATE diary_copy_runs SET updated_at = ? WHERE id = ? AND status = 'running'",
            (_timestamp(now), run_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def finish_diary_copy_run(self, run_id: str, status: str, result: dict[str, object]) -> None:
        """Persist the terminal result of a diary copy operation."""
        if status not in {"completed", "partial", "failed"}:
            raise ValueError(f"Unsupported diary copy status: {status}")
        self._conn.execute(
            """
            UPDATE diary_copy_runs
            SET status = ?, result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                _now(),
                run_id,
            ),
        )
        self._conn.commit()

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
