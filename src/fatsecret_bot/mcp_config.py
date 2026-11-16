"""Explicit, independent configuration for the private MCP endpoint."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import AnyHttpUrl, ValidationError


@dataclass(frozen=True)
class MCPConfig:
    group_id: str = field(repr=False)
    group_key: str = field(repr=False)
    issuer_url: str
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self) -> None:
        if (
            not isinstance(self.group_id, str) or not self.group_id or len(self.group_id) > 128
            or any(c.isspace() or ord(c) < 32 for c in self.group_id)
        ):
            raise ValueError("FATSECRET_MCP_GROUP_ID must be a nonempty group identifier without whitespace")
        if len(self.group_key) < 32 or len(self.group_key) > 1024 or self.group_key != self.group_key.strip():
            raise ValueError("FATSECRET_MCP_GROUP_KEY must contain 32 to 1024 characters without outer whitespace")
        try:
            if any(c.isspace() or ord(c) < 32 for c in self.issuer_url):
                raise ValueError("Whitespace in issuer URL")
            parsed = urlsplit(self.issuer_url)
            parsed.port
            canonical_issuer = AnyHttpUrl(self.issuer_url)
        except (ValueError, ValidationError) as exc:
            raise ValueError("FATSECRET_MCP_ISSUER_URL must be a valid HTTPS origin") from exc
        # These are also the loopback names supported by the SDK's issuer validator.
        loopback = parsed.hostname in {"localhost", "127.0.0.1"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        ):
            raise ValueError("FATSECRET_MCP_ISSUER_URL requires HTTPS (HTTP is allowed only on loopback)")
        if not self.host or any(c.isspace() for c in self.host) or "/" in self.host:
            raise ValueError("FATSECRET_MCP_HOST must be a bind host")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("FATSECRET_MCP_PORT must be between 1 and 65535")
        object.__setattr__(self, "issuer_url", str(canonical_issuer).rstrip("/"))

    @property
    def resource_url(self) -> str:
        return f"{self.issuer_url}/mcp"


def load_mcp_config(env_file: str | Path = ".env", *, required: bool = False) -> MCPConfig | None:
    load_dotenv(env_file)
    enabled = os.environ.get("FATSECRET_MCP_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise RuntimeError("FATSECRET_MCP_ENABLED must be a boolean")
    if not required and enabled not in {"true", "1", "yes", "on"}:
        return None

    def value(name: str, default: str = "") -> str:
        result = os.environ.get(name, default).strip()
        if not result:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return result

    group_id = value("FATSECRET_MCP_GROUP_ID")
    try:
        port = int(value("FATSECRET_MCP_PORT", "8765"))
    except ValueError as exc:
        raise RuntimeError("FATSECRET_MCP_PORT must be between 1 and 65535") from exc
    try:
        return MCPConfig(
            group_id=group_id,
            group_key=os.environ.get("FATSECRET_MCP_GROUP_KEY", ""),
            issuer_url=value("FATSECRET_MCP_ISSUER_URL"),
            host=value("FATSECRET_MCP_HOST", "127.0.0.1"),
            port=port,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
