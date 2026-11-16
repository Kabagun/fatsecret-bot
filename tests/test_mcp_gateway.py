from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fatsecret_bot.mcp_gateway import build_gateway


def test_gateway_preserves_oauth_headers_cookies_query_and_body():
    async def handler(request: Request):
        if request.url.path == "/.well-known/oauth-authorization-server":
            return JSONResponse({"issuer": "https://mcp.example.test"})
        response = JSONResponse({"host": request.headers["host"], "query": request.scope["query_string"].decode(),
            "body": (await request.body()).decode(), "authorization": request.headers.get("authorization"),
            "origin": request.headers.get("origin"), "cookie": request.headers.get("cookie"),
            "hop": request.headers.get("x-hop")}, status_code=401,
            headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"',
                     "Connection": "x-hop", "x-hop": "removed"})
        response.set_cookie("consent", "synthetic", secure=True, httponly=True)
        response.set_cookie("second", "synthetic", secure=True)
        return response
    backend = Starlette(routes=[Route("/{path:path}", handler, methods=["GET", "POST"])])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(backend)) as upstream:
            gateway = build_gateway("http://127.0.0.1:8765", client=upstream)
            async with gateway.router.lifespan_context(gateway):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url="https://mcp.example.test") as client:
                    assert (await client.get("/")).json()["status"] == "ok"
                    response = await client.post("/token?resource=a%2Fb&state=one%2Btwo", content="code=synthetic",
                        headers={"Authorization": "Bearer synthetic", "Origin": "https://client.example.test",
                                 "Cookie": "browser=synthetic", "Connection": "x-hop", "x-hop": "removed"})
                    assert response.status_code == 401
                    assert "resource_metadata" in response.headers["www-authenticate"]
                    assert len(response.headers.get_list("set-cookie")) == 2
                    assert "x-hop" not in response.headers
                    assert response.json() == {"host": "mcp.example.test", "query": "resource=a%2Fb&state=one%2Btwo",
                        "body": "code=synthetic", "authorization": "Bearer synthetic", "origin": "https://client.example.test",
                        "cookie": "browser=synthetic", "hop": None}
    asyncio.run(scenario())


def test_gateway_unavailable_is_reported_without_upstream_details():
    async def fail(request):
        raise httpx.ConnectError("synthetic internal network detail")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as upstream:
            gateway = build_gateway("http://127.0.0.1:8765", client=upstream)
            async with gateway.router.lifespan_context(gateway):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url="https://mcp.example.test") as client:
                    for method, url in [("GET", "/"), ("POST", "/mcp")]:
                        response = await client.request(method, url)
                        assert response.status_code == 503
                        assert "internal" not in response.text
    asyncio.run(scenario())


@pytest.mark.parametrize("upstream", ["https://example.test", "http://example.test", "http://127.0.0.1/path",
                                     "http://user:password@localhost", "http://127.0.0.1?x=1"])
def test_gateway_is_never_an_arbitrary_proxy(upstream):
    with pytest.raises(ValueError):
        build_gateway(upstream)
