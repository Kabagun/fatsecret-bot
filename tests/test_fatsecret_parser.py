from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx

from fatsecret_bot.fatsecret_client import FatSecretClient, parse_recipe_initial_save_response
from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, FatSecretSession


def _client() -> FatSecretClient:
    return FatSecretClient(
        FatSecretAccountConfig("a1", "A1", "user", "pass", "BY", "ru"),
        FatSecretDeviceConfig(
            app_version="11.5.0.4",
            device="6",
            build_sdk="30",
            build_api="11",
            build_model="NE2211",
            build_resolution="1920x1080",
            device_identifier="NE2211",
        ),
    )


def test_parse_recipe_list() -> None:
    xml = """
    <recipes>
      <recipe>
        <id>123</id>
        <title>Омлет</title>
        <energyPerPortion>100.5</energyPerPortion>
      </recipe>
    </recipes>
    """
    recipes = _client()._parse_recipe_list(xml)
    assert len(recipes) == 1
    assert recipes[0].remote_id == "123"
    assert recipes[0].title == "Омлет"


def test_parse_recipe_ingredients() -> None:
    xml = """
    <recipe>
      <id>999</id>
      <title>Омлет</title>
      <shortDescription>desc</shortDescription>
      <servings>2</servings>
      <preparationtimemin>5</preparationtimemin>
      <cookingtimemin>10</cookingtimemin>
      <defaultPortionID>4751539</defaultPortionID>
      <recipeingredient>
        <id>1</id>
        <associatedrecipeid>4881229</associatedrecipeid>
        <name>Куриное Филе</name>
        <portionid>4751539</portionid>
        <portionamount>100.0</portionamount>
        <portiondescription>г</portiondescription>
      </recipeingredient>
    </recipe>
    """
    recipe = _client()._parse_recipe(xml)
    assert recipe.title == "Омлет"
    assert recipe.default_portion_id == "4751539"
    assert recipe.ingredients[0].food_id == "4881229"
    assert recipe.ingredients[0].portion_id == "4751539"


def test_parse_recipe_initial_save_response() -> None:
    assert parse_recipe_initial_save_response("SUCCESS:129226840") == "129226840"
    assert parse_recipe_initial_save_response("129226840") == "129226840"


def test_delete_recipe_posts_recipedelete_form() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="True")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FatSecretClient(
        FatSecretAccountConfig("a1", "A1", "user", "pass", "BY", "ru"),
        FatSecretDeviceConfig(
            app_version="11.5.0.4",
            device="6",
            build_sdk="30",
            build_api="11",
            build_model="NE2211",
            build_resolution="1920x1080",
            device_identifier="NE2211",
        ),
        http=http,
    )
    client._session = FatSecretSession(server_id="server", device_key="device", secret_key="secret")
    try:
        ok = asyncio.run(client.delete_recipe("123456"))
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    assert len(requests) == 1
    form = parse_qs(requests[0].content.decode())
    assert form["action"] == ["recipedelete"]
    assert form["rid"] == ["123456"]
    assert form["fl"] == ["5"]
