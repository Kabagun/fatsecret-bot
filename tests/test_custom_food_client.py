from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from fatsecret_bot.fatsecret_client import (
    BARCODE_REMAP_URL,
    FatSecretClient,
    FatSecretError,
    FatSecretNotCustomFoodError,
)
from fatsecret_bot.models import (
    CustomFoodDefinition,
    FatSecretAccountConfig,
    FatSecretDeviceConfig,
    FatSecretSession,
)


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


def test_facebook_source_is_not_custom_when_is_own_is_false() -> None:
    client = FatSecretClient(_account("tg11"), _device())
    public_xml = """
    <recipe><id>7954761</id><title>Молоко 1,5%</title><source>Facebook</source><isOwn>False</isOwn>
    <gramsPerPortion>100</gramsPerPortion><energyPerPortion>44</energyPerPortion></recipe>
    """

    try:
        client._parse_custom_food_definition(public_xml, "7954761")
    except FatSecretNotCustomFoodError:
        pass
    else:
        raise AssertionError("expected public Facebook food to stay non-custom")


def test_list_custom_food_brands_uses_quick_picks_manufacturer_catalog() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text="""
            <item><man>true</man><manType>Manufacturer</manType>
              <item><name>Санта</name></item>
              <item><name>  Санта   Бремор </name></item>
              <item><name>санта</name></item>
            </item>
            """,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    try:
        brands = asyncio.run(client.list_custom_food_brands())
    finally:
        asyncio.run(http.aclose())

    assert brands == ["Санта", "Санта Бремор"]
    assert requests[0].url.path == "/android/QuickPicksAndroidPage.aspx"


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
                serving_size="",
                metric_serving_size="100g",
                nutrients={"calories": Decimal("150"), "protein": Decimal("10")},
                barcode="4006381333931",
                barcode_type="EAN_13",
            )
        )
    )
    asyncio.run(http.aclose())

    assert remote_id == "777"
    assert request_body["action"] == ["saveregional"]
    assert request_body["productName"] == ["Мой продукт"]
    assert request_body["servingType"] == ["Per100g"]
    assert "servingSize" not in request_body
    assert request_body["metricServingSize"] == ["100г"]
    assert request_body["calories"] == ["150"]
    assert request_body["manufacturerType"] == ["1"]
    assert request_body["manufacturerName"] == ["Бренд"]
    assert request_body["barcode"] == ["4006381333931"]
    assert request_body["barcodeType"] == ["EAN_13"]


def test_create_custom_food_omits_skipped_brand_and_barcode() -> None:
    request_body: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_body
        request_body = parse_qs(request.content.decode())
        return httpx.Response(200, text="SUCCESS:778")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    try:
        remote_id = asyncio.run(
            client.create_custom_food(
                CustomFoodDefinition(
                    source_recipe_id="",
                    title="Без бренда",
                    manufacturer_name="",
                    serving_type="Per100g",
                    serving_size="",
                    metric_serving_size="100g",
                    nutrients={"calories": Decimal("100"), "protein": Decimal("5")},
                )
            )
        )
    finally:
        asyncio.run(http.aclose())

    assert remote_id == "778"
    assert "manufacturerType" not in request_body
    assert "manufacturerName" not in request_body
    assert "barcode" not in request_body
    assert "barcodeType" not in request_body


def test_custom_food_readback_normalizes_metric_100g_without_named_serving() -> None:
    client = FatSecretClient(_account("tg11"), _device())
    correct_xml = """
    <recipe><id>1</id><title>КСБ-80</title><isOwn>True</isOwn><servingSize>100г</servingSize>
    <gramsPerPortion>100</gramsPerPortion><energyPerPortion>380</energyPerPortion>
    <recipeportion><id>-1</id><description>g</description><gramWeight>100</gramWeight></recipeportion></recipe>
    """
    legacy_bad_xml = """
    <recipe><id>2</id><title>Стрипсы</title><isOwn>True</isOwn><servingSize>100g</servingSize>
    <gramsPerPortion>100</gramsPerPortion><energyPerPortion>260</energyPerPortion>
    <recipeportion><id>99</id><description>100g</description><gramWeight>100</gramWeight></recipeportion>
    <recipeportion><id>-1</id><description>g</description><gramWeight>100</gramWeight></recipeportion></recipe>
    """

    correct = client._parse_custom_food_definition(correct_xml, "1")
    legacy_bad = client._parse_custom_food_definition(legacy_bad_xml, "2")

    assert correct.serving_size == ""
    assert correct.metric_serving_size == "100g"
    assert legacy_bad.serving_size == "100g"


def test_lookup_barcode_uses_app_scan_contract_and_parses_match() -> None:
    request_json: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_json
        request_json = json.loads(request.content)
        assert request.url.path == "/api/barcode-verification/v1/scan"
        return httpx.Response(
            200,
            json={
                "foodId": 12345,
                "barcodeId": 67890,
                "shouldPrompt": True,
                "foodName": "Known Milk",
                "brandName": "QA",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    result = asyncio.run(client.lookup_barcode("4006381333931"))
    asyncio.run(http.aclose())

    assert request_json == {"barcode": "4006381333931", "deviceCanPrompt": False}
    assert result.found is True
    assert result.food_id == "12345"
    assert result.barcode_id == "67890"
    assert result.food_name == "Known Milk"


def test_remap_barcode_uses_apk_contract_without_automatic_replay() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    try:
        asyncio.run(
            client.remap_barcode(
                "4006381333931",
                "12345",
                is_new_food=True,
                barcode_id="67890",
            )
        )
    finally:
        asyncio.run(http.aclose())

    assert len(requests) == 1
    assert requests[0].url.path == "/api/barcode-verification/v1/remap"
    assert str(requests[0].url).startswith(BARCODE_REMAP_URL)
    assert json.loads(requests[0].content) == {
        "rawBarcode": "4006381333931",
        "foodId": 12345,
        "isNewFood": True,
        "globalBarcodeId": 67890,
    }


def test_remap_barcode_does_not_replay_failed_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(_account("tg11"), _device(), http=http, session=FatSecretSession("s", "d", "k"))
    try:
        with pytest.raises(FatSecretError, match="barcode remap failed with HTTP 500"):
            asyncio.run(client.remap_barcode("4006381333931", "12345", is_new_food=True))
    finally:
        asyncio.run(http.aclose())

    assert len(requests) == 1


def test_custom_food_readback_normalizes_nutrients_per_100g() -> None:
    client = FatSecretClient(_account("tg11"), _device())
    custom_xml = """
    <recipe><id>9</id><title>Мой продукт</title><source>Facebook</source><isOwn>True</isOwn>
    <gramsPerPortion>200</gramsPerPortion><energyPerPortion>300</energyPerPortion>
    <proteinPerPortion>20</proteinPerPortion></recipe>
    """

    definition = client._parse_custom_food_definition(custom_xml, "9")

    assert definition.nutrients == {"calories": Decimal("150.0"), "protein": Decimal("10.0")}
