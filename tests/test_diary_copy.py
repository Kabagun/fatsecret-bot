from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx

from fatsecret_bot.fatsecret_client import DIARY_BULK_UPDATE_URL, FatSecretClient, FatSecretError
from fatsecret_bot.models import (
    CustomFoodDefinition,
    FatSecretAccountConfig,
    FatSecretDeviceConfig,
    FatSecretSession,
    FoodDiaryBulkResult,
    FoodDiaryDay,
    FoodDiaryEntry,
    FoodDiaryWriteEntry,
    Recipe,
    RecipeSummary,
)
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import RecipeSyncEngine
from fatsecret_bot.telegram_bot import _parse_diary_date, _parse_diary_range


def _device() -> FatSecretDeviceConfig:
    return FatSecretDeviceConfig(
        app_version="11.5.0.4",
        device="6",
        build_sdk="30",
        build_api="11",
        build_model="NE2211",
        build_resolution="1920x1080",
        device_identifier="NE2211",
    )


def _account(key: str) -> FatSecretAccountConfig:
    return FatSecretAccountConfig(key, key, f"{key}@example.com", "secret", "BY", "ru")


class FakeDiaryClient:
    def __init__(self, account: FatSecretAccountConfig, source_day: FoodDiaryDay | None = None) -> None:
        self.account = account
        self.source_day = source_day
        self.bulk_calls: list[tuple[dt.date, list[FoodDiaryWriteEntry]]] = []
        self.custom_definition: CustomFoodDefinition | None = None
        self.created_custom_foods: list[CustomFoodDefinition] = []
        self.recipes: dict[str, Recipe] = {}
        self.created_recipe_id = "202"

    async def get_food_diary_day(self, date: dt.date) -> FoodDiaryDay:
        assert self.source_day is not None
        assert date == self.source_day.date
        return self.source_day

    async def bulk_update_food_diary(
        self,
        date: dt.date,
        entries: list[FoodDiaryWriteEntry],
    ) -> FoodDiaryBulkResult:
        self.bulk_calls.append((date, entries))
        return FoodDiaryBulkResult(
            inserted_entries={entry.reference: str(index + 1) for index, entry in enumerate(entries)},
            failed_entries={},
        )

    async def get_custom_food_definition(self, remote_id: str) -> CustomFoodDefinition:
        assert self.custom_definition is not None
        assert remote_id == self.custom_definition.source_recipe_id
        return self.custom_definition

    async def create_custom_food(self, definition: CustomFoodDefinition) -> str:
        self.created_custom_foods.append(definition)
        return "777"

    async def get_recipe(self, remote_id: str) -> Recipe:
        return self.recipes[remote_id]

    async def create_recipe(self, recipe: Recipe) -> str:
        self.recipes[self.created_recipe_id] = Recipe(
            id=self.created_recipe_id,
            title=recipe.title,
            default_portion_id="303",
        )
        return self.created_recipe_id

    async def add_ingredient(self, remote_recipe_id: str, ingredient) -> bool:  # noqa: ANN001
        return True

    async def save_recipe_meta(self, recipe: Recipe, remote_id: str) -> bool:
        return True

    async def delete_recipe(self, remote_recipe_id: str) -> bool:
        return True

    async def ensure_logged_in(self) -> None:
        return None

    async def resolve_food_detail(self, result):  # noqa: ANN001, ANN201
        return result

    async def close(self) -> None:
        return None


def _storage_with_group(tmp_path) -> tuple[Storage, str]:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.register_user(11, "One")
    storage.register_user(22, "Two")
    group = storage.create_group(11, "Семья")
    storage.join_group_by_code(22, group.invite_code)
    storage.upsert_fatsecret_account(11, "One", "one@example.com", "secret", "BY", "ru")
    storage.upsert_fatsecret_account(22, "Two", "two@example.com", "secret", "BY", "ru")
    return storage, group.id


def _source_day(entry: FoodDiaryEntry | None = None) -> FoodDiaryDay:
    return FoodDiaryDay(
        date=dt.date(2026, 7, 14),
        guid="source-guid",
        entries=[
            entry
            or FoodDiaryEntry(
                entry_id="1",
                recipe_id="3092",
                meal=1,
                name="Яйцо",
                recipe_source="FNDDS",
                recipe_portion_id="10270",
                portion_amount=Decimal("2"),
                serving_description="2 средних",
            )
        ],
    )


def test_diary_copy_appends_to_both_accounts_skips_exact_source_and_is_idempotent(tmp_path) -> None:
    storage, group_id = _storage_with_group(tmp_path)
    try:
        source = FakeDiaryClient(_account("tg11"), _source_day())
        target = FakeDiaryClient(_account("tg22"))
        clients = {"tg11": source, "tg22": target}
        engine = RecipeSyncEngine(storage, _device())
        engine._build_client = lambda account: clients[account.key]  # type: ignore[method-assign]
        engine._build_clients = lambda group_id=None: clients  # type: ignore[method-assign]

        preview = asyncio.run(
            engine.prepare_diary_copy(
                group_id,
                11,
                "tg11",
                dt.date(2026, 7, 14),
                dt.date(2026, 7, 14),
                dt.date(2026, 7, 16),
            )
        )
        first = asyncio.run(engine.execute_diary_copy(preview.run_id))
        second = asyncio.run(engine.execute_diary_copy(preview.run_id))

        assert preview.target_operations == 5
        assert [date for date, _ in source.bulk_calls] == [dt.date(2026, 7, 15), dt.date(2026, 7, 16)]
        assert [date for date, _ in target.bulk_calls] == [
            dt.date(2026, 7, 14),
            dt.date(2026, 7, 15),
            dt.date(2026, 7, 16),
        ]
        assert sum(item.inserted for item in first.dates) == 5
        assert second == first
        assert len(source.bulk_calls) + len(target.bulk_calls) == 5
    finally:
        storage.close()


def test_diary_copy_clones_custom_food_once_and_reuses_mapping(tmp_path) -> None:
    storage, group_id = _storage_with_group(tmp_path)
    try:
        custom_entry = FoodDiaryEntry(
            entry_id="1",
            recipe_id="95638540",
            meal=1,
            name="Экспонента Кефирная",
            recipe_source="Facebook",
            recipe_portion_id="-1",
            portion_amount=Decimal("225"),
            serving_description="225 г",
        )
        source = FakeDiaryClient(_account("tg11"), _source_day(custom_entry))
        source.custom_definition = CustomFoodDefinition(
            source_recipe_id="95638540",
            title="Экспонента Кефирная",
            manufacturer_name="",
            serving_type="Per100g",
            serving_size="100",
            metric_serving_size="100g",
            nutrients={"calories": Decimal("45")},
        )
        target = FakeDiaryClient(_account("tg22"))
        clients = {"tg11": source, "tg22": target}
        engine = RecipeSyncEngine(storage, _device())
        engine._build_client = lambda account: clients[account.key]  # type: ignore[method-assign]
        engine._build_clients = lambda group_id=None: clients  # type: ignore[method-assign]

        for target_date in (dt.date(2026, 7, 14), dt.date(2026, 7, 15)):
            preview = asyncio.run(
                engine.prepare_diary_copy(
                    group_id,
                    11,
                    "tg11",
                    dt.date(2026, 7, 14),
                    target_date,
                    target_date,
                )
            )
            asyncio.run(engine.execute_diary_copy(preview.run_id))

        assert len(target.created_custom_foods) == 1
        assert storage.custom_food_mapping("tg11", "95638540", "tg22") == "777"
        target_writes = [entry for _, entries in target.bulk_calls for entry in entries]
        assert {(entry.recipe_id, entry.recipe_portion_id) for entry in target_writes} == {("777", "-1")}
    finally:
        storage.close()


def test_diary_copy_syncs_missing_personal_recipe_and_maps_target_portion(tmp_path) -> None:
    storage, group_id = _storage_with_group(tmp_path)
    try:
        storage.import_remote_recipe("tg11", RecipeSummary(remote_id="101", title="Омлет"), group_id)
        entry = FoodDiaryEntry(
            entry_id="1",
            recipe_id="101",
            meal=2,
            name="Омлет",
            recipe_source="MD",
            recipe_portion_id="111",
            portion_amount=Decimal("1"),
            serving_description="1 порция",
        )
        source = FakeDiaryClient(_account("tg11"), _source_day(entry))
        source.recipes["101"] = Recipe(id="101", title="Омлет", default_portion_id="111")
        target = FakeDiaryClient(_account("tg22"))
        clients = {"tg11": source, "tg22": target}
        engine = RecipeSyncEngine(storage, _device())
        engine._build_client = lambda account: clients[account.key]  # type: ignore[method-assign]
        engine._build_clients = lambda group_id=None: clients  # type: ignore[method-assign]

        preview = asyncio.run(
            engine.prepare_diary_copy(
                group_id,
                11,
                "tg11",
                dt.date(2026, 7, 14),
                dt.date(2026, 7, 14),
                dt.date(2026, 7, 14),
            )
        )
        asyncio.run(engine.execute_diary_copy(preview.run_id))

        recipe_id = storage.local_recipe_id_for_remote("tg11", "101")
        assert recipe_id is not None
        assert storage.remote_ids(recipe_id)["tg22"] == "202"
        assert target.bulk_calls[0][1][0].recipe_id == "202"
        assert target.bulk_calls[0][1][0].recipe_portion_id == "303"
    finally:
        storage.close()


def test_diary_copy_blocks_empty_source_and_ranges_longer_than_seven_days(tmp_path) -> None:
    storage, group_id = _storage_with_group(tmp_path)
    try:
        source = FakeDiaryClient(_account("tg11"), FoodDiaryDay(dt.date(2026, 7, 14), "", []))
        target = FakeDiaryClient(_account("tg22"))
        clients = {"tg11": source, "tg22": target}
        engine = RecipeSyncEngine(storage, _device())
        engine._build_client = lambda account: clients[account.key]  # type: ignore[method-assign]

        try:
            asyncio.run(
                engine.prepare_diary_copy(
                    group_id,
                    11,
                    "tg11",
                    dt.date(2026, 7, 14),
                    dt.date(2026, 7, 14),
                    dt.date(2026, 7, 21),
                )
            )
        except FatSecretError as exc:
            assert "не больше 7 дней" in str(exc)
        else:
            raise AssertionError("Expected range validation error")

        try:
            asyncio.run(
                engine.prepare_diary_copy(
                    group_id,
                    11,
                    "tg11",
                    dt.date(2026, 7, 14),
                    dt.date(2026, 7, 14),
                    dt.date(2026, 7, 14),
                )
            )
        except FatSecretError as exc:
            assert "нет записей еды" in str(exc)
        else:
            raise AssertionError("Expected empty diary validation error")
    finally:
        storage.close()


def test_client_parses_diary_and_custom_food_and_sends_bulk_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url).startswith(DIARY_BULK_UPDATE_URL):
            return httpx.Response(200, json={"insertedEntries": {"ref-1": 55}, "failedEntries": []})
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    diary_xml = """
    <recipejournalday><guid>g</guid><recipejournalentry><id>1</id><recipeid>3092</recipeid>
    <meal>1</meal><name>Яйцо</name><recipeSource>FNDDS</recipeSource><recipePortionID>10270</recipePortionID>
    <portionAmount>2</portionAmount><servingDescription>2 средних</servingDescription></recipejournalentry></recipejournalday>
    """
    custom_xml = """
    <recipe><id>9</id><title>Мой продукт</title><source>Facebook</source><isOwn>True</isOwn>
    <gramsPerPortion>200</gramsPerPortion><energyPerPortion>300</energyPerPortion>
    <proteinPerPortion>20</proteinPerPortion></recipe>
    """

    day = client._parse_food_diary_day(diary_xml, dt.date(2026, 7, 14))
    definition = client._parse_custom_food_definition(custom_xml, "9")
    result = asyncio.run(
        client.bulk_update_food_diary(
            dt.date(2026, 7, 15),
            [FoodDiaryWriteEntry("ref-1", "3092", "Яйцо", "10270", Decimal("2"), 1)],
        )
    )
    asyncio.run(http.aclose())

    assert day.entries[0].portion_amount == Decimal("2")
    assert definition.nutrients == {"calories": Decimal("150.0"), "protein": Decimal("10.0")}
    assert result.inserted_entries == {"ref-1": "55"}
    payload = json.loads(requests[0].content)
    assert payload["recordedDate"] == 20649
    assert payload["recipes"][0]["recipeportionid"] == 10270
    assert payload["deletes"] == []


def test_create_custom_food_uses_apk_saveregional_contract() -> None:
    request_body: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = parse_qs(request.content.decode())
        return httpx.Response(200, text="SUCCESS:777")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    remote_id = asyncio.run(
        client.create_custom_food(
            CustomFoodDefinition(
                source_recipe_id="9",
                title="Мой продукт",
                manufacturer_name="Бренд",
                serving_type="Per100g",
                serving_size="100",
                metric_serving_size="100g",
                nutrients={"calories": Decimal("150"), "protein": Decimal("10")},
            )
        )
    )
    asyncio.run(http.aclose())

    assert remote_id == "777"
    assert request_body["action"] == ["saveregional"]
    assert request_body["productName"] == ["Мой продукт"]
    assert request_body["servingType"] == ["Per100g"]
    assert request_body["calories"] == ["150"]


def test_diary_date_and_range_parser_accepts_manual_dates_and_relative_words() -> None:
    today = dt.date(2026, 7, 14)

    assert _parse_diary_date("сегодня", today) == today
    assert _parse_diary_date("вчера", today) == dt.date(2026, 7, 13)
    assert _parse_diary_range("15.07.2026 - 17.07.2026", today) == (
        dt.date(2026, 7, 15),
        dt.date(2026, 7, 17),
    )
    assert _parse_diary_range("завтра", today) == (dt.date(2026, 7, 15), dt.date(2026, 7, 15))
