from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.routes import create_auth_routes
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette

from fatsecret_bot.mcp_auth import COOKIE, MCP_SCOPE, GroupOAuthProvider, auth_settings
from fatsecret_bot.mcp_config import MCPConfig
from fatsecret_bot.storage import Storage

CONFIG = MCPConfig("00000000-0000-4000-8000-000000000041", "synthetic-connection-key-1234567890", "http://127.0.0.1:8765")
CALLBACK = "https://client.example.test/callback"
VERIFIER = "synthetic-pkce-verifier-" + "x" * 32
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def make_app(provider):
    settings = auth_settings(provider.config)
    return Starlette(routes=provider.routes() + create_auth_routes(
        provider, settings.issuer_url,
        client_registration_options=settings.client_registration_options,
        revocation_options=settings.revocation_options,
    ))


@pytest.fixture
def setup(tmp_path):
    storage = Storage(tmp_path / "state.sqlite3")
    provider = GroupOAuthProvider(storage, CONFIG)
    yield storage, provider, make_app(provider)
    storage.close()


async def register(http, method="none"):
    response = await http.post("/register", json={
        "redirect_uris": [CALLBACK], "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"], "scope": MCP_SCOPE,
        "client_name": "Synthetic client",
    })
    assert response.status_code == 201, response.text
    return response.json()


async def begin(http, client, **changes):
    params = {
        "client_id": client["client_id"], "response_type": "code", "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "synthetic-state",
        "scope": MCP_SCOPE, "resource": CONFIG.resource_url,
    }
    params.update(changes)
    return await http.get("/authorize", params=params)


async def consent_form(http, client, config=CONFIG):
    response = await begin(http, client)
    assert response.status_code == 302, response.text
    page = await http.get(response.headers["location"])
    assert page.status_code == 200, page.text
    assert CONFIG.group_key not in page.text
    assert str(CONFIG.group_id) not in page.text
    assert CALLBACK not in page.text
    assert "Synthetic client" in page.text
    assert "https://client.example.test" in page.text
    assert "no-store" in page.headers["cache-control"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    return {
        "request": re.search(r"name='request' value='([^']+)'", page.text)[1],
        "csrf": re.search(r"name='csrf' value='([^']+)'", page.text)[1],
        "action": "approve", "group_key": config.group_key,
    }


async def code(http, client, config=CONFIG):
    form = await consent_form(http, client, config)
    response = await http.post("/consent", data=form, headers={"Origin": CONFIG.issuer_url})
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["synthetic-state"]
    return query["code"][0], form


def token_data(client, authorization_code):
    data = {
        "grant_type": "authorization_code", "client_id": client["client_id"],
        "code": authorization_code, "redirect_uri": CALLBACK,
        "code_verifier": VERIFIER, "resource": CONFIG.resource_url,
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    return data


def test_full_http_flow_pkce_audience_rotation_revocation_and_persistence(setup):
    storage, provider, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            client = await register(http, "client_secret_post")
            authorization_code, form = await code(http, client)
            # Consent submission is consumed, including after a browser replay.
            replay = await http.post("/consent", data=form, headers={"Origin": CONFIG.issuer_url})
            assert replay.status_code == 400
            request = token_data(client, authorization_code)
            wrong_pkce = await http.post("/token", data={**request, "code_verifier": "z" * 50})
            assert wrong_pkce.status_code == 400
            assert wrong_pkce.json()["error"] == "invalid_grant"
            wrong_resource = await http.post("/token", data={**request, "resource": "https://other.example.test/mcp"})
            assert wrong_resource.status_code == 400
            missing_resource = await http.post("/token", data={k: v for k, v in request.items() if k != "resource"})
            assert missing_resource.status_code == 400
            wrong_secret = await http.post("/token", data={**request, "client_secret": "wrong"})
            assert wrong_secret.status_code == 401
            issued = await http.post("/token", data=request)
            assert issued.status_code == 200, issued.text
            tokens = issued.json()
            assert issued.headers["cache-control"] == "no-store"
            token = await provider.load_access_token(tokens["access_token"])
            assert token.resource == CONFIG.resource_url
            assert token.scopes == [MCP_SCOPE]
            assert token.subject and str(CONFIG.group_id) != token.subject
            assert (await http.post("/token", data=request)).json()["error"] == "invalid_grant"
            # A fresh provider verifies hashed client credentials and loads tokens.
            restarted = GroupOAuthProvider(storage, CONFIG)
            assert (await restarted.get_client(client["client_id"])).client_secret is None
            assert restarted.verify_client_secret(client["client_id"], client["client_secret"])
            assert await restarted.load_access_token(tokens["access_token"]) is not None
            persisted = "\n".join(storage._conn.iterdump())
            for secret in [CONFIG.group_key, client["client_secret"], authorization_code,
                           tokens["access_token"], tokens["refresh_token"], form["csrf"]]:
                assert secret not in persisted
            refresh = {"grant_type": "refresh_token", "client_id": client["client_id"],
                       "client_secret": client["client_secret"], "refresh_token": tokens["refresh_token"],
                       "resource": CONFIG.resource_url}
            assert (await http.post("/token", data={**refresh, "resource": "https://other.example.test/mcp"})).status_code == 400
            assert (await http.post("/token", data={**refresh, "scope": "admin"})).json()["error"] == "invalid_scope"
            rotated = await http.post("/token", data=refresh)
            assert rotated.status_code == 200, rotated.text
            new_tokens = rotated.json()
            assert new_tokens["refresh_token"] != tokens["refresh_token"]
            assert await provider.load_access_token(tokens["access_token"]) is None
            assert await provider.load_access_token(new_tokens["access_token"]) is not None
            assert (await http.post("/token", data=refresh)).json()["error"] == "invalid_grant"
            revoked = await http.post("/revoke", data={
                "client_id": client["client_id"], "client_secret": client["client_secret"],
                "token": new_tokens["access_token"], "token_type_hint": "access_token",
            })
            assert revoked.status_code == 200
            assert await provider.load_access_token(new_tokens["access_token"]) is None
            assert await provider.load_refresh_token(await provider.get_client(client["client_id"]), new_tokens["refresh_token"]) is None

    asyncio.run(scenario())


def test_csrf_browser_binding_origin_denial_and_failed_key_budget(setup):
    storage, provider, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            client = await register(http)
            form = await consent_form(http, client)
            assert (await http.post("/consent", data=form)).status_code == 403
            assert (await http.post("/consent", data=form, headers={"Origin": "https://evil.example.test"})).status_code == 403
            assert (await http.post("/consent", data={**form, "csrf": "wrong"}, headers={"Origin": CONFIG.issuer_url})).status_code == 403
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as other:
                assert (await other.post("/consent", data=form, headers={"Origin": CONFIG.issuer_url})).status_code == 403
            denied = await http.post("/consent", data={**form, "action": "deny", "group_key": ""}, headers={"Origin": CONFIG.issuer_url})
            assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]
            assert storage._conn.execute("SELECT COUNT(*) FROM mcp_oauth_records WHERE kind='code'").fetchone()[0] == 0

            form = await consent_form(http, client)
            for _ in range(5):
                result = await http.post("/consent", data={**form, "group_key": "wrong"}, headers={"Origin": CONFIG.issuer_url})
                assert result.status_code == 403
                # Exact POST replay cannot add guesses or re-use a consumed nonce.
                assert (await http.post("/consent", data=form, headers={"Origin": CONFIG.issuer_url})).status_code in {403, 429}
                page = await http.get("/consent", params={"request": form["request"]})
                if page.status_code == 200:
                    form["csrf"] = re.search(r"name='csrf' value='([^']+)'", page.text)[1]
            assert page.status_code == 429
            assert (await http.post("/consent", data=form, headers={"Origin": CONFIG.issuer_url})).status_code == 429

    asyncio.run(scenario())


def test_group_key_issuer_and_client_isolation(setup):
    storage, provider, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            first, second = await register(http), await register(http)
            authorization_code, _ = await code(http, first)
            response = await http.post("/token", data=token_data(second, authorization_code))
            assert response.json()["error"] == "invalid_grant"
            issued = (await http.post("/token", data=token_data(first, authorization_code))).json()
            for config in [replace(CONFIG, group_id="00000000-0000-4000-8000-000000000042"), replace(CONFIG, group_key="another-synthetic-connection-key-1234567890"),
                           replace(CONFIG, issuer_url="https://other.example.test")]:
                isolated = GroupOAuthProvider(storage, config)
                assert (await isolated.get_client(first["client_id"]) is not None) == (config.issuer_url == CONFIG.issuer_url)
                assert await isolated.load_access_token(issued["access_token"]) is None
                assert await isolated.load_refresh_token(await provider.get_client(first["client_id"]), issued["refresh_token"]) is None
            assert await provider.load_access_token(issued["access_token"]) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
@pytest.mark.parametrize("changes", [
    {"group_id": "00000000-0000-4000-8000-000000000042"},
    {"group_key": "another-synthetic-connection-key-1234567890"},
    {"group_id": "00000000-0000-4000-8000-000000000042", "group_key": "another-synthetic-connection-key-1234567890"},
])
def test_registered_client_survives_group_or_key_change_but_requires_fresh_consent(setup, method, changes):
    storage, _, app = setup
    changed = replace(CONFIG, **changes)

    async def exchange(http, client, authorization_code):
        data, headers = token_data(client, authorization_code), {}
        if method == "client_secret_basic":
            data.pop("client_secret")
            credentials = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"
        return await http.post("/token", data=data, headers=headers)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            client = await register(http, method)
            previous_code, _ = await code(http, client)
            issued = (await exchange(http, client, previous_code)).json()
            unused_code, _ = await code(http, client)
            pending = await consent_form(http, client)
        restarted = GroupOAuthProvider(storage, changed)
        assert await restarted.load_access_token(issued["access_token"]) is None
        registered = await restarted.get_client(client["client_id"])
        assert registered is not None and [str(uri) for uri in registered.redirect_uris] == [CALLBACK]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(restarted)), base_url=CONFIG.issuer_url) as http:
            assert (await http.post("/consent", data=pending, headers={"Origin": CONFIG.issuer_url})).status_code == 400
            assert (await exchange(http, client, unused_code)).json()["error"] == "invalid_grant"
            assert await restarted.load_refresh_token(registered, issued["refresh_token"]) is None
            # A cached client cannot redirect consent to a newly supplied callback.
            invalid = await begin(http, client, redirect_uri="https://other.example.test/callback")
            assert invalid.status_code == 400 and "location" not in invalid.headers
            if "group_key" in changes:
                old_key_form = await consent_form(http, client)
                assert (await http.post("/consent", data=old_key_form, headers={"Origin": CONFIG.issuer_url})).status_code == 403
            new_code, _ = await code(http, client, changed)
            response = await exchange(http, client, new_code)
            assert response.status_code == 200, response.text
            assert await restarted.load_access_token(response.json()["access_token"]) is not None
            # Registered clients have no arbitrary 90-day expiry.
            with storage._conn:
                storage._conn.execute("UPDATE mcp_oauth_records SET expires=0")
            again = GroupOAuthProvider(storage, changed)
            assert await again.get_client(client["client_id"]) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
def test_legacy_client_migration_preserves_original_secret_and_redirects(setup, method):
    storage, provider, _ = setup
    client = OAuthClientInformationFull(
        client_id="synthetic-legacy-client", redirect_uris=[CALLBACK],
        token_endpoint_auth_method=method, grant_types=["authorization_code", "refresh_token"],
        scope=MCP_SCOPE, client_name="Synthetic client",
    )
    secret = provider._mac(f"client:{provider._binding}:{client.client_id}") if method != "none" else None
    with storage._conn:
        provider._put("client", client.client_id, client.model_dump(mode="json", exclude={"client_secret"}), time.time()+86400)
    # Run the one-time migration with the old configuration, then use the new one.
    GroupOAuthProvider(storage, CONFIG)
    changed = replace(CONFIG, group_id="00000000-0000-4000-8000-000000000042", group_key="another-synthetic-connection-key-1234567890")
    migrated = GroupOAuthProvider(storage, changed)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(migrated)), base_url=CONFIG.issuer_url) as http:
            metadata = json.loads(client.model_dump_json())
            if secret:
                metadata["client_secret"] = secret
            authorization_code, _ = await code(http, metadata, changed)
            data, headers = token_data(metadata, authorization_code), {}
            if method == "client_secret_basic":
                data.pop("client_secret")
                basic = base64.b64encode(f"{client.client_id}:{secret}".encode()).decode()
                headers["Authorization"] = f"Basic {basic}"
            if secret:
                invalid_data = {**data, "client_secret": "wrong"}
                invalid_headers = {"Authorization": "Basic " + base64.b64encode(f"{client.client_id}:wrong".encode()).decode()} if method == "client_secret_basic" else {}
                assert (await http.post("/token", data=invalid_data, headers=invalid_headers)).status_code == 401
            response = await http.post("/token", data=data, headers=headers)
            assert response.status_code == 200, response.text
            assert await migrated.load_access_token(response.json()["access_token"]) is not None
            if secret:
                assert secret not in "\n".join(storage._conn.iterdump())
            assert storage._conn.execute("SELECT COUNT(*) FROM mcp_oauth_records WHERE kind='client'").fetchone()[0] == 0

    asyncio.run(scenario())


def test_authorization_rejects_wrong_resource_scope_pkce_and_redirect(setup):
    _, _, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            client = await register(http)
            for changes, error in [
                ({"resource": "https://other.example.test/mcp"}, "invalid_request"),
                ({"resource": ""}, "invalid_request"),
                ({"scope": "admin"}, "invalid_scope"),
                ({"scope": ""}, "invalid_scope"),
                ({"code_challenge_method": "plain"}, "invalid_request"),
                ({"code_challenge": "short"}, "invalid_request"),
            ]:
                result = await begin(http, client, **changes)
                assert result.status_code == 302
                assert parse_qs(urlsplit(result.headers["location"]).query)["error"] == [error]
            redirect = await begin(http, client, redirect_uri="https://evil.example.test/callback")
            assert redirect.status_code == 400
            assert "location" not in redirect.headers

    asyncio.run(scenario())


def test_expiry_body_limits_and_single_use_concurrent_exchange(setup):
    storage, provider, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            client = await register(http)
            form = await consent_form(http, client)
            with storage._conn:
                storage._conn.execute("UPDATE mcp_oauth_records SET expires=0 WHERE kind='pending'")
            assert (await http.post("/consent", data=form, headers={"Origin": CONFIG.issuer_url})).status_code == 400
            authorization_code, _ = await code(http, client)
            with storage._conn:
                storage._conn.execute("UPDATE mcp_oauth_records SET expires=0 WHERE kind='code'")
            assert (await http.post("/token", data=token_data(client, authorization_code))).json()["error"] == "invalid_grant"
            authorization_code, _ = await code(http, client)
            results = await asyncio.gather(*[http.post("/token", data=token_data(client, authorization_code)) for _ in range(2)])
            assert sorted(r.status_code for r in results) == [200, 400]
            issued = next(r.json() for r in results if r.status_code == 200)
            with storage._conn:
                storage._conn.execute("UPDATE mcp_oauth_records SET expires=0 WHERE kind IN ('access','refresh')")
            assert await provider.load_access_token(issued["access_token"]) is None
            assert await provider.load_refresh_token(await provider.get_client(client["client_id"]), issued["refresh_token"]) is None
            assert (await http.post("/consent", content="x" * 9000, headers={"Origin": CONFIG.issuer_url})).status_code == 413
            assert (await http.post("/token", content="x" * 9000)).status_code == 413

    asyncio.run(scenario())


def test_registration_validation_and_persistent_rate_limit(setup):
    storage, provider, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            for changes in [{"redirect_uris": ["javascript:alert(1)"]}, {"redirect_uris": ["http://remote.example.test/callback"]},
                            {"scope": "admin"}, {"token_endpoint_auth_method": "private_key_jwt"}]:
                response = await http.post("/register", json={"redirect_uris": [CALLBACK], "scope": MCP_SCOPE, **changes})
                assert response.status_code == 400
            for _ in range(32):
                response = await http.post("/register", json={"redirect_uris": [CALLBACK], "scope": MCP_SCOPE})
                if response.status_code != 201:
                    break
            assert response.status_code == 400
            restarted = GroupOAuthProvider(storage, CONFIG)
            assert not restarted._budget("register", 32, 600)
            assert storage._conn.execute("SELECT COUNT(*) FROM mcp_oauth_records WHERE kind='limit'").fetchone()[0] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["none", "client_secret_basic"])
def test_public_and_basic_client_flow_and_revocation_ownership(setup, method):
    _, provider, app = setup

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CONFIG.issuer_url) as http:
            metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
            assert method in metadata["token_endpoint_auth_methods_supported"]
            assert metadata["code_challenge_methods_supported"] == ["S256"]
            client = await register(http, method)
            other = await register(http)
            authorization_code, _ = await code(http, client)
            data = token_data(client, authorization_code)
            headers = {}
            if method == "client_secret_basic":
                data.pop("client_secret")
                basic = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
                headers["Authorization"] = f"Basic {basic}"
            result = await http.post("/token", data=data, headers=headers)
            assert result.status_code == 200, result.text
            issued = result.json()
            assert (await http.post("/revoke", data={"client_id": other["client_id"], "token": issued["access_token"]})).status_code == 200
            assert await provider.load_access_token(issued["access_token"]) is not None
            revoke = await http.post("/revoke", data={"client_id": client["client_id"], "token": issued["refresh_token"]}, headers=headers)
            assert revoke.status_code == 200, revoke.text
            assert await provider.load_access_token(issued["access_token"]) is None
            assert (await http.post("/revoke", data={"client_id": client["client_id"], "token": "not-a-real-token"}, headers=headers)).status_code == 200

    asyncio.run(scenario())
