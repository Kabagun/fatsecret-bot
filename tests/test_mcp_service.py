from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from fatsecret_bot.mcp_service import FatSecretMCPService, IngredientInput, WorkflowError
from fatsecret_bot.models import BarcodeLookupResult, Ingredient, Recipe, RemoteRecipeVariant
from fatsecret_bot.recipe_compare import recipe_fingerprint
from fatsecret_bot.storage import Storage
from fatsecret_bot.sync import AccountSyncResult, CustomFoodCreateResult, RecipeCreateResult, RecipeSyncEngine, ResolvedRecipeListItem


def candidate(title="Молоко", food_id="food-1", grams=Decimal(100)):
    return ResolvedRecipeListItem(title, grams,
        Ingredient("row", "", food_id, title, "portion", grams / 100, "100г", grams=grams),
        "FatSecret", brand="Synthetic", protein_per_100g=Decimal(3))


class Engine:
    _validate_custom_food_definition = staticmethod(RecipeSyncEngine._validate_custom_food_definition)
    custom_food_recipe_list_item = staticmethod(RecipeSyncEngine.custom_food_recipe_list_item)

    @asynccontextmanager
    async def operation(self):
        yield


@pytest.fixture
def setup(tmp_path):
    storage = Storage(tmp_path / "test.sqlite3")
    storage.register_user(1001, "Synthetic owner")
    group = storage.create_group(1001, "Test group")
    accounts = [storage.create_fatsecret_account(1001, f"Account {n}", f"synthetic-{n}@example.test",
                "synthetic-password", "BY", "ru", group_id=group.id) for n in [1, 2]]
    storage.register_user(2001, "Other owner")
    other = storage.create_group(2001, "Other group")
    engine = Engine()
    engine.recipe_list_candidates = AsyncMock(side_effect=lambda group_id, query, **kw: [candidate(query, grams=kw["grams"])])
    engine.lookup_barcode = AsyncMock(return_value=BarcodeLookupResult("4006381333931"))
    engine.create_custom_food_for_group = AsyncMock(return_value=CustomFoodCreateResult("run", "My food", dict(zip(accounts, ["custom-1", "custom-2"]))))
    engine.create_recipe_from_list = AsyncMock(return_value=RecipeCreateResult("new", [AccountSyncResult(k, "remote", True, "OK") for k in accounts], "Test recipe"))
    engine.load_remote_recipe_index = AsyncMock(return_value=[])
    engine.hydrate_recipe_edit_source_items = AsyncMock(side_effect=lambda g, a, items: items)
    engine.edit_recipe_from_list = AsyncMock(return_value=engine.create_recipe_from_list.return_value)
    service = FatSecretMCPService(storage, engine, group.id)
    yield storage, engine, service, accounts, other.id
    storage.close()


def run(coro):
    return asyncio.run(coro)


def test_recipe_requires_each_selection_and_current_approval(setup):
    _, engine, service, _, _ = setup

    async def scenario():
        draft = await service.prepare_recipe("Test recipe", [IngredientInput(query="Milk", grams=150)])
        assert not draft["ready"]
        engine.create_recipe_from_list.assert_not_awaited()
        with pytest.raises(WorkflowError, match="каждый ингредиент"):
            await service.confirm_action(draft["action_ref"], draft["revision"], True)
        entry = draft["ingredients"][0]
        selected = await service.set_recipe_ingredient(draft["action_ref"], entry["ingredient_ref"],
                    candidate_ref=entry["candidates"][0]["candidate_ref"])
        assert selected["ready"]
        assert selected["revision"] == 2
        with pytest.raises(WorkflowError, match="подтверждение"):
            await service.confirm_action(draft["action_ref"], 2)
        with pytest.raises(WorkflowError, match="изменился"):
            await service.confirm_action(draft["action_ref"], 1, True)
        result = await service.confirm_action(draft["action_ref"], 2, True)
        assert result["ok"]
        assert await service.confirm_action(draft["action_ref"], 2, True) == result
        engine.create_recipe_from_list.assert_awaited_once()
        args = engine.create_recipe_from_list.await_args.args
        assert args[0] == service.group_id
        assert args[3] == 1001
        assert args[2][0].ingredient.amount == Decimal("1.5")
    run(scenario())


def test_custom_product_automatically_binds_account_ids_then_creates_recipe(setup):
    _, engine, service, accounts, _ = setup

    async def scenario():
        draft = await service.prepare_recipe("Test recipe", [IngredientInput(query="My food", grams=250)])
        entry = draft["ingredients"][0]
        food = await service.prepare_custom_food("My food", 100, 5, 4, 11,
            recipe_draft_ref=draft["action_ref"], ingredient_ref=entry["ingredient_ref"])
        engine.create_custom_food_for_group.assert_not_awaited()
        result = await service.confirm_action(food["action_ref"], 1, True)
        recipe_draft = result["recipe_draft"]
        assert recipe_draft["ready"]
        assert recipe_draft["revision"] == 2
        assert recipe_draft["ingredients"][0]["selected"]["title"] == "My food"
        # A lost response reuses the completed action rather than creating a second food.
        assert await service.confirm_action(food["action_ref"], 1, True) == result
        await service.confirm_action(recipe_draft["action_ref"], 2, True)
        item = engine.create_recipe_from_list.await_args.args[2][0]
        assert item.custom_food_ids == dict(zip(accounts, ["custom-1", "custom-2"]))
        assert item.ingredient.amount == Decimal("2.5")
        assert item.ingredient.id == entry["ingredient_ref"]
        engine.create_custom_food_for_group.assert_awaited_once()
    run(scenario())


def test_serving_food_needs_weight_before_recipe_write(setup):
    _, engine, service, _, _ = setup

    async def scenario():
        draft = await service.prepare_recipe("Recipe", [IngredientInput(query="Food", grams=50)])
        with pytest.raises(WorkflowError, match="вес одной порции"):
            await service.prepare_custom_food("Food", 100, 5, 4, 11, basis="serving", serving_name="Bar",
                recipe_draft_ref=draft["action_ref"], ingredient_ref=draft["ingredients"][0]["ingredient_ref"])
        food = await service.prepare_custom_food("Food", 100, 5, 4, 11, basis="serving", serving_name="Bar", serving_weight_grams=50,
                recipe_draft_ref=draft["action_ref"], ingredient_ref=draft["ingredients"][0]["ingredient_ref"])
        result = await service.confirm_action(food["action_ref"], 1, True)
        assert result["recipe_draft"]["ready"]
        definition = engine.create_custom_food_for_group.await_args.args[1]
        assert definition.nutrition_grams == 50
    run(scenario())


def test_known_barcode_returns_existing_product_without_creation(setup):
    _, engine, service, _, _ = setup
    engine.lookup_barcode.return_value = BarcodeLookupResult("4006381333931", food_id="known", food_name="Known")
    engine.barcode_recipe_list_item = AsyncMock(return_value=candidate("Known", "known"))
    result = run(service.prepare_custom_food("Duplicate", 100, 5, 4, 11, barcode="4006381333931"))
    assert result["existing_product"]["title"] == "Known"
    assert result["needs_confirmation"] is False
    engine.create_custom_food_for_group.assert_not_awaited()


def test_drafts_persist_and_references_cannot_cross_groups(setup):
    storage, engine, service, _, other = setup

    async def scenario():
        draft = await service.prepare_recipe("Recipe", [IngredientInput(query="Food", grams=50)])
        restored = FatSecretMCPService(storage, engine, service.group_id)
        assert (await restored.get_action(draft["action_ref"]))["title"] == "Recipe"
        assert (await restored.list_drafts())["drafts"][0]["action_ref"] == draft["action_ref"]
        foreign = FatSecretMCPService(storage, engine, other)
        with pytest.raises(WorkflowError, match="недоступен"):
            await foreign.get_action(draft["action_ref"])
        ref = draft["ingredients"][0]["candidates"][0]["candidate_ref"]
        with pytest.raises(WorkflowError, match="недоступен"):
            foreign._get(ref, "candidate")
        assert (await foreign.list_drafts())["drafts"] == []
    run(scenario())


def test_quantity_changes_preserve_exact_product_and_invalidate_confirmation(setup):
    _, _, service, _, _ = setup

    async def scenario():
        found = await service.search_foods("Milk", 100)
        draft = await service.prepare_recipe("Recipe", [IngredientInput(query="Milk", grams=100, candidate_ref=found["candidates"][0]["candidate_ref"])])
        entry = draft["ingredients"][0]
        updated = await service.set_recipe_ingredient(draft["action_ref"], entry["ingredient_ref"], grams=250)
        data, _ = service._get(draft["action_ref"], "action")
        assert Decimal(str(data["entries"][0]["selected"]["ingredient"]["amount"])) == Decimal("2.5")
        assert updated["ready"]
        changed = await service.set_recipe_ingredient(draft["action_ref"], entry["ingredient_ref"], query="Oat milk")
        assert not changed["ready"]
        assert changed["ingredients"][0]["candidates"][0]["title"] == "Oat milk"
    run(scenario())


def live_recipe(engine, group_id, accounts):
    recipe = Recipe("local", "Omelette", portions=Decimal(2), steps=["Mix"], description="Description", group_id=group_id,
        ingredients=[Ingredient("original-row", "local", "egg", "Egg", "weight", Decimal(2), "100г", grams=Decimal(200))],
        remote_ids={accounts[0]: "remote-1", accounts[1]: "remote-2"},
        remote_ids_by_account={accounts[0]: ["remote-1"], accounts[1]: ["remote-2"]})
    engine.load_remote_recipe_index.return_value = [recipe]

    async def variants(ref):
        return [RemoteRecipeVariant(k, remote, copy.deepcopy(recipe), recipe_fingerprint(recipe)) for k, remote in recipe.remote_ids.items()]
    engine.hydrate_live_recipe_variants = AsyncMock(side_effect=variants)
    return recipe


def test_edit_retains_source_rows_and_passes_strict_digest(setup):
    _, engine, service, accounts, _ = setup
    recipe = live_recipe(engine, service.group_id, accounts)

    async def scenario():
        ref = (await service.list_recipes())["recipes"][0]["recipe_ref"]
        details = await service.get_recipe(ref)
        exported = details["variants"][0]["export_text"]
        from fatsecret_bot.telegram_bot import _parse_recipe_list_payload
        parsed = _parse_recipe_list_payload(exported)
        assert not parsed.bad_lines
        assert parsed.items[0].query == "Egg" and parsed.items[0].grams == 200
        assert parsed.portions == 2 and parsed.steps == ["Mix"]
        draft = await service.prepare_recipe(source_variant_ref=details["variants"][0]["variant_ref"])
        assert draft["portions"] == "2"
        assert draft["steps"] == ["Mix"]
        assert draft["ingredients"][0]["ingredient_ref"] == "original-row"
        draft = await service.update_recipe_draft(draft["action_ref"], portions=3, steps=["Mix", "Cook"])
        await service.confirm_action(draft["action_ref"], draft["revision"], True)
        args = engine.edit_recipe_from_list.await_args
        assert args.args[:4] == (service.group_id, accounts[0], "remote-1", recipe_fingerprint(recipe).digest)
        assert args.args[4][0].ingredient.id == "original-row"
        assert args.kwargs["steps"] == ["Mix", "Cook"]
    run(scenario())


def test_changed_recipe_blocks_delete_before_remote_write(setup):
    _, engine, service, accounts, _ = setup
    recipe = live_recipe(engine, service.group_id, accounts)
    engine.delete_live_recipes_everywhere = AsyncMock()

    async def scenario():
        ref = (await service.list_recipes())["recipes"][0]["recipe_ref"]
        action = await service.prepare_recipe_action("delete", [ref])
        recipe.steps.append("Changed after approval")
        with pytest.raises(WorkflowError, match="изменился после"):
            await service.confirm_action(action["action_ref"], 1, True)
        engine.delete_live_recipes_everywhere.assert_not_awaited()
    run(scenario())


def test_group_membership_change_blocks_prepared_write(setup):
    storage, engine, service, _, _ = setup

    async def scenario():
        action = await service.prepare_custom_food("Food", 100, 5, 4, 11)
        storage.create_fatsecret_account(1001, "New account", "new@example.test", "synthetic", "BY", "ru", group_id=service.group_id)
        with pytest.raises(WorkflowError, match="Состав аккаунтов"):
            await service.confirm_action(action["action_ref"], 1, True)
        engine.create_custom_food_for_group.assert_not_awaited()
    run(scenario())


def test_new_recipe_timeout_retries_same_durable_request(setup):
    _, engine, service, _, _ = setup

    async def scenario():
        found = await service.search_foods("Milk")
        draft = await service.prepare_recipe("Recipe", [IngredientInput(query="Milk", grams=100, candidate_ref=found["candidates"][0]["candidate_ref"])])
        expected = engine.create_recipe_from_list.return_value
        engine.create_recipe_from_list.side_effect = [TimeoutError(), expected]
        with pytest.raises(TimeoutError):
            await service.confirm_action(draft["action_ref"], 1, True)
        assert (await service.get_action(draft["action_ref"]))["status"] == "executing"
        engine.load_remote_recipe_index.return_value = [Recipe("visible", "Recipe", group_id=service.group_id)]
        assert (await service.confirm_action(draft["action_ref"], 1, True))["ok"]
        assert engine.create_recipe_from_list.await_count == 2
        assert engine.create_recipe_from_list.await_args_list[0] == engine.create_recipe_from_list.await_args_list[1]
    run(scenario())


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), -1, 0, 100001])
def test_invalid_mass_never_reaches_engine(setup, value):
    _, engine, service, _, _ = setup
    with pytest.raises(WorkflowError):
        run(service.search_foods("Food", value))
    engine.recipe_list_candidates.assert_not_awaited()


def test_group_response_never_exposes_credentials_or_technical_accounts(setup):
    _, _, service, accounts, _ = setup
    result = run(service.get_group())
    text = str(result)
    assert "Account 1" in text
    assert "password" not in text
    assert "@example.test" not in text
    assert all(k not in text for k in accounts)


def test_food_attachment_rejects_a_changed_recipe_draft(setup):
    _, engine, service, _, _ = setup

    async def scenario():
        draft = await service.prepare_recipe("Recipe", [IngredientInput(query="Food", grams=50)])
        food = await service.prepare_custom_food("Food", 100, 5, 4, 11,
            recipe_draft_ref=draft["action_ref"], ingredient_ref=draft["ingredients"][0]["ingredient_ref"])
        await service.update_recipe_draft(draft["action_ref"], portions=2)
        with pytest.raises(WorkflowError, match="Черновик рецепта изменился"):
            await service.confirm_action(food["action_ref"], 1, True)
        engine.create_custom_food_for_group.assert_not_awaited()
    run(scenario())


def test_discard_prevents_execution_and_removes_draft_from_list(setup):
    _, engine, service, _, _ = setup

    async def scenario():
        food = await service.prepare_custom_food("Food", 100, 5, 4, 11)
        assert (await service.discard_action(food["action_ref"]))["cancelled"]
        assert not (await service.list_drafts())["drafts"]
        with pytest.raises(WorkflowError, match="отменён"):
            await service.confirm_action(food["action_ref"], 2, True)
        engine.create_custom_food_for_group.assert_not_awaited()
    run(scenario())


def test_authenticated_http_mcp_protocol_and_tool_schemas(setup):
    import base64
    import hashlib
    import re
    from urllib.parse import parse_qs, urlsplit

    import httpx
    from fatsecret_bot.mcp_config import MCPConfig
    from fatsecret_bot.mcp_server import TOOLS, build_http_app

    storage, engine, service, _, _ = setup
    config = MCPConfig(service.group_id, "synthetic-connection-key-1234567890", "http://127.0.0.1:8765")
    app = build_http_app(storage, engine, config)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=config.issuer_url,
                    headers={"Accept": "application/json, text/event-stream"}) as http:
                denied = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert denied.status_code == 401
                assert "oauth-protected-resource" in denied.headers["www-authenticate"]
                protected = await http.get("/.well-known/oauth-protected-resource/mcp")
                assert protected.json()["resource"] == config.resource_url
                metadata = await http.get("/.well-known/oauth-authorization-server")
                assert "none" in metadata.json()["token_endpoint_auth_methods_supported"]
                registered = await http.post("/register", json={"redirect_uris": ["https://client.example.test/callback"],
                    "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "scope": "fatsecret:group"})
                assert registered.status_code == 201, registered.text
                client = registered.json()
                verifier = "v" * 64
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                redirect = await http.get("/authorize", params={"client_id": client["client_id"], "response_type": "code",
                    "redirect_uri": "https://client.example.test/callback", "code_challenge": challenge,
                    "code_challenge_method": "S256", "scope": "fatsecret:group", "resource": config.resource_url})
                page = await http.get(redirect.headers["location"])
                consent = await http.post("/consent", data={
                    "request": re.search(r"name='request' value='([^']+)'", page.text)[1],
                    "csrf": re.search(r"name='csrf' value='([^']+)'", page.text)[1],
                    "action": "approve", "group_key": config.group_key}, headers={"Origin": config.issuer_url})
                code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
                token = await http.post("/token", data={"grant_type": "authorization_code", "client_id": client["client_id"],
                    "code": code, "code_verifier": verifier, "redirect_uri": "https://client.example.test/callback", "resource": config.resource_url})
                assert token.status_code == 200, token.text
                http.headers["Authorization"] = f"Bearer {token.json()['access_token']}"

                async def rpc(method, params=None):
                    response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
                    assert response.status_code == 200, response.text
                    body = response.json()
                    assert "error" not in body, body
                    return body["result"]

                initialized = await rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "synthetic-test", "version": "1.0"}})
                assert initialized["serverInfo"]["name"] == "FatSecret"
                tools = (await rpc("tools/list"))["tools"]
                assert {t["name"] for t in tools} == set(TOOLS)
                assert next(t for t in tools if t["name"] == "confirm_action")["annotations"]["destructiveHint"]
                for tool in tools:
                    assert not {"group_id", "account_key", "password", "telegram_id"} & tool["inputSchema"]["properties"].keys()
                result = await rpc("tools/call", {"name": "get_group", "arguments": {}})
                assert result["structuredContent"]["group"] == "Test group"
                assert "synthetic-password" not in str(result)
                refused = await rpc("tools/call", {"name": "confirm_action", "arguments": {"action_ref": "unknown", "revision": 1}})
                assert refused["isError"]
                assert "подтверждение" in refused["content"][0]["text"]
                untrusted_host = await http.post("/mcp", headers={"Host": "untrusted.example.test"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert untrusted_host.status_code == 421
    run(scenario())
