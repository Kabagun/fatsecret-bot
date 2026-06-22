from __future__ import annotations

import asyncio
from decimal import Decimal
from urllib.parse import parse_qs

import httpx

from fatsecret_bot.fatsecret_client import FatSecretClient, FatSecretError, parse_recipe_initial_save_response
from fatsecret_bot.models import FatSecretAccountConfig, FatSecretDeviceConfig, FatSecretSession, FoodSearchResult, Ingredient, Recipe


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
      <step1>Смешать</step1>
      <step4>Подать</step4>
      <step2>Запечь</step2>
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
    assert recipe.steps == ["Смешать", "Запечь", "Подать"]
    assert recipe.ingredients[0].food_id == "4881229"
    assert recipe.ingredients[0].portion_id == "4751539"
    assert recipe.ingredients[0].grams == Decimal("100.0")


def test_parse_recipe_ingredient_normalizes_serving_with_grams_per_portion() -> None:
    xml = """
    <recipe>
      <id>999</id>
      <title>Омлет</title>
      <recipeingredient>
        <id>1</id>
        <associatedrecipeid>4881229</associatedrecipeid>
        <name>Соус</name>
        <portionid>serving-1</portionid>
        <portionamount>1.5</portionamount>
        <portiondescription>порции</portiondescription>
        <gramsPerPortion>100</gramsPerPortion>
      </recipeingredient>
    </recipe>
    """
    recipe = _client()._parse_recipe(xml)

    assert recipe.ingredients[0].amount == Decimal("1.5")
    assert recipe.ingredients[0].portion_description == "порции"
    assert recipe.ingredients[0].grams == Decimal("150.0")


def test_search_recipes_uses_app_food_search_data_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/food/v1/search/data"
        params = dict(request.url.params)
        assert params["c_fl"] == "1"
        assert "c_id" not in params
        assert "c_s" not in params
        assert "c_d" not in params
        assert request.headers["c_id"] == "server"
        assert request.headers["c_s"] == "secret"
        assert request.headers["c_d"] == "device"
        assert request.headers["fs_device"] == "android"
        assert request.headers["fs_dt"]
        assert request.headers["market"] == "BY"
        assert request.headers["fs_market_locale"] == "BY"
        assert request.headers["fs_language_locale"] == "ru"
        assert request.content
        return httpx.Response(
            200,
            json={
                "summaries": [
                    {
                        "id": "8418618",
                        "title": "Кетчуп Русский",
                        "manufacturername": "Махеев",
                        "defaultPortionId": "0",
                        "servingSize": "50г",
                        "gramsPerPortion": 50,
                        "energyPerPortion": 31,
                        "proteinPerPortion": 0.6,
                        "fatPerPortion": 0,
                        "carbohydratePerPortion": 7.1,
                        "isOwn": True,
                    }
                ]
            },
        )

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
        session=FatSecretSession(server_id="server", device_key="device", secret_key="secret"),
    )
    try:
        results = asyncio.run(client.search_recipes("русский", page=2))
    finally:
        asyncio.run(http.aclose())

    assert len(requests) == 1
    assert requests[0].url.path == "/api/food/v1/search/data"
    assert results[0].food_id == "8418618"
    assert results[0].title == "Кетчуп Русский"
    assert results[0].brand == "Махеев"
    assert results[0].default_portion_description == "50г"
    assert results[0].is_own is True
    assert results[0].energy_per_portion == Decimal("62")
    assert results[0].protein_per_portion == Decimal("1.2")
    assert results[0].carbohydrate_per_portion == Decimal("14.2")


def test_parse_food_search_data_keeps_app_100g_energy_when_grams_per_portion_zero() -> None:
    results = _client()._parse_food_search_data(
        {
            "summaries": [
                {
                    "id": 31531828,
                    "title": "Фарш Сочный",
                    "brandName": "Green",
                    "gramsPerPortion": 0,
                    "servingSize": "100г",
                    "energyPerPortion": 320,
                    "proteinPerPortion": 15,
                    "fatPerPortion": 29,
                    "carbohydratePerPortion": 0,
                }
            ]
        }
    )

    assert results[0].food_id == "31531828"
    assert results[0].title == "Фарш Сочный"
    assert results[0].brand == "Green"
    assert results[0].default_portion_description == "100г"
    assert results[0].energy_per_portion == Decimal("320")
    assert results[0].protein_per_portion == Decimal("15")
    assert results[0].fat_per_portion == Decimal("29")
    assert results[0].carbohydrate_per_portion == Decimal("0")


def test_resolve_food_detail_extracts_brand_and_portion_from_metadata_description() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/RecipeAndroidPage.aspx")
        return httpx.Response(
            200,
            text="""
            <recipe>
              <id>8418618</id>
              <title>Сметана 20%</title>
              <shortDescription>mtypeS#E{P&lt;A*R*A&gt;T}O!R1S#E{P&lt;A*R*A&gt;T}O!RmnameS#E{P&lt;A*R*A&gt;T}O!RБрест-ЛитовскS#E{P&lt;A*R*A&gt;T}O!RssizeS#E{P&lt;A*R*A&gt;T}O!R100г</shortDescription>
              <defaultPortionID>0</defaultPortionID>
            </recipe>
            """,
        )

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
        session=FatSecretSession(server_id="server", device_key="device", secret_key="secret"),
    )
    try:
        result = asyncio.run(client.resolve_food_detail(FoodSearchResult(food_id="8418618", title="Сметана 20%")))
    finally:
        asyncio.run(http.aclose())

    assert result.title == "Сметана 20%"
    assert result.brand == "Брест-Литовск"
    assert result.default_portion_description == "100г"


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


def test_cached_session_skips_login() -> None:
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
        session=FatSecretSession(server_id="server", device_key="device", secret_key="secret"),
    )
    try:
        ok = asyncio.run(client.delete_recipe("123456"))
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    assert len(requests) == 1
    assert "authenticate" not in str(requests[0].url)


def test_cached_session_retries_login_once_on_auth_failure() -> None:
    requests: list[httpx.Request] = []
    saved: list[FatSecretSession] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "authenticate" in str(request.url):
            return httpx.Response(200, json={"serverId": "new-server", "deviceKey": "new-device", "secretKey": "new-secret"})
        if len([item for item in requests if "RecipeActionAndroidPage.aspx" in str(item.url)]) == 1:
            return httpx.Response(500, text="expired")
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
        session=FatSecretSession(server_id="old-server", device_key="old-device", secret_key="old-secret"),
        session_saver=saved.append,
    )
    try:
        ok = asyncio.run(client.delete_recipe("123456"))
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    assert saved == [FatSecretSession(server_id="new-server", device_key="new-device", secret_key="new-secret")]
    assert [request.url.path for request in requests].count("/api/authenticate/v1/fatsecret") == 1
    assert [request.url.path for request in requests].count("/android/RecipeActionAndroidPage.aspx") == 2


def test_cached_session_retries_login_once_on_redirect() -> None:
    requests: list[httpx.Request] = []
    saved: list[FatSecretSession] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "authenticate" in str(request.url):
            return httpx.Response(
                200,
                json={"serverId": "new-server", "deviceKey": "new-device", "secretKey": "new-secret"},
            )
        if len([item for item in requests if "RecipeActionAndroidPage.aspx" in str(item.url)]) == 1:
            return httpx.Response(302, headers={"Location": "/Default.aspx"}, text="")
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
        session=FatSecretSession(server_id="old-server", device_key="old-device", secret_key="old-secret"),
        session_saver=saved.append,
    )
    try:
        ok = asyncio.run(client.delete_recipe("123456"))
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    assert saved == [FatSecretSession(server_id="new-server", device_key="new-device", secret_key="new-secret")]
    assert [request.url.path for request in requests].count("/api/authenticate/v1/fatsecret") == 1
    assert [request.url.path for request in requests].count("/android/RecipeActionAndroidPage.aspx") == 2


def test_concurrent_cached_session_redirects_share_one_login() -> None:
    requests: list[httpx.Request] = []
    saved: list[FatSecretSession] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/authenticate/v1/fatsecret":
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                json={"serverId": "new-server", "deviceKey": "new-device", "secretKey": "new-secret"},
            )
        form = parse_qs(request.content.decode())
        if form.get("c_id") == ["old-server"]:
            await asyncio.sleep(0.01)
            return httpx.Response(302, headers={"Location": "/Default.aspx"}, text="")
        return httpx.Response(200, text="True")

    async def run_deletes(client: FatSecretClient) -> list[bool]:
        return await asyncio.gather(client.delete_recipe("111"), client.delete_recipe("222"))

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
        session=FatSecretSession(server_id="old-server", device_key="old-device", secret_key="old-secret"),
        session_saver=saved.append,
    )
    try:
        results = asyncio.run(run_deletes(client))
    finally:
        asyncio.run(http.aclose())

    assert results == [True, True]
    assert saved == [FatSecretSession(server_id="new-server", device_key="new-device", secret_key="new-secret")]
    assert [request.url.path for request in requests].count("/api/authenticate/v1/fatsecret") == 1
    assert [request.url.path for request in requests].count("/android/RecipeActionAndroidPage.aspx") == 4


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
    steps = [f"Шаг {index}" for index in range(1, 102)]
    try:
        ok = asyncio.run(
            client.save_recipe_meta(
                Recipe(
                    id="local",
                    title="Омлет",
                    description="desc",
                    steps=steps,
                ),
                "recipe-1",
            )
        )
    finally:
        asyncio.run(http.aclose())

    assert ok is True
    form = parse_qs(requests[0].content.decode())
    assert form["action"] == ["recipesave"]
    assert form["step1"] == ["Шаг 1"]
    assert form["step4"] == ["Шаг 4"]
    assert form["step100"] == ["Шаг 100"]
    assert "step101" not in form
