"""Configuration loading and writing for Bridge."""

from __future__ import annotations

import tomllib
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

TrustMode = Literal["restricted", "full_delegate"]
ToolProfile = Literal["full", "delegate"]


class BridgeGrant(BaseModel):
    """One user-approved local path grant."""

    path: Path
    name: str | None = None
    read: bool = True
    write: bool = False
    execute: bool = False

    @property
    def resolved_path(self) -> Path:
        """Return the expanded, absolute path without requiring it to exist."""

        return self.path.expanduser().resolve(strict=False)


class BridgeConfig(BaseModel):
    """Runtime configuration for the local bridge."""

    host: str = "127.0.0.1"
    port: int = 8766
    auth_token: str = Field(default_factory=lambda: token_urlsafe(32))
    connector_secret: str = Field(default_factory=lambda: token_urlsafe(32))
    trust_mode: TrustMode = "full_delegate"
    tool_profile: ToolProfile = "full"
    enable_tasks: bool = True
    enable_codex_tasks: bool = True
    max_file_chars: int = 10_000_000
    max_write_chars: int = 20_000_000
    grants: list[BridgeGrant] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sync_task_flags(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "enable_tasks" in data and "enable_codex_tasks" not in data:
                data["enable_codex_tasks"] = data["enable_tasks"]
            elif "enable_codex_tasks" in data and "enable_tasks" not in data:
                data["enable_tasks"] = data["enable_codex_tasks"]
        return data


def default_config_path() -> Path:
    """Return the per-user config path."""

    primary = Path.home() / ".bridge" / "config.toml"
    legacy = Path.home() / ".codex-chatgpt-bridge" / "config.toml"
    if not primary.exists() and legacy.exists():
        return legacy
    return primary


def default_config() -> BridgeConfig:
    """Return a secure empty config."""

    return BridgeConfig()


def load_config(path: Path | None = None) -> BridgeConfig:
    """Load bridge config from TOML, returning the secure default if missing."""

    config_path = path or default_config_path()
    if not config_path.exists():
        return default_config()
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return BridgeConfig.model_validate(data)


def save_config(config: BridgeConfig, path: Path | None = None) -> Path:
    """Write bridge config as TOML and return the path."""

    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_to_toml(config), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path


def add_grant(
    config: BridgeConfig,
    *,
    path: Path,
    name: str | None,
    read: bool,
    write: bool,
    execute: bool,
) -> BridgeConfig:
    """Return a config with a grant added or replaced by resolved path."""

    grant = BridgeGrant(path=path, name=name, read=read, write=write, execute=execute)
    grant_path = grant.resolved_path
    grants = [item for item in config.grants if item.resolved_path != grant_path]
    grants.append(grant)
    return config.model_copy(update={"grants": grants})


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _to_toml(config: BridgeConfig) -> str:
    lines = [
        f"host = {_quote(config.host)}",
        f"port = {config.port}",
        f"auth_token = {_quote(config.auth_token)}",
        f"connector_secret = {_quote(config.connector_secret)}",
        f"trust_mode = {_quote(config.trust_mode)}",
        f"tool_profile = {_quote(config.tool_profile)}",
        f"enable_tasks = {str(config.enable_tasks).lower()}",
        f"enable_codex_tasks = {str(config.enable_codex_tasks).lower()}",
        f"max_file_chars = {config.max_file_chars}",
        f"max_write_chars = {config.max_write_chars}",
        "",
    ]
    for grant in config.grants:
        lines.append("[[grants]]")
        lines.append(f"path = {_quote(str(grant.resolved_path))}")
        if grant.name:
            lines.append(f"name = {_quote(grant.name)}")
        lines.append(f"read = {str(grant.read).lower()}")
        lines.append(f"write = {str(grant.write).lower()}")
        lines.append(f"execute = {str(grant.execute).lower()}")
        lines.append("")
    return "\n".join(lines)
