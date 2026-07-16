from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from fatsecret_bot.models import Ingredient, Recipe
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import RecipeCreateResult
from fatsecret_bot.telegram_bot import TelegramRecipeBot, _format_recipe, _recipe_actions_keyboard


def test_authorization_requires_allowlist_or_existing_registration(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "Existing")
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.allowed_user_ids = {22}

        assert bot._is_authorized(11) is True
        assert bot._is_authorized(22) is True
        assert bot._is_authorized(33) is False

        bot.allowed_user_ids = set()
        assert bot._is_authorized(11) is True
        assert bot._is_authorized(22) is False
    finally:
        storage.close()


def test_format_recipe_hides_remote_ids_and_pretty_prints_amounts() -> None:
    recipe = Recipe(
        id="local",
        title="Завтрак",
        description="Описание",
        portions=Decimal("2.0"),
        prep_time=30,
        cook_time=10,
        remote_ids={"tg1": "123"},
        steps=["Смешать", "Запечь"],
        ingredients=[
            Ingredient(
                id="i1",
                recipe_id="local",
                food_id="f1",
                title="Яичный Белок",
                portion_id="p1",
                amount=Decimal("125.250"),
                portion_description="г",
            ),
            Ingredient(
                id="i2",
                recipe_id="local",
                food_id="f2",
                title="Соус",
                portion_id="p2",
                amount=Decimal("0.060"),
                portion_description="serving",
                grams=Decimal("6"),
            ),
            Ingredient(
                id="i3",
                recipe_id="local",
                food_id="f3",
                title="Кетчуп",
                portion_id="0",
                amount=Decimal("3"),
                portion_description="100г",
            ),
        ],
    )

    text = _format_recipe(recipe)

    assert "Remote:" not in text
    assert "Порций: 2;" in text
    assert "- Яичный Белок: 125.25г" in text
    assert "- Соус: 6г" in text
    assert "- Кетчуп: 300г" in text
    assert "<b>Шаги</b>" in text
    assert "1. Смешать" in text


def test_recipe_actions_keyboard_keeps_only_recipe_actions_and_list_return() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=1, page_action="list", total_pages=3)
    rows = keyboard.inline_keyboard

    assert [button.text for button in rows[0]] == ["Синхронизировать"]
    assert [button.text for button in rows[1]] == ["Удалить в FatSecret"]
    assert [button.text for button in rows[2]] == ["К списку"]
    assert rows[2][0].callback_data == "list:1"
    flat_texts = [button.text for row in rows for button in row]
    assert "Назад" not in flat_texts
    assert "Дальше" not in flat_texts
    assert "Поиск" not in flat_texts
    assert "Создать из списка" not in flat_texts
    assert "В меню" not in flat_texts


def test_recipe_actions_keyboard_keeps_actions_without_navigation() -> None:
    keyboard = _recipe_actions_keyboard("recipe-1", page=0, page_action="list", total_pages=1)
    flat_texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Назад" not in flat_texts
    assert "Дальше" not in flat_texts
    assert "Синхронизировать" in flat_texts
    assert "Удалить в FatSecret" in flat_texts


def test_recipe_list_keyboard_keeps_recipe_buttons_navigation_and_actions_inline() -> None:
    recipes = [
        Recipe(id=f"recipe-{index}", title=f"Рецепт {index}", remote_ids={"tg1": "remote"})
        for index in range(9)
    ]
    bot = object.__new__(TelegramRecipeBot)

    keyboard = TelegramRecipeBot._recipe_list_keyboard(bot, recipes, 0, "list", {"tg1": "Каба"})
    rows = keyboard.inline_keyboard
    flat_texts = [button.text for row in rows for button in row]

    assert "Дальше" in flat_texts
    assert "1/2" not in flat_texts
    assert "Поиск" not in flat_texts
    assert "Создать из списка" not in flat_texts
    assert "Удалить несколько" in flat_texts
    assert "В меню" not in flat_texts


def test_ensure_main_keyboard_does_not_send_extra_message() -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def reply_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.sent.append(text)

    class FakeContext:
        def __init__(self) -> None:
            self.chat_data: dict[str, str] = {}

    bot = object.__new__(TelegramRecipeBot)
    message = FakeMessage()
    context = FakeContext()

    asyncio.run(TelegramRecipeBot._ensure_main_keyboard(bot, message, context))

    assert message.sent == []
    assert context.chat_data["reply_keyboard"] == "main"


def test_next_food_usage_refresh_runs_at_noon_in_bot_timezone() -> None:
    bot = object.__new__(TelegramRecipeBot)
    bot.sync_engine = type("Engine", (), {"timezone": "Europe/Minsk"})()
    timezone = ZoneInfo("Europe/Minsk")

    before_noon = TelegramRecipeBot._next_food_usage_refresh_at(
        bot,
        dt.datetime(2026, 6, 21, 11, 30, tzinfo=timezone),
    )
    after_noon = TelegramRecipeBot._next_food_usage_refresh_at(
        bot,
        dt.datetime(2026, 6, 21, 12, 1, tzinfo=timezone),
    )

    assert before_noon == dt.datetime(2026, 6, 21, 12, 0, tzinfo=timezone)
    assert after_noon == dt.datetime(2026, 6, 22, 12, 0, tzinfo=timezone)


def test_recipe_list_create_refreshes_live_titles_and_ignores_reconciled_stale_recipe(tmp_path) -> None:
    class FakeQuery:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def edit_message_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.messages.append(text)

    class FakeContext:
        def __init__(self) -> None:
            self.user_data = {
                "recipe_list_title": "Блины тонкие",
                "group_id": "group",
                "recipe_list_draft": [object()],
                "recipe_list_unresolved": [],
                "recipe_list_portions": Decimal("1"),
                "recipe_list_steps": [],
            }
            self.chat_data: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, storage: Storage) -> None:
            self.storage = storage
            self.created_titles: list[str] = []

        async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
            self.storage.reconcile_group_remote_recipes(group_id, {"tg11": set()})
            return []

        async def create_recipe_from_list(self, group_id: str, title: str, items, updated_by: int, **kwargs):  # noqa: ANN001, ANN003
            self.created_titles.append(title)
            recipe_id = self.storage.create_recipe(title, "", Decimal("1"), 0, 0, updated_by, group_id)
            return RecipeCreateResult(recipe_id=recipe_id, results=[], title=title)

    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        stale_id = storage.create_recipe("Блины тонкие", "", Decimal("1"), 0, 0, updated_by=11, group_id="group")
        storage.set_remote_recipe_id(stale_id, "tg11", "old-111", last_synced_version=1)
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = FakeEngine(storage)
        query = FakeQuery()
        context = FakeContext()

        asyncio.run(TelegramRecipeBot._create_recipe_list_from_draft(bot, query, context, 11))

        assert bot.sync_engine.created_titles == ["Блины тонкие"]
        assert storage.get_recipe(stale_id) is None
        assert all("Рецепт с таким названием уже есть" not in message for message in query.messages)
    finally:
        storage.close()


def test_recipe_list_create_still_prompts_for_current_live_duplicate(tmp_path) -> None:
    class FakeQuery:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def edit_message_text(self, text: str, **kwargs) -> None:  # noqa: ANN003
            self.messages.append(text)

    class FakeContext:
        def __init__(self) -> None:
            self.user_data = {
                "recipe_list_title": "Блины тонкие",
                "group_id": "group",
                "recipe_list_draft": [object()],
                "recipe_list_unresolved": [],
                "recipe_list_portions": Decimal("1"),
                "recipe_list_steps": [],
            }
            self.chat_data: dict[str, object] = {}

    class FakeEngine:
        async def load_remote_recipe_index(self, group_id: str) -> list[Recipe]:
            return [Recipe(id="live", title="Блины тонкие", group_id=group_id, remote_ids={"tg11": "111"})]

        async def create_recipe_from_list(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("duplicate must block creation")

    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage
        bot.sync_engine = FakeEngine()
        query = FakeQuery()
        context = FakeContext()

        asyncio.run(TelegramRecipeBot._create_recipe_list_from_draft(bot, query, context, 11))

        assert query.messages[-1].startswith("Рецепт с таким названием уже есть")
    finally:
        storage.close()


def test_accounts_keyboard_and_lookup_allow_only_owner_account_actions(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    try:
        storage.register_user(11, "One")
        group = storage.create_group(11, "Семья")
        storage.upsert_fatsecret_account(11, "Каба", "one@example.com", "secret", "BY", "ru")
        storage.register_user(22, "Two")
        storage.join_group_by_code(22, group.invite_code)
        storage.upsert_fatsecret_account(22, "Света", "two@example.com", "secret", "BY", "ru")
        bot = object.__new__(TelegramRecipeBot)
        bot.storage = storage

        keyboard = TelegramRecipeBot._accounts_keyboard(bot, 22, group)
        flat_texts = [button.text for row in keyboard.inline_keyboard for button in row]
        _, own_account = TelegramRecipeBot._active_group_account(bot, 22, "tg22")
        _, other_account = TelegramRecipeBot._active_group_account(bot, 22, "tg11")

        assert "Поменять ник: Света" in flat_texts
        assert "Выйти: Света" in flat_texts
        assert "Поменять ник: Каба" not in flat_texts
        assert "Выйти: Каба" not in flat_texts
        assert own_account is not None
        assert other_account is None
    finally:
        storage.close()
