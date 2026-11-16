"""Credential-free HTTP gateway for hosts with separately managed web applications."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

_HOP_HEADERS = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
                b"te", b"trailer", b"transfer-encoding", b"upgrade"}


def _headers(values: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    excluded = set(_HOP_HEADERS)
    for name, value in values:
        if name.lower() == b"connection":
            excluded.update(part.strip().lower() for part in value.split(b","))
    return [(name, value) for name, value in values if name.lower() not in excluded]


def build_gateway(upstream: str, *, client: httpx.AsyncClient | None = None) -> Starlette:
    parsed = urlsplit(upstream)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("MCP gateway upstream must be a fixed loopback HTTP origin")
    base = httpx.URL(upstream)

    @asynccontextmanager
    async def lifespan(app):
        if client is not None:
            app.state.client = client
            yield
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=None), follow_redirects=False,
                                         trust_env=False) as connection:
                app.state.client = connection
                yield

    async def health(request: Request):
        try:
            response = await request.app.state.client.get(base.copy_with(path="/.well-known/oauth-authorization-server"))
            if response.status_code == 200:
                return JSONResponse({"service": "fatsecret-mcp", "status": "ok"})
        except httpx.HTTPError:
            pass
        return JSONResponse({"service": "fatsecret-mcp", "status": "unavailable"}, status_code=503)

    async def forward(request: Request):
        raw_path = request.scope.get("raw_path", request.url.path.encode())
        if request.scope["query_string"]:
            raw_path += b"?" + request.scope["query_string"]
        target = base.copy_with(raw_path=raw_path)
        connection = request.app.state.client
        outgoing = connection.build_request(request.method, target, headers=_headers(request.headers.raw), content=request.stream())
        try:
            incoming = await connection.send(outgoing, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "MCP service temporarily unavailable"}, status_code=503)
        response = StreamingResponse(incoming.aiter_raw(), status_code=incoming.status_code,
                                     background=BackgroundTask(incoming.aclose))
        response.raw_headers = _headers(incoming.headers.raw)
        return response

    return Starlette(routes=[Route("/", health, methods=["GET", "HEAD"]),
                             Route("/{path:path}", forward, methods=["GET", "HEAD", "POST", "DELETE", "OPTIONS"])],
                     lifespan=lifespan)


def main() -> None:
    upstream = os.environ.get("FATSECRET_MCP_UPSTREAM", "http://127.0.0.1:8765")
    app = build_gateway(upstream)
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
