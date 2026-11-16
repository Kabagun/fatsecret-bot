"""Private group OAuth using the official MCP SDK authorization/PKCE handlers.

All SQLite operations are synchronous on Storage's owning event-loop thread. Only
digests of random credentials are persisted. Client registrations belong to the
issuer; authorizations and tokens remain bound to the group and connection key.
Mount ``routes()`` before SDK routes: its token guard closes the SDK's currently
unchecked token-request resource parameter without replacing its PKCE handling.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    OAuthAuthorizationServerProvider, RefreshToken, RegistrationError, TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

from .mcp_config import MCPConfig
from .storage import Storage

MCP_SCOPE = "fatsecret:group"
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 24 * 3600
AUTH_SECONDS = 600
CODE_SECONDS = 120
MAX_RECORDS = 4096
MAX_CLIENTS = 256
MAX_PENDING = 128
COOKIE = "fatsecret_mcp_consent"
SAFE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def auth_settings(config: MCPConfig) -> AuthSettings:
    return AuthSettings(
        issuer_url=config.issuer_url,
        resource_server_url=config.resource_url,
        validate_token_resource=True,
        required_scopes=[MCP_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class GroupClientAuthenticator(ClientAuthenticator):
    """Use the SDK's client authentication methods with hashed client secrets."""

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()
        client_id = form.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AuthenticationError("Missing client_id")
        client = await self.provider.get_client(client_id)
        if client is None:
            raise AuthenticationError("Invalid client_id")
        method = client.token_endpoint_auth_method
        if method == "none":
            return client
        if method == "client_secret_basic":
            header = request.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                raise AuthenticationError("Missing Basic authentication")
            try:
                credentials = base64.b64decode(header[6:], validate=True).decode("utf-8")
                basic_id, secret = (unquote(part) for part in credentials.split(":", 1))
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise AuthenticationError("Invalid Basic authentication") from None
            if basic_id != client_id:
                raise AuthenticationError("Client ID mismatch")
        elif method == "client_secret_post":
            secret = form.get("client_secret")
        else:
            raise AuthenticationError("Unsupported authentication method")
        if not isinstance(secret, str) or not self.provider.verify_client_secret(client_id, secret):
            raise AuthenticationError("Invalid client_secret")
        if client.client_secret_expires_at and client.client_secret_expires_at < int(time.time()):
            raise AuthenticationError("Client secret has expired")
        return client


class GroupOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, storage: Storage, config: MCPConfig) -> None:
        self.config = config
        self._conn = storage._conn
        self._key = config.group_key.encode()
        self._binding = self._mac(f"binding:{config.group_id}:{config.issuer_url}")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS mcp_oauth_records (
                binding TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL,
                payload TEXT NOT NULL, expires REAL NOT NULL, family TEXT,
                PRIMARY KEY(binding, kind, digest)
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_expiry ON mcp_oauth_records(expires);
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_family ON mcp_oauth_records(binding, family);
            CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                issuer TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                secret_digest TEXT, PRIMARY KEY(issuer, digest)
            );
        """)
        with self._conn:
            self._prune()
            self._migrate_legacy_clients()

    def _mac(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    def _prune(self) -> None:
        self._conn.execute("DELETE FROM mcp_oauth_records WHERE expires <= ?", (time.time(),))

    def _migrate_legacy_clients(self) -> None:
        # An old configuration may be used once for migration after a key change.
        # Recover only client metadata and a secret digest, never old grants.
        rows = self._conn.execute(
            "SELECT payload FROM mcp_oauth_records WHERE binding=? AND kind='client'", (self._binding,),
        ).fetchall()
        for row in rows:
            client = OAuthClientInformationFull.model_validate_json(row["payload"])
            secret = self._mac(f"client:{self._binding}:{client.client_id}") if client.token_endpoint_auth_method != "none" else None
            self._store_client(client, secret, legacy=True)
        self._conn.execute("DELETE FROM mcp_oauth_records WHERE binding=? AND kind='client'", (self._binding,))

    def _store_client(self, client: OAuthClientInformationFull, secret: str | None, *, legacy: bool = False) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO mcp_oauth_clients VALUES (?, ?, ?, ?)" if legacy else
            "INSERT INTO mcp_oauth_clients VALUES (?, ?, ?, ?)",
            (self.config.issuer_url, _digest(str(client.client_id)),
             client.model_dump_json(exclude={"client_secret"}), _digest(secret) if secret else None),
        )

    def _count(self, kind: str | None = None) -> int:
        clients = self._conn.execute("SELECT COUNT(*) FROM mcp_oauth_clients").fetchone()[0]
        if kind is None:
            return clients + self._conn.execute("SELECT COUNT(*) FROM mcp_oauth_records").fetchone()[0]
        if kind == "client":
            return clients
        return self._conn.execute("SELECT COUNT(*) FROM mcp_oauth_records WHERE kind = ?", (kind,)).fetchone()[0]

    def _put(self, kind: str, raw: str, payload: dict[str, Any], expires: float, family: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO mcp_oauth_records VALUES (?, ?, ?, ?, ?, ?)",
            (self._binding, kind, _digest(raw), json.dumps(payload, separators=(",", ":")), expires, family),
        )

    def _read(self, kind: str, raw: str) -> Any:
        if not raw or len(raw) > 4096:
            return None
        return self._conn.execute(
            "SELECT * FROM mcp_oauth_records WHERE binding = ? AND kind = ? AND digest = ? AND expires > ?",
            (self._binding, kind, _digest(raw), time.time()),
        ).fetchone()

    def _remove(self, kind: str, raw: str) -> bool:
        return self._conn.execute(
            "DELETE FROM mcp_oauth_records WHERE binding = ? AND kind = ? AND digest = ? AND expires > ?",
            (self._binding, kind, _digest(raw), time.time()),
        ).rowcount == 1

    def _budget(self, name: str, limit: int, seconds: int) -> bool:
        """A single bounded, persisted counter per action, independent of spoofable IPs."""
        with self._conn:
            self._prune()
            row = self._read("limit", name)
            count = json.loads(row["payload"])["count"] if row else 0
            if count >= limit:
                return False
            if row:
                self._remove("limit", name)
            self._put("limit", name, {"count": count + 1}, row["expires"] if row else time.time() + seconds)
        return True

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id or len(client_id) > 4096:
            return None
        row = self._conn.execute(
            "SELECT payload FROM mcp_oauth_clients WHERE issuer=? AND digest=?",
            (self.config.issuer_url, _digest(client_id)),
        ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["payload"])

    def verify_client_secret(self, client_id: str, secret: str) -> bool:
        row = self._conn.execute(
            "SELECT secret_digest FROM mcp_oauth_clients WHERE issuer=? AND digest=?",
            (self.config.issuer_url, _digest(client_id)),
        ).fetchone()
        return bool(row and row["secret_digest"] and hmac.compare_digest(row["secret_digest"], _digest(secret)))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self._budget("register", 32, 600):
            raise RegistrationError("invalid_client_metadata", "Registration temporarily unavailable")
        if (
            not client_info.client_id
            or client_info.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}
            or set((client_info.scope or "").split()) != {MCP_SCOPE}
            or not client_info.redirect_uris
            or len(client_info.redirect_uris) > 8
            or len(client_info.model_dump_json()) > 8192
        ):
            raise RegistrationError("invalid_client_metadata", "Unsupported client metadata")
        # The SDK validates exact redirect matches at authorization and exchange.
        # Reject web-unsafe callback schemes during registration as well.
        for uri in client_info.redirect_uris:
            if uri.scheme not in {"https", "http"} or uri.fragment or uri.username or uri.password:
                raise RegistrationError("invalid_redirect_uri", "A web callback without credentials or fragment is required")
            if uri.scheme == "http" and uri.host not in {"localhost", "127.0.0.1", "[::1]", "::1"}:
                raise RegistrationError("invalid_redirect_uri", "Remote callbacks require HTTPS")
        with self._conn:
            self._prune()
            if self._count("client") >= MAX_CLIENTS or self._count() >= MAX_RECORDS:
                raise RegistrationError("invalid_client_metadata", "Registration temporarily unavailable")
            self._store_client(client_info, client_info.client_secret)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource != self.config.resource_url:
            raise AuthorizeError("invalid_request", "The requested resource is not available")
        if params.scopes is not None and set(params.scopes) != {MCP_SCOPE}:
            raise AuthorizeError("invalid_scope", "The requested scope is not available")
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge)
            or len(params.state or "") > 2048
            or len(str(params.redirect_uri)) > 2048
        ):
            raise AuthorizeError("invalid_request", "Invalid authorization parameters")
        if not self._budget("authorize", 60, 600):
            raise AuthorizeError("temporarily_unavailable", "Please try again later")
        request_id = secrets.token_urlsafe(32)
        with self._conn:
            self._prune()
            if self._count("pending") >= MAX_PENDING or self._count() >= MAX_RECORDS:
                raise AuthorizeError("temporarily_unavailable", "Please try again later")
            self._put("pending", request_id, {
                "client_id": client.client_id, "params": params.model_dump(mode="json"), "attempts": 0,
            }, time.time() + AUTH_SECONDS)
        return f"{self.config.issuer_url}/consent?{urlencode({'request': request_id})}"

    def routes(self) -> list[Route]:
        """Prepend these routes to the FastMCP application's router."""
        return [
            Route("/.well-known/oauth-authorization-server", endpoint=cors_middleware(self._metadata, ["GET", "OPTIONS"]),
                  methods=["GET", "OPTIONS"]),
            Route("/consent", endpoint=RequestBodyLimitMiddleware(request_response(self._consent), 8192),
                  methods=["GET", "POST"]),
            Route("/token", endpoint=RequestBodyLimitMiddleware(cors_middleware(self._token, ["POST", "OPTIONS"]), 8192),
                  methods=["POST", "OPTIONS"]),
            Route("/revoke", endpoint=RequestBodyLimitMiddleware(cors_middleware(self._revoke, ["POST", "OPTIONS"]), 8192),
                  methods=["POST", "OPTIONS"]),
        ]

    async def _metadata(self, request: Request) -> Response:
        settings = auth_settings(self.config)
        metadata = build_metadata(settings.issuer_url, None, settings.client_registration_options, settings.revocation_options)
        metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
        metadata.revocation_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
        return JSONResponse(metadata.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "no-store"})

    async def _revoke(self, request: Request) -> Response:
        # SDK 1.30's revocation model requires client_secret even for public
        # clients. Keep its authenticator, but accept the standard public form.
        form = await request.form()
        if any(len(form.getlist(k)) != 1 for k in form) or not isinstance(form.get("token"), str):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=SAFE_HEADERS)
        try:
            client = await GroupClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({"error": "unauthorized_client"}, status_code=401, headers=SAFE_HEADERS)
        raw = str(form["token"])
        token = await self.load_access_token(raw) or await self.load_refresh_token(client, raw)
        if token and token.client_id == client.client_id:
            await self.revoke_token(token)
        return Response(status_code=200, headers=SAFE_HEADERS)

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        if any(len(form.getlist(k)) != 1 for k in form):
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=SAFE_HEADERS)
        if form.get("resource") != self.config.resource_url:
            return JSONResponse({"error": "invalid_request", "error_description": "Invalid resource"},
                                status_code=400, headers=SAFE_HEADERS)
        if form.get("grant_type") == "authorization_code" and not re.fullmatch(
            r"[A-Za-z0-9._~-]{43,128}", str(form.get("code_verifier", "")),
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=SAFE_HEADERS)
        return await TokenHandler(self, GroupClientAuthenticator(self)).handle(request)

    def _failure(self, status: int = 400) -> Response:
        return HTMLResponse("<html lang='ru'><meta charset='utf-8'><title>Подключение</title>"
                            "<p>Подключение не подтверждено. Начните подключение заново.</p></html>",
                            status_code=status, headers=SAFE_HEADERS)

    async def _consent(self, request: Request) -> Response:
        if request.method == "POST":
            if request.headers.get("origin") != self.config.issuer_url:
                return self._failure(403)
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                return self._failure()
            form = await request.form()
            if any(len(form.getlist(k)) != 1 for k in form):
                return self._failure()
            request_id = str(form.get("request", ""))
        else:
            if len(request.query_params.getlist("request")) != 1:
                return self._failure()
            request_id = request.query_params.get("request", "")
        row = self._read("pending", request_id)
        if row is None:
            return self._failure()
        data = json.loads(row["payload"])
        if data["attempts"] >= 5:
            return self._failure(429)
        if request.method == "GET":
            client = await self.get_client(data["client_id"])
            if client is None:
                return self._failure()
            params = AuthorizationParams.model_validate(data["params"])
            callback = urlsplit(str(params.redirect_uri))
            client_name = html.escape(client.client_name or "MCP-клиент", quote=True)
            callback_origin = html.escape(f"{callback.scheme}://{callback.netloc}", quote=True)
            csrf, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            data.update(csrf=_digest(csrf), cookie=_digest(cookie))
            with self._conn:
                self._remove("pending", request_id)
                self._put("pending", request_id, data, row["expires"])
            response = HTMLResponse(
                "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<title>Подключение FatSecret</title><h1>Подключение FatSecret</h1>"
                "<p>Разрешите доступ к рецептам и действиям вашей группы. Введите ключ подключения группы.</p>"
                f"<p>Клиент: <strong>{client_name}</strong><br>Адрес возврата: {callback_origin}</p>"
                "<form method='post' action='/consent'>"
                f"<input type='hidden' name='request' value='{html.escape(request_id, quote=True)}'>"
                f"<input type='hidden' name='csrf' value='{csrf}'>"
                "<label>Ключ подключения <input type='password' name='group_key' required maxlength='1024' "
                "autocomplete='off'></label><p><button type='submit' name='action' value='approve'>Разрешить доступ</button> "
                "<button type='submit' name='action' value='deny' formnovalidate>Отказаться</button></p></form></html>",
                headers=SAFE_HEADERS,
            )
            response.set_cookie(COOKIE, cookie, max_age=AUTH_SECONDS, httponly=True,
                                secure=self.config.issuer_url.startswith("https:"), samesite="lax", path="/consent")
            return response
        if (
            not hmac.compare_digest(_digest(str(form.get("csrf", ""))), data.get("csrf", ""))
            or not hmac.compare_digest(_digest(request.cookies.get(COOKIE, "")), data.get("cookie", ""))
        ):
            return self._failure(403)
        if not self._budget("consent", 30, 600):
            return self._failure(429)
        params = AuthorizationParams.model_validate(data["params"])
        action = form.get("action")
        approved = action == "approve" and hmac.compare_digest(
            _digest(str(form.get("group_key", ""))), _digest(self.config.group_key),
        )
        with self._conn:
            # Consume exactly the bound form version; no await occurs in this transaction.
            current = self._read("pending", request_id)
            if current is None or current["payload"] != row["payload"] or not self._remove("pending", request_id):
                return self._failure()
            if not approved and action != "deny":
                data["attempts"] += 1
                data.pop("csrf", None)
                data.pop("cookie", None)
                self._put("pending", request_id, data, row["expires"])
                return self._failure(403)
            if approved:
                if self._count() >= MAX_RECORDS:
                    return self._failure(429)
                code = secrets.token_urlsafe(32)
                authorization = AuthorizationCode(
                    code=code, client_id=data["client_id"], scopes=[MCP_SCOPE],
                    expires_at=time.time() + CODE_SECONDS, subject=self._binding,
                    **params.model_dump(exclude={"state", "scopes"}),
                )
                self._put("code", code, authorization.model_dump(mode="json", exclude={"code"}), authorization.expires_at)
                target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
            else:
                target = construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)
        response = RedirectResponse(target, status_code=303, headers=SAFE_HEADERS)
        response.delete_cookie(COOKIE, path="/consent")
        return response

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        row = self._read("code", authorization_code)
        if row is None:
            return None
        data = json.loads(row["payload"])
        if data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    def _issue(self, client_id: str, scopes: list[str], family: str | None = None) -> OAuthToken:
        self._prune()
        if self._count() + 2 > MAX_RECORDS:
            raise TokenError("invalid_grant", "Please reconnect later")
        family = family or secrets.token_hex(32)
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        data = {"client_id": client_id, "scopes": scopes, "resource": self.config.resource_url, "subject": self._binding}
        access_expiry = int(time.time()) + ACCESS_SECONDS
        refresh_expiry = int(time.time()) + REFRESH_SECONDS
        self._put("access", access, {**data, "expires_at": access_expiry}, access_expiry, family)
        self._put("refresh", refresh, {**data, "expires_at": refresh_expiry}, refresh_expiry, family)
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=ACCESS_SECONDS, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        with self._conn:
            row = self._read("code", authorization_code.code)
            stored = json.loads(row["payload"]) if row else None
            if stored is None or stored["client_id"] != client.client_id or not self._remove("code", authorization_code.code):
                raise TokenError("invalid_grant", "Authorization code is invalid or already used")
            return self._issue(str(client.client_id), stored["scopes"])

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self._read("refresh", refresh_token)
        if row is None:
            return None
        data = json.loads(row["payload"])
        if data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **data)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self._conn:
            row = self._read("refresh", refresh_token.token)
            if row is None or json.loads(row["payload"])["client_id"] != client.client_id:
                raise TokenError("invalid_grant", "Refresh token is invalid or already used")
            original = json.loads(row["payload"])
            if set(scopes) != {MCP_SCOPE} or not set(scopes).issubset(original["scopes"]):
                raise TokenError("invalid_scope", "Invalid scope")
            self._conn.execute("DELETE FROM mcp_oauth_records WHERE binding = ? AND family = ?", (self._binding, row["family"]))
            return self._issue(str(client.client_id), scopes, row["family"])

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._read("access", token)
        if row is None:
            return None
        data = json.loads(row["payload"])
        if (
            data.get("resource") != self.config.resource_url or data.get("subject") != self._binding
            or set(data.get("scopes", [])) != {MCP_SCOPE}
        ):
            return None
        return AccessToken(token=token, **data)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        row = self._read("access" if isinstance(token, AccessToken) else "refresh", token.token)
        if row:
            with self._conn:
                self._conn.execute("DELETE FROM mcp_oauth_records WHERE binding = ? AND family = ?", (self._binding, row["family"]))
