"""Command-line installer and server runner."""

from __future__ import annotations

import argparse
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

import uvicorn

from .config import (
    BridgeConfig,
    ToolProfile,
    TrustMode,
    add_grant,
    default_config_path,
    load_config,
    save_config,
)
from .gateway import LocalGateway
from .mcp import create_app


def main() -> None:
    """CLI entrypoint."""

    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "init":
        _cmd_init(args)
    elif args.command == "grant":
        _cmd_grant(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "doctor":
        _cmd_doctor(args)
    elif args.command == "set-mode":
        _cmd_set_mode(args)
    elif args.command == "set-tool-profile":
        _cmd_set_tool_profile(args)
    elif args.command in {"enable-tasks", "enable-codex-tasks"}:
        _cmd_set_codex_tasks(args, enabled=True)
    elif args.command in {"disable-tasks", "disable-codex-tasks"}:
        _cmd_set_codex_tasks(args, enabled=False)
    elif args.command == "install-launch-agent":
        _cmd_install_launch_agent(args)
    elif args.command in {"print-setup", "print-chatgpt-setup"}:
        _cmd_print_chatgpt_setup(args)
    elif args.command == "tunnel-doctor":
        _cmd_tunnel_doctor(args)
    else:
        parser.print_help()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge",
        description="Universal local MCP bridge for AI agents (Gemini, Claude, ChatGPT, etc.).",
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="Create a secure empty config file")

    grant = subparsers.add_parser("grant", help="Add or replace a user-approved local path grant")
    grant.add_argument("path", type=Path)
    grant.add_argument("--name", default=None)
    grant.add_argument("--no-read", action="store_true")
    grant.add_argument("--write", action="store_true")
    grant.add_argument("--execute", action="store_true")

    run = subparsers.add_parser("run", help="Run the local MCP server")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])

    subparsers.add_parser("doctor", help="Check config and local dependencies")

    mode = subparsers.add_parser("set-mode", help="Set restricted or full_delegate trust mode")
    mode.add_argument("mode", choices=["restricted", "full_delegate"])

    profile = subparsers.add_parser(
        "set-tool-profile",
        help="Set full or delegate tool profile for AI agents",
    )
    profile.add_argument("profile", choices=["full", "delegate"])

    subparsers.add_parser("enable-tasks", help="Enable execution task tools")
    subparsers.add_parser("enable-codex-tasks", help="Compatibility alias for enable-tasks")
    subparsers.add_parser("disable-tasks", help="Disable execution task tools")
    subparsers.add_parser("disable-codex-tasks", help="Compatibility alias for disable-tasks")

    launch = subparsers.add_parser("install-launch-agent", help="Install a macOS launchd service")
    launch.add_argument("--label", default="com.bridge")
    launch.add_argument(
        "--load",
        action="store_true",
        help="Load and start the service after writing it",
    )

    setup = subparsers.add_parser("print-setup", help="Print AI connector setup text")
    setup.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    setup.add_argument(
        "--show-token",
        action="store_true",
        help="Print the local bearer token needed by connector setup",
    )

    legacy_setup = subparsers.add_parser("print-chatgpt-setup", help="Compatibility alias for print-setup")
    legacy_setup.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    legacy_setup.add_argument(
        "--show-token",
        action="store_true",
        help="Print the local bearer token needed by connector setup",
    )

    tunnel_doctor = subparsers.add_parser(
        "tunnel-doctor",
        help="Diagnose ChatGPT remote MCP tunnel setup without printing connector secrets",
    )
    tunnel_doctor.add_argument(
        "--url",
        default=None,
        help="ChatGPT connector URL or tunnel base URL to inspect; secret path is redacted.",
    )
    tunnel_doctor.add_argument(
        "--log",
        action="append",
        type=Path,
        default=[],
        help="Tunnel log file to scan for connection failures; can be passed more than once.",
    )
    return parser


def _cmd_init(args: argparse.Namespace) -> None:
    existing = load_config(args.config) if args.config.exists() else None
    new_cfg = BridgeConfig()
    if existing is not None:
        new_cfg = new_cfg.model_copy(
            update={
                "auth_token": existing.auth_token,
                "connector_secret": existing.connector_secret,
            }
        )
    path = save_config(new_cfg, args.config)
    print(f"Created secure empty config: {path}")
    print("Next: bridge grant ~/Projects --write --execute")


def _cmd_grant(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    updated = add_grant(
        config,
        path=args.path,
        name=args.name,
        read=not args.no_read,
        write=bool(args.write),
        execute=bool(args.execute),
    )
    path = save_config(updated, args.config)
    print(f"Updated config: {path}")
    print(f"Granted: {args.path.expanduser().resolve(strict=False)}")


def _apply_windows_socket_inheritance_protection() -> None:
    """Ensure listening sockets created by Uvicorn are marked non-inheritable on Windows."""
    if sys.platform != "win32":
        return

    import ctypes
    import socket
    from uvicorn.config import Config
    from uvicorn.server import Server

    handle_flag_inherit = 0x00000001
    try:
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            new_mode = (mode.value & ~0x0040) | 0x0080
            kernel32.SetConsoleMode(h_stdin, new_mode)
    except Exception:
        pass

    def _mark_non_inheritable(sock: socket.socket) -> None:
        try:
            sock.set_inheritable(False)
        except Exception:
            pass
        try:
            ctypes.windll.kernel32.SetHandleInformation(
                ctypes.c_void_p(sock.fileno()),
                handle_flag_inherit,
                0,
            )
        except Exception:
            pass

    orig_bind = Config.bind_socket

    def bind_socket_safe(self: Config) -> socket.socket:
        sock = orig_bind(self)
        _mark_non_inheritable(sock)
        return sock

    Config.bind_socket = bind_socket_safe  # type: ignore[method-assign]

    orig_startup = Server.startup

    async def startup_safe(self: Server, sockets: list[socket.socket] | None = None) -> None:
        await orig_startup(self, sockets=sockets)
        for s in getattr(self, "servers", []) or []:
            for sock in getattr(s, "sockets", []) or []:
                _mark_non_inheritable(sock)

    Server.startup = startup_safe  # type: ignore[method-assign]


def _cmd_run(args: argparse.Namespace) -> None:
    _apply_windows_socket_inheritance_protection()
    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    gateway = LocalGateway(config, config_path=args.config)
    app = create_app(gateway)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=args.log_level,
        timeout_keep_alive=75,
        backlog=2048,
    )


def _cmd_doctor(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    print(f"Config: {args.config}")
    print(f"Host: {config.host}")
    print(f"Port: {config.port}")
    print(f"Trust mode: {config.trust_mode}")
    print(f"Tool profile: {config.tool_profile}")
    print(f"Auth token: {'configured' if config.auth_token else 'missing'}")
    print(f"Codex tasks: {'enabled' if config.enable_codex_tasks else 'disabled'}")
    print(f"Grants: {len(config.grants)}")
    for grant in config.grants:
        status = "exists" if grant.resolved_path.exists() else "missing"
        print(
            f"- {grant.resolved_path} [{status}] "
            f"read={grant.read} write={grant.write} execute={grant.execute}"
        )
    print(f"codex: {shutil.which('codex') or 'missing'}")
    print(f"rg: {shutil.which('rg') or 'missing (using native Python search fallback)'}")
    print(f"cloudflared: {shutil.which('cloudflared') or 'optional, missing'}")


def _cmd_set_mode(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    mode: TrustMode = args.mode
    save_config(config.model_copy(update={"trust_mode": mode}), args.config)
    print(f"Set bridge trust mode: {mode}")
    if mode == "full_delegate":
        print("full_delegate allows broad local read, write, and execute access via the bridge.")


def _cmd_set_tool_profile(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    profile: ToolProfile = args.profile
    save_config(config.model_copy(update={"tool_profile": profile}), args.config)
    print(f"Set tool profile: {profile}")
    if profile == "delegate":
        print("delegate exposes one primary delegation tool plus read-only status tools.")


def _cmd_set_codex_tasks(args: argparse.Namespace, *, enabled: bool) -> None:
    config = load_config(args.config)
    save_config(config.model_copy(update={"enable_codex_tasks": enabled}), args.config)
    if enabled:
        print("Enabled delegation tools.")
    else:
        print("Disabled delegation tools.")


def _cmd_install_launch_agent(args: argparse.Namespace) -> None:
    if platform.system() != "Darwin":
        raise SystemExit("install-launch-agent currently supports macOS launchd only")
    executable = shutil.which("bridge") or shutil.which("codex-chatgpt-bridge") or str(Path(sys.argv[0]).resolve())
    label = str(args.label)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log_dir = Path.home() / "Library" / "Logs" / "bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": [
            executable,
            "--config",
            str(args.config),
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "bridge.out.log"),
        "StandardErrorPath": str(log_dir / "bridge.err.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
    print(f"Wrote launch agent: {plist_path}")
    if args.load:
        uid = str(_user_id())
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist_path)], check=False)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=True)
        subprocess.run(["launchctl", "enable", f"gui/{uid}/{label}"], check=True)
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=True)
        print(f"Started launch agent: {label}")
    else:
        print(f"Load it with: launchctl bootstrap gui/$(id -u) {plist_path}")


def _cmd_print_chatgpt_setup(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    connector_url = _connector_secret_url(args.url, config.connector_secret)
    safe_bearer_url = _redact_connector_url(args.url)
    token_line = (
        f"Bearer token: {config.auth_token}"
        if args.show_token
        else "Bearer token: stored in config; rerun with --show-token to display it locally."
    )
    connector_url_line = (
        f"Connector no-auth URL: {connector_url}"
        if args.show_token
        else (
            "Connector no-auth URL: stored in config; rerun with "
            "--show-token to display it locally."
        )
    )
    print(
        "\n".join(
            [
                "Bridge connector setup",
                "1. Start the bridge: bridge run",
                "2. AI web clients need an HTTPS tunnel for remote MCP connections.",
                "3. Use Cloudflare Tunnel, ngrok, or another HTTPS endpoint you control.",
                f"4. Bearer-protected MCP URL: {safe_bearer_url}",
                f"5. {connector_url_line}",
                "6. If the client supports HTTP Bearer, use the bearer-protected URL and paste the token.",
                f"7. {token_line}",
                "8. In any AI conversation (Gemini, Claude, ChatGPT, etc.), connect to this bridge.",
            ]
        )
    )


def _cmd_tunnel_doctor(args: argparse.Namespace) -> None:
    url = str(args.url or "").strip()
    print("Tunnel doctor")
    print("Recommended: OpenAI Secure MCP Tunnel")
    print("Fallback: named Cloudflare Tunnel or another stable HTTPS endpoint")
    print(f"tunnel-client: {shutil.which('tunnel-client') or 'missing'}")
    print(f"cloudflared: {shutil.which('cloudflared') or 'missing'}")
    if not url:
        print("URL: missing")
    else:
        print(f"URL: {_redact_connector_url(url)}")
        parsed = _split_diagnostic_url(url)
        print(f"HTTPS: {'ok' if parsed.scheme == 'https' else 'missing'}")
        host = parsed.hostname or ""
        if host.endswith(".trycloudflare.com"):
            print("trycloudflare.com quick tunnel: unstable")
        else:
            print("trycloudflare.com quick tunnel: not detected")
        if host in {"127.0.0.1", "localhost"}:
            print("remote reachability: localhost is not reachable from ChatGPT web")
    log_text, log_issues = _read_logs(args.log)
    for issue in log_issues:
        print(issue)
    if log_text:
        connection_failed = _contains_any(
            log_text,
            [
                "Connection failed",
                "Unable to establish connection",
                "Serve tunnel error",
                "Connection terminated",
            ],
        )
        timeout_7844 = "7844" in log_text and "timeout" in log_text.lower()
        print(f"connection failures: {'found' if connection_failed else 'not found'}")
        print(f"7844 timeout: {'found' if timeout_7844 else 'not found'}")
    else:
        print("connection failures: no logs provided")
        print("7844 timeout: no logs provided")


def _connector_secret_url(base_url: str, connector_secret: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/mcp"):
        return f"{trimmed}/{connector_secret}"
    return f"{trimmed}/mcp/{connector_secret}"


def _redact_connector_url(url: str) -> str:
    parsed = _split_diagnostic_url(url)
    netloc = _safe_netloc(parsed)
    path = _redacted_url_path(parsed.path)
    if netloc:
        prefix = f"{parsed.scheme}://{netloc}" if parsed.scheme else netloc
        return f"{prefix}{path}"
    return urlunsplit((parsed.scheme, "", path, "", ""))


def _split_diagnostic_url(url: str) -> SplitResult:
    raw_url = url.strip()
    if "://" in raw_url:
        return urlsplit(raw_url)
    first_part = raw_url.split("/", 1)[0]
    if first_part in {"localhost", "127.0.0.1"} or "." in first_part or ":" in first_part:
        return urlsplit(f"//{raw_url}")
    return urlsplit(raw_url)


def _safe_netloc(parsed: SplitResult) -> str:
    host = parsed.hostname or ""
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


def _redacted_url_path(path: str) -> str:
    path_parts = [part for part in path.split("/") if part]
    if "mcp" in path_parts:
        mcp_index = path_parts.index("mcp")
        redacted_parts = path_parts[: mcp_index + 1]
        if len(path_parts) > mcp_index + 1:
            redacted_parts.append("[redacted]")
        redacted_path = "/" + "/".join(redacted_parts)
        return redacted_path
    if path and path != "/":
        return "/[redacted]"
    return path


def _read_logs(paths: list[Path]) -> tuple[str, list[str]]:
    chunks: list[str] = []
    issues: list[str] = []
    for path in paths:
        if not path.exists():
            issues.append(f"log missing: {path}")
            continue
        if not path.is_file():
            issues.append(f"log not readable: {path}")
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            issues.append(f"log not readable: {path}")
    return "\n".join(chunks), issues


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)


def _user_id() -> int:
    if hasattr(Path, "home"):
        import os

        return os.getuid()
    raise RuntimeError("cannot determine user id")


if __name__ == "__main__":
    main()
