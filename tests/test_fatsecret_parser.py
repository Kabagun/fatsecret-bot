from __future__ import annotations

import asyncio
from decimal import Decimal
from urllib.parse import parse_qs

import httpx

from fatsecret_bot.fatsecret_client import FatSecretClient, FatSecretError, parse_recipe_initial_save_response
from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, FatSecretSession, Ingredient, Recipe


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


def test_parse_recipe_list_extracts_brand_and_default_portion_from_metadata() -> None:
    xml = """
    <recipes>
      <recipe>
        <id>123</id>
        <title>Кетчуп Русский</title>
        <defaultPortionID>0</defaultPortionID>
        <defaultPortionDescription>100г</defaultPortionDescription>
        <shortDescription>mtypeS#E{P&lt;A*R*A&gt;T}O!R1S#E{P&lt;A*R*A&gt;T}O!RmnameS#E{P&lt;A*R*A&gt;T}O!RМахеевS#E{P&lt;A*R*A&gt;T}O!RssizeS#E{P&lt;A*R*A&gt;T}O!R100г</shortDescription>
      </recipe>
    </recipes>
    """
    recipes = _client()._parse_recipe_list(xml)

    assert recipes[0].brand == "Махеев"
    assert recipes[0].description == ""
    assert recipes[0].default_portion_id == "0"
    assert recipes[0].default_portion_description == "100г"


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


def test_parse_recipe_initial_save_response_explains_rejected_save() -> None:
    try:
        parse_recipe_initial_save_response("Невозможно сохранить. Пожалуйста, попробуйте еще раз позже.")
    except FatSecretError as exc:
        assert "FatSecret отклонил создание рецепта на первом шаге" in str(exc)
        assert "другое имя" in str(exc)
    else:
        raise AssertionError("expected FatSecretError")


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


def test_add_ingredient_sends_prepared_portion_amount() -> None:
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
        ok = asyncio.run(
            client.add_ingredient(
                "recipe-1",
                Ingredient(
                    id="ingredient-1",
                    recipe_id="recipe-1",
                    food_id="food-1",
                    title="Запеченное Филе Карпа",
                    portion_id="0",
                    amount=Decimal("3"),
                    portion_description="100г",
                ),
            )
        )
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    form = parse_qs(requests[0].content.decode())
    assert form["action"] == ["ingredientsave"]
    assert form["portionamount"] == ["3"]


def test_add_ingredient_converts_legacy_zero_portion_grams() -> None:
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
        ok = asyncio.run(
            client.add_ingredient(
                "recipe-1",
                Ingredient(
                    id="ingredient-1",
                    recipe_id="recipe-1",
                    food_id="food-1",
                    title="Куркума",
                    portion_id="0",
                    amount=Decimal("5"),
                    portion_description="г",
                ),
            )
        )
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    form = parse_qs(requests[0].content.decode())
    assert form["portionamount"] == ["0.05"]


def test_save_recipe_meta_posts_recipe_steps() -> None:
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
        ok = asyncio.run(
            client.save_recipe_meta(
                Recipe(
                    id="local",
                    title="Омлет",
                    description="desc",
                    steps=["Смешать", "Запечь", "Подать", "Лишнее"],
                ),
                "recipe-1",
            )
        )
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    form = parse_qs(requests[0].content.decode())
    assert form["action"] == ["recipesave"]
    assert form["step1"] == ["Смешать"]
    assert form["step2"] == ["Запечь"]
    assert form["step3"] == ["Подать"]
