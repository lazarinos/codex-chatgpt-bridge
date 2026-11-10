"""Local gateway used by MCP tools."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import fnmatch
import io
import json
import mimetypes
import os
import platform
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .codex_sessions import (
    CodexSessionRecord,
    CodexSessionStore,
    default_session_store_path,
    extract_codex_session_id,
)
from .config import (
    BridgeConfig,
    BridgeGrant,
    TrustMode,
    add_grant,
    default_config_path,
    load_config,
    save_config,
)
from .live_inbox import LiveInboxStore, default_inbox_path
from .process_runner import BackgroundJobManager, ShellKind, kill_process_tree, run_argv_captured
from .security import is_relative_to, is_sensitive_path, resolve_user_path

SandboxMode = Literal["full_access", "read_only", "workspace_write", "danger_full_access"]
CodexSessionSandboxMode = Literal["full_access", "read_only", "workspace_write", "danger_full_access"]
BridgeSessionSandboxMode = CodexSessionSandboxMode
DelegatePermissionLevel = Literal[
    "read_only",
    "local_file_write",
    "repo_edit",
    "shell_command",
    "gui_control",
    "full_local",
]
ProcessAction = Literal[
    "list_jobs",
    "job_status",
    "kill_job",
    "send_input",
    "clear_finished",
    "system_processes",
    "kill_pid",
    "restart_bridge",
]
FileOpAction = Literal[
    "stat",
    "mkdir",
    "delete",
    "move",
    "copy",
    "read_lines",
    "head",
    "tail",
]
EditMode = Literal["replace", "replace_lines", "append", "prepend"]

JsonObject = dict[str, Any]
DELEGATE_PERMISSION_LEVELS: set[str] = {
    "read_only",
    "local_file_write",
    "repo_edit",
    "shell_command",
    "gui_control",
    "full_local",
}
DELEGATE_DEFAULT_SANDBOX: dict[str, CodexSessionSandboxMode] = {
    "read_only": "read_only",
    "local_file_write": "workspace_write",
    "repo_edit": "workspace_write",
    "shell_command": "danger_full_access",
    "gui_control": "danger_full_access",
    "full_local": "danger_full_access",
}
SANDBOX_RANK: dict[str, int] = {
    "read_only": 0,
    "workspace_write": 1,
    "full_access": 2,
    "danger_full_access": 2,
}
_IGNORED_SEARCH_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "target",
}
_BINARY_EXTENSIONS: set[str] = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".obj",
    ".o",
    ".a",
    ".lib",
    ".pdb",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".mp3",
    ".mp4",
    ".wav",
    ".ogg",
    ".avi",
    ".mov",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".pyc",
    ".pyo",
    ".vpk",
    ".bsp",
}
_LINE_RANGE_RE = re.compile(r"^(?P<path>.+?)#L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")


def find_rg_path() -> str | None:
    """Find the ripgrep binary on PATH or common Windows package locations."""
    found = shutil.which("rg")
    if found:
        return found
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            link = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "rg.exe"
            if link.exists():
                return str(link)
            pkg_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            if pkg_root.exists():
                for candidate in pkg_root.glob("BurntSushi.ripgrep*/**/rg.exe"):
                    if candidate.exists():
                        return str(candidate)
        for path_str in (
            r"C:\Program Files\ripgrep\rg.exe",
            r"C:\ProgramData\chocolatey\bin\rg.exe",
        ):
            if Path(path_str).exists():
                return path_str
    return None


class BridgeError(RuntimeError):
    """User-facing bridge error."""


class LocalGateway:
    """Authorize local actions and execute them for the MCP layer."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        config_path: Path | None = None,
        session_store_path: Path | None = None,
    ) -> None:
        self._config = config
        self._config_path = config_path or default_config_path()
        self._has_explicit_config_path = config_path is not None
        self._config_mtime_ns = self._get_config_mtime_ns()
        self._session_store = CodexSessionStore(
            session_store_path or default_session_store_path(self._config_path)
        )
        self._inbox_store = LiveInboxStore(default_inbox_path(self._config_path))
        self._job_manager = BackgroundJobManager(max_buffer_chars=5_000_000)

    def _get_config_mtime_ns(self) -> int | None:
        if not self._has_explicit_config_path:
            return None
        with contextlib.suppress(OSError):
            if self._config_path.exists():
                return self._config_path.stat().st_mtime_ns
        return None

    def _maybe_reload_config(self) -> None:
        if not self._has_explicit_config_path:
            return
        current_mtime = self._get_config_mtime_ns()
        if current_mtime is not None and current_mtime != self._config_mtime_ns:
            with contextlib.suppress(Exception):
                reloaded = load_config(self._config_path)
                # Keep active connector_secret and auth_token in memory so active tunnels never 401
                self._config = reloaded.model_copy(
                    update={
                        "auth_token": self._config.auth_token,
                        "connector_secret": self._config.connector_secret,
                    }
                )
                self._config_mtime_ns = current_mtime

    @property
    def config(self) -> BridgeConfig:
        """Return runtime config (auto-reloading if modified on disk)."""

        self._maybe_reload_config()
        return self._config

    async def bridge_status(self) -> JsonObject:
        """Return bridge status and capabilities."""

        self._maybe_reload_config()
        return {
            "status": "ok",
            "host": self._config.host,
            "port": self._config.port,
            "trust_mode": self._config.trust_mode,
            "tool_profile": self._config.tool_profile,
            "grant_count": len(self._config.grants),
            "auth_required": True,
            "tasks_enabled": self._config.enable_codex_tasks,
            "codex_tasks_enabled": self._config.enable_codex_tasks,
            "active_background_jobs": sum(
                1 for job in self._job_manager.list_jobs() if job["status"] == "running"
            ),
            "unread_user_messages": len(self._inbox_store.peek_unread_for_agent()),
            "live_inbox_enabled": True,
            "bridge_chat_channel": "active (consola interactiva 'bridge chat' conectada a live_inbox.json)",
            "ripgrep_available": find_rg_path() is not None,
            "search_engine": "ripgrep" if find_rg_path() else "python_native",
            "tools": self._visible_tool_names(),
        }

    def inbox_send_user_message(self, text: str, *, priority: str = "normal") -> JsonObject:
        """Queue a live message from the user without interrupting the agent work loop."""
        return self._inbox_store.send_user_message(text, priority=priority)

    def inbox_send_agent_reply(self, text: str) -> JsonObject:
        """Send a live reply or progress update from the agent to the local Bridge Messenger UI."""
        return self._inbox_store.send_agent_reply(text)

    def inbox_pop_unread(self, *, tool_name: str) -> list[JsonObject]:
        """Pop unread user messages and attach them to the current tool response."""
        return self._inbox_store.pop_unread_for_agent(tool_name=tool_name)

    def inbox_record_activity(self, tool_name: str, summary: str = "") -> None:
        """Record the agent's latest tool call for live UI telemetry."""
        self._inbox_store.record_agent_activity(tool_name, summary)

    def inbox_state(self, *, limit: int = 50) -> JsonObject:
        """Return live inbox messages and agent telemetry."""
        return self._inbox_store.get_state(limit=limit)

    def inbox_clear(self) -> JsonObject:
        """Clear all live inbox messages."""
        return self._inbox_store.clear()

    def _visible_tool_names(self) -> list[str]:
        if self._config.tool_profile == "delegate":
            return [
                "bridge_status",
                *(
                    [
                        "bridge_delegate",
                        "bridge_session_list",
                        "bridge_session_status",
                    ]
                    if self._config.enable_codex_tasks
                    else []
                ),
            ]
        return [
            "bridge_status",
            "set_bridge_mode",
            "grant_path",
            "revoke_grant",
            "list_grants",
            "search_files",
            "read_file",
            "view_image",
            "write_file",
            "list_dir",
            "edit_file",
            "file_ops",
            "exec_command",
            "process_manager",
            "system_info",
            *(["bridge_task_run"] if self._config.enable_codex_tasks else []),
            *(
                [
                    "bridge_delegate",
                    "bridge_session_start",
                    "bridge_session_continue",
                    "bridge_session_list",
                    "bridge_session_status",
                ]
                if self._config.enable_codex_tasks
                else []
            ),
        ]

    async def list_grants(self) -> JsonObject:
        """List configured grants."""

        self._maybe_reload_config()
        return {
            "trust_mode": self._config.trust_mode,
            "full_delegate": self._config.trust_mode == "full_delegate",
            "grants": [
                {
                    "name": grant.name,
                    "path": str(grant.resolved_path),
                    "read": grant.read,
                    "write": grant.write,
                    "execute": grant.execute,
                }
                for grant in self._config.grants
            ],
        }

    async def set_bridge_mode(self, *, mode: TrustMode) -> JsonObject:
        """Set and persist the bridge trust mode."""

        if mode not in {"restricted", "full_delegate"}:
            raise BridgeError("mode must be restricted or full_delegate")
        self._config = self._config.model_copy(update={"trust_mode": mode})
        self._save_runtime_config()
        return {
            "trust_mode": self._config.trust_mode,
            "full_delegate": self._config.trust_mode == "full_delegate",
        }

    async def grant_path(
        self,
        *,
        path: str,
        name: str | None = None,
        read: bool = True,
        write: bool = False,
        execute: bool = False,
    ) -> JsonObject:
        """Grant a local path at runtime and persist the config."""

        self._config = add_grant(
            self._config,
            path=Path(path),
            name=name,
            read=read,
            write=write,
            execute=execute,
        )
        self._save_runtime_config()
        grant = self._matching_grant(resolve_user_path(path))
        if grant is None:
            raise BridgeError(f"failed to persist grant: {path}")
        return {
            "grant": {
                "name": grant.name,
                "path": str(grant.resolved_path),
                "read": grant.read,
                "write": grant.write,
                "execute": grant.execute,
            }
        }

    async def revoke_grant(self, *, path: str) -> JsonObject:
        """Remove a local path grant at runtime and persist the config."""

        resolved = resolve_user_path(path)
        before = len(self._config.grants)
        grants = [grant for grant in self._config.grants if grant.resolved_path != resolved]
        self._config = self._config.model_copy(update={"grants": grants})
        self._save_runtime_config()
        return {"path": str(resolved), "removed": len(grants) < before}

    async def read_file(
        self,
        *,
        path: str,
        max_chars: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> JsonObject:
        """Read an approved UTF-8 file (or directory listing / power shortcut in full_delegate)."""

        self._maybe_reload_config()
        shortcut_result = await self._handle_read_shortcut(path)
        if shortcut_result is not None:
            return shortcut_result

        raw_path = path
        match = _LINE_RANGE_RE.match(raw_path)
        if match and start_line is None and end_line is None:
            raw_path = match.group("path")
            start_line = int(match.group("start"))
            end_line = int(match.group("end")) if match.group("end") else start_line + 200

        resolved = self._authorize(raw_path, read=True)
        limit = max_chars or self._config.max_file_chars
        if limit <= 0:
            raise BridgeError("max_chars must be >= 1")
        limit = max(limit, 1)
        if not resolved.exists():
            raise BridgeError(f"file does not exist: {resolved}")
        if resolved.is_dir():
            listing = await self.list_dir(path=str(resolved))
            formatted = json.dumps(listing, ensure_ascii=False, indent=2)
            return {
                "path": str(resolved),
                "is_directory": True,
                "text": formatted[:limit],
                "truncated": len(formatted) > limit,
                "chars": min(len(formatted), limit),
            }
        if not resolved.is_file():
            raise BridgeError(f"file does not exist: {resolved}")

        if (
            start_line is None
            and end_line is None
            and resolved.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
        ):
            return await self._read_image_payload(resolved)

        text = await asyncio.to_thread(resolved.read_text, encoding="utf-8", errors="replace")
        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            s_idx = max(0, (start_line or 1) - 1)
            e_idx = min(len(lines), end_line or len(lines))
            text = "".join(lines[s_idx:e_idx])

        truncated = len(text) > limit
        return {
            "path": str(resolved),
            "text": text[:limit],
            "truncated": truncated,
            "chars": min(len(text), limit),
        }

    async def _handle_read_shortcut(self, raw_path: str) -> JsonObject | None:
        """Support multiplexed prefixes (ps:, cmd:, py:, exec:, bg:, jobs:, inbox:) via read_file."""

        if raw_path.startswith("inbox:"):
            spec = raw_path[6:].strip()
            if not spec or spec == "check" or spec == "pop":
                popped = self.inbox_pop_unread(tool_name="read_file(inbox:)")
                state = self.inbox_state()
                res: JsonObject = {"popped_messages": popped, "inbox": state}
            elif spec in {"peek", "history", "list", "status"}:
                res = self.inbox_state()
            elif spec == "clear":
                res = self.inbox_clear()
            elif spec.startswith("reply:"):
                reply_text = spec[6:].strip()
                res = {"sent_reply": self.inbox_send_agent_reply(reply_text)}
            elif spec.startswith("send:"):
                user_text = spec[5:].strip()
                res = {"queued_user_message": self.inbox_send_user_message(user_text)}
            else:
                res = {"sent_reply": self.inbox_send_agent_reply(spec)}
            text_out = json.dumps(res, ensure_ascii=False, indent=2)
            return {
                "path": raw_path,
                "text": text_out,
                "truncated": False,
                "chars": len(text_out),
            }

        if self._config.trust_mode != "full_delegate":
            return None

        prefixes: tuple[tuple[str, str], ...] = (
            ("ps:", "powershell"),
            ("powershell:", "powershell"),
            ("cmd:", "cmd"),
            ("py:", "python"),
            ("python:", "python"),
            ("exec:", "auto"),
            ("sh:", "sh"),
        )
        for prefix, shell_kind in prefixes:
            if raw_path.startswith(prefix):
                cmd_text = raw_path[len(prefix) :].strip()
                res = await self.exec_command(command=cmd_text, shell=shell_kind)  # type: ignore[arg-type]
                text_out = json.dumps(res, ensure_ascii=False, indent=2)
                return {
                    "path": raw_path,
                    "text": text_out,
                    "truncated": False,
                    "chars": len(text_out),
                }

        if raw_path.startswith("bg:"):
            cmd_text = raw_path[3:].strip()
            res = await self.exec_command(command=cmd_text, background=True)
            text_out = json.dumps(res, ensure_ascii=False, indent=2)
            return {
                "path": raw_path,
                "text": text_out,
                "truncated": False,
                "chars": len(text_out),
            }

        if raw_path.startswith("jobs:"):
            spec = raw_path[5:].strip()
            if not spec or spec == "list":
                res = await self.process_manager(action="list_jobs")
            elif spec.startswith("wait:"):
                jid = spec.split(":", 1)[1].strip()
                res = {"job": await self._job_manager.wait_for_job(jid, timeout_s=20.0)}
            elif spec.startswith("status:"):
                jid = spec.split(":", 1)[1].strip()
                res = {"job": await self._job_manager.wait_for_job(jid, timeout_s=15.0)}
            elif spec.startswith("kill:"):
                res = await self.process_manager(
                    action="kill_job",
                    job_id=spec.split(":", 1)[1].strip(),
                )
            elif spec.startswith("ps"):
                parts = spec.split(":", 1)
                flt = parts[1].strip() if len(parts) > 1 else None
                res = await self.process_manager(action="system_processes", filter_name=flt)
            else:
                res = {"job": await self._job_manager.wait_for_job(spec, timeout_s=15.0)}
            text_out = json.dumps(res, ensure_ascii=False, indent=2)
            return {
                "path": raw_path,
                "text": text_out,
                "truncated": False,
                "chars": len(text_out),
            }

        if raw_path.startswith("sysinfo:"):
            res = await self.system_info()
            text_out = json.dumps(res, ensure_ascii=False, indent=2)
            return {
                "path": raw_path,
                "text": text_out,
                "truncated": False,
                "chars": len(text_out),
            }

        if raw_path.startswith(("img:", "image:")):
            target = raw_path.split(":", 1)[1].strip()
            return await self.view_image(path=target)

        if raw_path.startswith("screenshot:"):
            target = raw_path.split(":", 1)[1].strip() or "screenshot"
            return await self.view_image(path=f"screenshot:{target}" if target != "screenshot" else "screenshot")

        return None

    async def view_image(
        self,
        *,
        path: str,
        max_width: int = 1280,
    ) -> JsonObject:
        """Read a local image or capture a live desktop/game screenshot and return MCP ImageContent + visual metrics."""

        self._maybe_reload_config()
        spec = path.strip()
        if spec.lower() in {"screenshot", "screen", "desktop"} or spec.lower().startswith("screenshot:"):
            if self._config.trust_mode != "full_delegate":
                raise BridgeError("live screenshot capture requires full_delegate trust_mode")
            custom_out = spec.split(":", 1)[1].strip() if ":" in spec else ""
            out_path = (
                resolve_user_path(custom_out)
                if custom_out
                else Path(tempfile.gettempdir()) / "codex_bridge_screenshot.png"
            )
            await asyncio.to_thread(self._capture_screenshot_sync, out_path)
            resolved = out_path
        else:
            resolved = self._authorize(spec, read=True)
            if not resolved.exists() or not resolved.is_file():
                raise BridgeError(f"image file does not exist: {resolved}")

        return await self._read_image_payload(resolved, max_width=max_width)

    @staticmethod
    def _capture_screenshot_sync(out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import ImageGrab  # type: ignore[import-not-found]

            img = ImageGrab.grab(all_screens=False)
            img.save(out_path, format="PNG")
            return
        except Exception:
            pass
        if os.name == "nt":
            import subprocess

            ps_cmd = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height); "
                "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
                f"$bmp.Save('{str(out_path).replace(chr(39), chr(39)*2)}', [System.Drawing.Imaging.ImageFormat]::Png); "
                "$g.Dispose(); $bmp.Dispose()"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0 and out_path.exists():
                return
        raise BridgeError("failed to capture screenshot (Pillow ImageGrab and GDI fallback unavailable)")

    async def _read_image_payload(
        self,
        resolved: Path,
        *,
        max_width: int = 1280,
    ) -> JsonObject:
        def _load_sync() -> JsonObject:
            raw_bytes = resolved.read_bytes()
            mime_type = mimetypes.guess_type(str(resolved))[0] or "image/png"
            width: int | None = None
            height: int | None = None
            mode_str: str | None = None
            mean_rgb: list[float] | None = None
            out_bytes = raw_bytes
            try:
                from PIL import Image, ImageStat  # type: ignore[import-not-found]

                with Image.open(io.BytesIO(raw_bytes)) as im:
                    width, height = im.size
                    mode_str = im.mode
                    rgb_im = im.convert("RGB")
                    stat = ImageStat.Stat(rgb_im)
                    mean_rgb = [round(float(x), 2) for x in stat.mean[:3]]
                    if (max_width > 0 and width > max_width) or len(raw_bytes) > 90_000:
                        target_w = min(width, max_width) if max_width > 0 else width
                        new_h = max(1, int(round(height * (target_w / float(width)))))
                        resized = (
                            rgb_im.resize((target_w, new_h), Image.Resampling.LANCZOS)
                            if target_w != width
                            else rgb_im
                        )
                        buf = io.BytesIO()
                        resized.save(buf, format="JPEG", quality=82, optimize=True)
                        out_bytes = buf.getvalue()
                        mime_type = "image/jpeg"
                        width, height = resized.size
            except Exception:
                if os.name == "nt":
                    with contextlib.suppress(Exception):
                        import subprocess
                        tmp_jpg = Path(tempfile.gettempdir()) / "codex_bridge_opt.jpg"
                        src_esc = str(resolved).replace("'", "''")
                        dst_esc = str(tmp_jpg).replace("'", "''")
                        tw = min(max_width, 640) if max_width > 0 else 640
                        ps_opt = (
                            "Add-Type -AssemblyName System.Drawing; "
                            f"$img = [System.Drawing.Image]::FromFile('{src_esc}'); "
                            f"$w = $img.Width; $h = $img.Height; $tw = [Math]::Min($w, {tw}); "
                            "$th = [Math]::Max(1, [int][Math]::Round($h * ($tw / [double]$w))); "
                            "$bmp = New-Object System.Drawing.Bitmap($tw, $th); "
                            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                            "$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic; "
                            "$g.DrawImage($img, 0, 0, $tw, $th); "
                            "$enc = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }; "
                            "$ep = New-Object System.Drawing.Imaging.EncoderParameters(1); "
                            "$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, [long]65); "
                            f"$bmp.Save('{dst_esc}', $enc, $ep); "
                            "Write-Output \"$tw,$th\"; "
                            "$g.Dispose(); $bmp.Dispose(); $img.Dispose()"
                        )
                        r = subprocess.run(
                            ["powershell", "-NoProfile", "-Command", ps_opt],
                            capture_output=True,
                            text=True,
                            timeout=8,
                            check=False,
                        )
                        if r.returncode == 0 and tmp_jpg.exists():
                            out_bytes = tmp_jpg.read_bytes()
                            mime_type = "image/jpeg"
                            dims = r.stdout.strip().split(",")
                            if len(dims) == 2:
                                width, height = int(dims[0]), int(dims[1])

            b64_data = base64.b64encode(out_bytes).decode("ascii")
            summary = {
                "path": str(resolved),
                "is_image": True,
                "mime_type": mime_type,
                "bytes": len(raw_bytes),
                "encoded_bytes": len(out_bytes),
                "width": width,
                "height": height,
                "mode": mode_str,
                "mean_rgb": mean_rgb,
                "image_base64": b64_data,
            }
            text_summary = json.dumps(summary, ensure_ascii=False, indent=2)
            return {
                **summary,
                "text": text_summary,
                "truncated": False,
                "chars": len(text_summary),
                "_mcp_image": {
                    "data": b64_data,
                    "mimeType": mime_type,
                },
            }

        return await asyncio.to_thread(_load_sync)

    async def write_file(
        self,
        *,
        path: str,
        content: str,
        create_parents: bool = False,
    ) -> JsonObject:
        """Write UTF-8 text to an approved path (or run surgical edit via edit:<path>)."""

        self._maybe_reload_config()
        if path.startswith("edit:"):
            target_path = path[5:].strip()
            return await self._apply_inline_edit_block(target_path, content)

        if len(content) > self._config.max_write_chars:
            raise BridgeError(f"content exceeds max_write_chars={self._config.max_write_chars}")
        resolved = self._authorize(path, write=True)
        if create_parents:
            await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
        if not resolved.parent.exists():
            raise BridgeError(f"parent directory does not exist: {resolved.parent}")
        if resolved.name.lower() in {"agent_outbox.txt", "bridge_chat.txt"} and content.strip():
            with contextlib.suppress(OSError):
                resolved.unlink(missing_ok=True)
            msg = self.inbox_send_agent_reply(content.strip())
            return {"path": str(resolved), "bytes": len(content.encode("utf-8")), "bridge_chat_delivered": msg}
        existed_before = resolved.exists()
        old_line_count = 0
        if existed_before and resolved.is_file():
            with contextlib.suppress(Exception):
                old_line_count = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
        await asyncio.to_thread(resolved.write_text, content, encoding="utf-8")
        if resolved.name.lower() == "run_task.py" and content.strip():
            job = await self._job_manager.start_job(
                command=content,
                cwd=resolved.parent,
                shell="python",
                allow_stdin=False,
            )
            waited_res = await self._job_manager.wait_for_job(job.job_id, timeout_s=18.0)
            return {"path": str(resolved), "bytes": len(content.encode("utf-8")), "job": waited_res}
        with contextlib.suppress(Exception):
            new_lines = content.replace("\r\n", "\n").splitlines()
            byte_len = len(content.encode("utf-8"))
            if existed_before:
                header = f"Replaced file · +{len(new_lines)} / -{old_line_count} lines ({byte_len} B)"
            else:
                header = f"Created file · +{len(new_lines)} lines ({byte_len} B)"
            w_preview = [header]
            for idx, nl in enumerate(new_lines[:8], 1):
                w_preview.append(f"+ {idx:>4d} │ {nl[:104]}")
            if len(new_lines) > 8:
                w_preview.append(f"+ ... ({len(new_lines) - 8} more lines)")
            self._inbox_store.record_agent_activity("write_file", str(resolved), "\n".join(w_preview))
        return {"path": str(resolved), "bytes": len(content.encode("utf-8"))}

    async def _apply_inline_edit_block(self, path: str, content: str) -> JsonObject:
        if "<<<OLD\n" in content and "\n===\n" in content and "\n>>>" in content:
            body = content.split("<<<OLD\n", 1)[1]
            old_part, rest = body.split("\n===\n", 1)
            new_part = rest.rsplit("\n>>>", 1)[0]
            return await self.edit_file(path=path, old_text=old_part, new_text=new_part)
        raise BridgeError("inline edit:<path> requires <<<OLD\\n...\\n===\\n...\\n>>> format")

    async def edit_file(
        self,
        *,
        path: str,
        old_text: str | None = None,
        new_text: str = "",
        replace_all: bool = False,
        mode: EditMode = "replace",
        start_line: int | None = None,
        end_line: int | None = None,
        create_if_missing: bool = False,
    ) -> JsonObject:
        """Perform fast surgical edits on a local file without rewriting the whole file over MCP."""

        self._maybe_reload_config()
        resolved = self._authorize(path, read=True, write=True)
        if not resolved.exists():
            if create_if_missing:
                await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(resolved.write_text, new_text, encoding="utf-8")
                return {
                    "path": str(resolved),
                    "mode": "created",
                    "replacements": 1,
                    "bytes": len(new_text.encode("utf-8")),
                }
            raise BridgeError(f"file does not exist: {resolved}")

        raw_bytes = await asyncio.to_thread(resolved.read_bytes)
        uses_crlf = b"\r\n" in raw_bytes
        current_text = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")
        normalized_new = new_text.replace("\r\n", "\n")
        base_line = 1
        old_lines: list[str] = []

        if mode == "append":
            base_line = current_text.count("\n") + 1
            updated_text = current_text + normalized_new
            replacements = 1
        elif mode == "prepend":
            base_line = 1
            updated_text = normalized_new + current_text
            replacements = 1
        elif mode == "replace_lines":
            if start_line is None or start_line < 1:
                raise BridgeError("start_line (>= 1) is required for mode='replace_lines'")
            lines = current_text.splitlines(keepends=True)
            s_idx = start_line - 1
            e_idx = end_line if end_line is not None else start_line
            if s_idx > len(lines):
                raise BridgeError(f"start_line={start_line} exceeds file length ({len(lines)})")
            base_line = start_line
            old_lines = [ln.rstrip("\r\n") for ln in lines[s_idx:e_idx]]
            replacement_lines = (
                [normalized_new]
                if normalized_new.endswith("\n") or not normalized_new
                else [f"{normalized_new}\n"]
            )
            lines[s_idx:e_idx] = replacement_lines if normalized_new else []
            updated_text = "".join(lines)
            replacements = 1
        elif mode == "replace":
            if old_text is None or old_text == "":
                raise BridgeError("old_text must be provided for mode='replace'")
            normalized_old = old_text.replace("\r\n", "\n")
            count = current_text.count(normalized_old)
            if count == 0:
                raise BridgeError(f"old_text was not found in {resolved}")
            if count > 1 and not replace_all:
                raise BridgeError(
                    f"old_text matched {count} locations in {resolved}; "
                    "pass replace_all=True or include more surrounding context"
                )
            pos = current_text.find(normalized_old)
            base_line = (current_text[:pos].count("\n") + 1) if pos >= 0 else 1
            old_lines = normalized_old.splitlines()
            if replace_all:
                updated_text = current_text.replace(normalized_old, normalized_new)
                replacements = count
            else:
                updated_text = current_text.replace(normalized_old, normalized_new, 1)
                replacements = 1
        else:
            raise BridgeError(f"unsupported edit mode: {mode}")

        final_text = updated_text.replace("\n", "\r\n") if uses_crlf else updated_text
        final_bytes = final_text.encode("utf-8")
        await asyncio.to_thread(resolved.write_bytes, final_bytes)
        with contextlib.suppress(Exception):
            new_lines = normalized_new.splitlines()
            rep_label = f"{replacements} replacement" + ("s" if replacements != 1 else "")
            diff_preview = [
                f"@@ L{base_line} · +{len(new_lines)} / -{len(old_lines)} lines ({mode}, {rep_label}) @@"
            ]
            for idx, ol in enumerate(old_lines[:8]):
                diff_preview.append(f"- {base_line + idx:>4d} │ {ol[:104]}")
            if len(old_lines) > 8:
                diff_preview.append(f"- ... ({len(old_lines) - 8} more removed lines)")
            for idx, nl in enumerate(new_lines[:10]):
                diff_preview.append(f"+ {base_line + idx:>4d} │ {nl[:104]}")
            if len(new_lines) > 10:
                diff_preview.append(f"+ ... ({len(new_lines) - 10} more added lines)")
            self._inbox_store.record_agent_activity("edit_file", str(resolved), "\n".join(diff_preview))
        return {
            "path": str(resolved),
            "mode": mode,
            "replacements": replacements,
            "bytes": len(final_bytes),
        }

    async def list_dir(
        self,
        *,
        path: str = "~",
        recursive: bool = False,
        max_depth: int = 2,
        pattern: str | None = None,
        include_hidden: bool = False,
        max_entries: int = 200,
    ) -> JsonObject:
        """List files and subdirectories inside an approved directory."""

        self._maybe_reload_config()
        resolved = self._authorize(path, read=True)
        if not resolved.exists():
            raise BridgeError(f"directory does not exist: {resolved}")
        if not resolved.is_dir():
            raise BridgeError(f"path is not a directory: {resolved}")
        capped_entries = max(1, min(max_entries, 1000))
        entries, truncated = await asyncio.to_thread(
            self._scan_directory_sync,
            root=resolved,
            recursive=recursive,
            max_depth=max(1, min(max_depth, 10)),
            pattern=pattern,
            include_hidden=include_hidden,
            max_entries=capped_entries,
        )
        return {
            "path": str(resolved),
            "entries": entries,
            "count": len(entries),
            "truncated": truncated,
        }

    def _scan_directory_sync(
        self,
        *,
        root: Path,
        recursive: bool,
        max_depth: int,
        pattern: str | None,
        include_hidden: bool,
        max_entries: int,
    ) -> tuple[list[JsonObject], bool]:
        entries: list[JsonObject] = []
        truncated = False
        enforce_sensitive = self._config.trust_mode != "full_delegate"

        def _visit(current_dir: Path, depth: int) -> None:
            nonlocal truncated
            if len(entries) >= max_entries:
                truncated = True
                return
            try:
                with os.scandir(current_dir) as it:
                    raw_items = list(it)
            except OSError:
                return

            raw_items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            for entry in raw_items:
                if len(entries) >= max_entries:
                    truncated = True
                    return
                name = entry.name
                if not include_hidden and name.startswith("."):
                    continue
                entry_path = Path(entry.path)
                if enforce_sensitive and is_sensitive_path(entry_path):
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if pattern and not fnmatch.fnmatch(name.lower(), pattern.lower()):
                    if (
                        is_dir
                        and recursive
                        and depth < max_depth
                        and name not in _IGNORED_SEARCH_DIRS
                    ):
                        _visit(entry_path, depth + 1)
                    continue
                try:
                    stat_res = entry.stat(follow_symlinks=False)
                    size_bytes = 0 if is_dir else stat_res.st_size
                    modified_at = datetime.fromtimestamp(stat_res.st_mtime, tz=UTC).isoformat()
                except OSError:
                    size_bytes = 0
                    modified_at = None

                entries.append(
                    {
                        "name": name,
                        "path": str(entry_path),
                        "type": "dir" if is_dir else "file",
                        "size_bytes": size_bytes,
                        "modified_at": modified_at,
                    }
                )
                if is_dir and recursive and depth < max_depth and name not in _IGNORED_SEARCH_DIRS:
                    _visit(entry_path, depth + 1)

        _visit(root, 1)
        return entries, truncated

    async def file_ops(
        self,
        *,
        action: FileOpAction,
        path: str,
        dest_path: str | None = None,
        start_line: int = 1,
        end_line: int = 200,
        lines: int = 100,
        recursive: bool = False,
    ) -> JsonObject:
        """Perform filesystem operations: stat, mkdir, delete, move, copy, read_lines."""

        self._maybe_reload_config()
        if action in {"stat", "read_lines", "head", "tail"}:
            resolved = self._authorize(path, read=True)
        else:
            resolved = self._authorize(path, read=True, write=True)

        if action == "stat":
            if not resolved.exists():
                return {"path": str(resolved), "exists": False}
            st = await asyncio.to_thread(resolved.stat)
            return {
                "path": str(resolved),
                "exists": True,
                "type": "dir" if resolved.is_dir() else "file",
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_ctime, tz=UTC).isoformat(),
                "modified_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
            }

        if action == "mkdir":
            await asyncio.to_thread(resolved.mkdir, parents=True, exist_ok=True)
            return {"path": str(resolved), "created": True}

        if action == "delete":
            if not resolved.exists():
                return {"path": str(resolved), "deleted": False, "reason": "not_found"}
            if resolved.is_dir():
                if not recursive:
                    await asyncio.to_thread(resolved.rmdir)
                else:
                    await asyncio.to_thread(shutil.rmtree, resolved)
            else:
                await asyncio.to_thread(resolved.unlink)
            return {"path": str(resolved), "deleted": True}

        if action in {"move", "copy"}:
            if not dest_path:
                raise BridgeError(f"dest_path is required for action='{action}'")
            resolved_dest = self._authorize(dest_path, write=True)
            await asyncio.to_thread(resolved_dest.parent.mkdir, parents=True, exist_ok=True)
            if action == "move":
                await asyncio.to_thread(shutil.move, str(resolved), str(resolved_dest))
            elif resolved.is_dir():
                await asyncio.to_thread(
                    shutil.copytree,
                    resolved,
                    resolved_dest,
                    dirs_exist_ok=True,
                )
            else:
                await asyncio.to_thread(shutil.copy2, resolved, resolved_dest)
            return {"source": str(resolved), "dest": str(resolved_dest), "action": action}

        if action in {"read_lines", "head", "tail"}:
            if not resolved.exists() or not resolved.is_file():
                raise BridgeError(f"file does not exist: {resolved}")
            text = await asyncio.to_thread(resolved.read_text, encoding="utf-8", errors="replace")
            all_lines = text.splitlines()
            total_lines = len(all_lines)
            if action == "head":
                selected = all_lines[: max(1, lines)]
                s_num = 1
            elif action == "tail":
                count = max(1, lines)
                selected = all_lines[-count:]
                s_num = max(1, total_lines - len(selected) + 1)
            else:
                s_idx = max(0, start_line - 1)
                e_idx = min(total_lines, max(start_line, end_line))
                selected = all_lines[s_idx:e_idx]
                s_num = s_idx + 1
            numbered = "\n".join(f"{s_num + idx}: {line}" for idx, line in enumerate(selected))
            return {
                "path": str(resolved),
                "total_lines": total_lines,
                "start_line": s_num,
                "returned_lines": len(selected),
                "text": numbered,
            }

        raise BridgeError(f"unsupported file_ops action: {action}")

    async def exec_command(
        self,
        *,
        command: str,
        cwd: str | None = None,
        shell: ShellKind = "auto",
        timeout_s: float = 30.0,
        background: bool = False,
        auto_background_on_timeout: bool = True,
        env: dict[str, str] | None = None,
    ) -> JsonObject:
        """Execute a direct shell/PowerShell/CMD/Python command without stalling."""

        self._maybe_reload_config()
        if not command or not command.strip():
            raise BridgeError("command must not be empty")
        if timeout_s <= 0:
            raise BridgeError("timeout_s must be > 0")

        target_cwd = cwd or (
            str(self._config.grants[0].resolved_path)
            if self._config.grants and self._config.trust_mode != "full_delegate"
            else str(Path.home())
        )
        resolved_cwd = self._authorize(target_cwd, read=True, execute=True)
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            resolved_cwd = Path.home()

        res = await self._job_manager.run_command(
            command=command,
            cwd=resolved_cwd,
            shell=shell,
            timeout_s=timeout_s,
            background=background,
            auto_background_on_timeout=auto_background_on_timeout,
            env=env,
        )
        with contextlib.suppress(Exception):
            eff_shell = str(res.get("shell") or shell or "auto").lower()
            prefix_map = {"python": "py: ", "powershell": "ps: ", "cmd": "cmd: ", "sh": "sh: "}
            cmd_lines = [ln.strip() for ln in command.strip().splitlines() if ln.strip()]
            first_cmd = cmd_lines[0] if cmd_lines else command.strip()
            summary_str = f"{prefix_map.get(eff_shell, '')}{first_cmd}"[:200]
            status = str(res.get("status") or "completed")
            rc = res.get("returncode")
            elapsed = float(res.get("elapsed_s") or 0.0)
            cwd_name = Path(str(res.get("cwd") or resolved_cwd)).name or str(resolved_cwd)
            if status == "running_in_background" or res.get("background"):
                head = f"Running in background (job {res.get('job_id')}, pid {res.get('pid')}) · {elapsed:.2f}s · cwd: {cwd_name}"
            elif rc == 0 or rc is None:
                head = f"Exit 0 (ok) · {elapsed:.2f}s · cwd: {cwd_name}"
            else:
                head = f"Exit {rc} (error) · {elapsed:.2f}s · cwd: {cwd_name}"
            exec_details = [head]
            for cl in cmd_lines[:3]:
                exec_details.append(f"$ {cl[:108]}")
            if len(cmd_lines) > 3:
                exec_details.append(f"$ ... (+{len(cmd_lines) - 3} more script lines)")
            stdout_lines = [ln.rstrip() for ln in str(res.get("stdout") or "").splitlines() if ln.strip()]
            stderr_lines = [ln.rstrip() for ln in str(res.get("stderr") or "").splitlines() if ln.strip()]
            for sl in stdout_lines[:8]:
                exec_details.append(f"> {sl[:108]}")
            if len(stdout_lines) > 8:
                exec_details.append(f"> ... (+{len(stdout_lines) - 8} more stdout lines)")
            for el in stderr_lines[:5]:
                exec_details.append(f"! {el[:108]}")
            if len(stderr_lines) > 5:
                exec_details.append(f"! ... (+{len(stderr_lines) - 5} more stderr lines)")
            self._inbox_store.record_agent_activity("exec_command", summary_str, "\n".join(exec_details))
        return res

    async def process_manager(
        self,
        *,
        action: ProcessAction = "list_jobs",
        job_id: str | None = None,
        pid: int | None = None,
        input_text: str | None = None,
        filter_name: str | None = None,
        tail_chars: int = 16_000,
    ) -> JsonObject:
        """Inspect or control background jobs and OS processes."""

        self._maybe_reload_config()
        if self._config.trust_mode != "full_delegate" and not any(
            g.execute for g in self._config.grants
        ):
            raise BridgeError("execute permission or full_delegate mode is required")

        if action == "list_jobs":
            return {"jobs": self._job_manager.list_jobs()}

        if action == "job_status":
            target_id = job_id or "latest"
            try:
                job = self._job_manager.get_job(target_id)
            except KeyError as exc:
                raise BridgeError(str(exc)) from exc
            return {"job": job.to_dict(include_output=True, tail_chars=tail_chars)}

        if action == "kill_job":
            if not job_id:
                raise BridgeError("job_id is required for action='kill_job'")
            try:
                killed = await self._job_manager.kill_job(job_id)
            except KeyError as exc:
                raise BridgeError(str(exc)) from exc
            return {"job": killed}

        if action == "send_input":
            if not job_id or input_text is None:
                raise BridgeError("job_id and input_text are required for action='send_input'")
            try:
                updated = await self._job_manager.send_input(job_id, input_text)
            except (KeyError, RuntimeError) as exc:
                raise BridgeError(str(exc)) from exc
            return {"job": updated}

        if action == "clear_finished":
            removed = self._job_manager.clear_finished()
            return {"cleared_jobs": removed}

        if action == "kill_pid":
            if pid is None or pid <= 0:
                raise BridgeError("pid (> 0) is required for action='kill_pid'")
            await kill_process_tree(pid)
            return {"pid": pid, "killed": True}

        if action == "system_processes":
            processes = await self._list_system_processes(filter_name=filter_name)
            return {"processes": processes, "count": len(processes)}

        if action == "restart_bridge":
            return await self._schedule_self_restart()

        raise BridgeError(f"unsupported process_manager action: {action}")

    async def _schedule_self_restart(self) -> JsonObject:
        """Spawn a detached watchdog to restart the bridge process after returning HTTP 200."""

        import subprocess

        current_pid = os.getpid()
        bridge_ps1 = Path.home() / "bin" / "bridge.ps1"
        repo_restart_ps1 = Path.home() / "codex-chatgpt-bridge-clean" / "restart-bridge.ps1"
        if bridge_ps1.exists():
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(bridge_ps1),
                "self-restart",
                "-DelayMs",
                "500",
            ]
        else:
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"Start-Sleep -Milliseconds 500; & '{repo_restart_ps1}'",
            ]
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags if os.name == "nt" else 0,
            close_fds=True,
            start_new_session=(os.name != "nt"),
        )
        return {
            "status": "restarting",
            "old_pid": current_pid,
            "delay_ms": 500,
            "message": (
                "Detached self-restart scheduled. The bridge will replace PID "
                f"{current_pid} and be back online on port 8766 in ~1.5 seconds."
            ),
        }

    async def _list_system_processes(self, *, filter_name: str | None = None) -> list[JsonObject]:
        if os.name == "nt":
            _, stdout, _ = await run_argv_captured(
                ["tasklist", "/FO", "CSV", "/NH"],
                cwd=Path.home(),
                timeout_s=10.0,
            )
            items: list[JsonObject] = []
            reader = csv.reader(io.StringIO(stdout))
            for row in reader:
                if len(row) < 5:
                    continue
                name, pid_str, session_name, session_num, mem_usage = row[:5]
                if filter_name and filter_name.lower() not in name.lower():
                    continue
                with contextlib.suppress(ValueError):
                    items.append(
                        {
                            "name": name,
                            "pid": int(pid_str),
                            "session": session_name,
                            "session_num": session_num,
                            "memory": mem_usage.strip(),
                        }
                    )
            return items[:200]

        _, stdout, _ = await run_argv_captured(
            ["ps", "-eo", "pid,comm,args"],
            cwd=Path.home(),
            timeout_s=10.0,
        )
        items = []
        for line in stdout.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            pid_str, comm = parts[0], parts[1]
            args = parts[2] if len(parts) > 2 else comm
            if filter_name and filter_name.lower() not in line.lower():
                continue
            with contextlib.suppress(ValueError):
                items.append({"pid": int(pid_str), "name": comm, "command": args[:300]})
        return items[:200]

    async def system_info(self) -> JsonObject:
        """Return hardware, OS, disk usage, and environment diagnostics."""

        self._maybe_reload_config()
        drives: list[JsonObject] = []
        if os.name == "nt":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                drive_root = Path(f"{letter}:\\")
                if drive_root.exists():
                    with contextlib.suppress(OSError):
                        usage = shutil.disk_usage(drive_root)
                        drives.append(
                            {
                                "drive": f"{letter}:\\",
                                "total_gb": round(usage.total / (1024**3), 2),
                                "used_gb": round(usage.used / (1024**3), 2),
                                "free_gb": round(usage.free / (1024**3), 2),
                            }
                        )
        else:
            with contextlib.suppress(OSError):
                usage = shutil.disk_usage("/")
                drives.append(
                    {
                        "drive": "/",
                        "total_gb": round(usage.total / (1024**3), 2),
                        "used_gb": round(usage.used / (1024**3), 2),
                        "free_gb": round(usage.free / (1024**3), 2),
                    }
                )

        return {
            "os": os.name,
            "platform": platform.platform(),
            "release": platform.release(),
            "machine": platform.machine(),
            "hostname": platform.node(),
            "cpu_count": os.cpu_count(),
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "user_home": str(Path.home()),
            "drives": drives,
            "binaries": {
                "codex": shutil.which("codex"),
                "rg": find_rg_path(),
                "git": shutil.which("git"),
                "node": shutil.which("node"),
                "npm": shutil.which("npm"),
                "python": shutil.which("python"),
                "powershell": shutil.which("pwsh") or shutil.which("powershell"),
                "cloudflared": shutil.which("cloudflared"),
            },
            "bridge": await self.bridge_status(),
        }

    async def search_files(
        self,
        *,
        query: str,
        root: str | None = None,
        max_results: int = 50,
    ) -> JsonObject:
        """Search approved readable directories with ripgrep or native Python fallback."""

        self._maybe_reload_config()
        if not query.strip():
            raise BridgeError("query must not be empty")
        if max_results <= 0:
            raise BridgeError("max_results must be >= 1")

        roots = [self._authorize(root, read=True)] if root else self._readable_roots()
        has_rg = find_rg_path() is not None
        matches: list[JsonObject] = []
        for search_root in roots:
            if not search_root.exists():
                continue
            remaining = max_results - len(matches)
            if remaining <= 0:
                break
            if has_rg:
                matches.extend(
                    await self._run_rg(
                        query=query,
                        root=search_root,
                        remaining=remaining,
                    )
                )
            else:
                matches.extend(
                    await asyncio.to_thread(
                        self._python_search_files,
                        query=query,
                        root=search_root,
                        remaining=remaining,
                    )
                )
            if len(matches) >= max_results:
                break
        return {
            "query": query,
            "engine": "ripgrep" if has_rg else "python_native",
            "matches": matches,
            "truncated": len(matches) >= max_results,
        }

    async def codex_session_start(
        self,
        *,
        prompt: str,
        cwd: str,
        sandbox_mode: CodexSessionSandboxMode = "danger_full_access",
        wait_timeout_s: float = 600.0,
    ) -> JsonObject:
        """Start a persisted local Codex session and return its bridge handle."""

        self._maybe_reload_config()
        if not self._config.enable_codex_tasks:
            raise BridgeError(
                "codex session tools are disabled; enable codex tasks in the bridge config"
            )
        if not prompt.strip():
            raise BridgeError("prompt must not be empty")
        resolved_cwd = self._authorize_codex_cwd(cwd, sandbox_mode)
        output_path = self._new_output_path()
        command = [
            self._codex_path(),
            "exec",
            "--cd",
            str(resolved_cwd),
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(output_path),
            *self._codex_permission_args(sandbox_mode),
            prompt,
        ]
        return await self._finish_codex_session_run(
            command=command,
            output_path=output_path,
            wait_timeout_s=wait_timeout_s,
            prompt=prompt,
            cwd=resolved_cwd,
            existing_record=None,
            sandbox_mode=sandbox_mode,
        )

    async def codex_delegate(
        self,
        *,
        task: str | None = None,
        prompt: str | None = None,
        permission_level: DelegatePermissionLevel = "full_local",
        target_paths: list[str] | None = None,
        expected_result: str | None = None,
        cwd: str | None = None,
        bridge_session_id: str | None = None,
        sandbox_mode: CodexSessionSandboxMode | None = None,
        wait_timeout_s: float = 600.0,
    ) -> JsonObject:
        """Delegate one whole local task to Codex through a single MCP call."""

        self._maybe_reload_config()
        task_text = self._delegate_task_text(task=task, prompt=prompt)
        if permission_level not in DELEGATE_PERMISSION_LEVELS:
            raise BridgeError("permission_level must be a supported delegate permission level")
        resolved_sandbox_mode = self._resolve_delegate_sandbox_mode(
            permission_level=permission_level,
            sandbox_mode=sandbox_mode,
        )
        normalized_paths = self._normalize_delegate_target_paths(target_paths)
        effective_prompt = self._build_delegate_prompt(
            task=task_text,
            permission_level=permission_level,
            target_paths=normalized_paths,
            expected_result=expected_result,
        )
        if bridge_session_id:
            result = await self.codex_session_continue(
                prompt=effective_prompt,
                bridge_session_id=bridge_session_id,
                sandbox_mode=resolved_sandbox_mode,
                wait_timeout_s=wait_timeout_s,
            )
            result["delegation_mode"] = "continue"
        else:
            result = await self.codex_session_start(
                prompt=effective_prompt,
                cwd=cwd or str(Path.home()),
                sandbox_mode=resolved_sandbox_mode,
                wait_timeout_s=wait_timeout_s,
            )
            result["delegation_mode"] = "start"
            final_msg_lower = (result.get("final_message") or "").lower().strip()
            prompt_markers = (
                "ready to proceed",
                "should i proceed",
                "would you like me to",
                "shall i proceed",
                "please confirm",
                "session initialized",
                "session configured",
                "ready to start",
                "do you want me to",
                "how would you like to proceed",
            )
            if any(marker in final_msg_lower for marker in prompt_markers) and result.get("bridge_session_id"):
                continue_res = await self.codex_session_continue(
                    prompt="Execute and complete all steps of the task now. Do not stop to ask questions.",
                    bridge_session_id=result["bridge_session_id"],
                    sandbox_mode=resolved_sandbox_mode,
                    wait_timeout_s=wait_timeout_s,
                )
                result["final_message"] = continue_res.get("final_message", result.get("final_message", ""))
                result["stdout_tail"] = continue_res.get("stdout_tail", result.get("stdout_tail", ""))
                result["stderr_tail"] = continue_res.get("stderr_tail", result.get("stderr_tail", ""))
                result["returncode"] = continue_res.get("returncode", result.get("returncode"))
        result["permission_level"] = permission_level
        result["target_paths"] = normalized_paths
        return result

    def _delegate_task_text(self, *, task: str | None, prompt: str | None) -> str:
        raw_task = task if task is not None else prompt
        if raw_task is None or not raw_task.strip():
            raise BridgeError("task must not be empty")
        return raw_task.strip()

    def _normalize_delegate_target_paths(self, target_paths: list[str] | None) -> list[str]:
        if target_paths is None:
            return []
        normalized: list[str] = []
        for raw_path in target_paths:
            if not raw_path.strip():
                raise BridgeError("target_paths must not contain empty paths")
            normalized.append(str(resolve_user_path(raw_path)))
        return normalized

    def _resolve_delegate_sandbox_mode(
        self,
        *,
        permission_level: DelegatePermissionLevel,
        sandbox_mode: CodexSessionSandboxMode | None,
    ) -> CodexSessionSandboxMode:
        default_sandbox = DELEGATE_DEFAULT_SANDBOX[permission_level]
        if sandbox_mode is None:
            return default_sandbox
        if sandbox_mode not in SANDBOX_RANK:
            raise BridgeError(
                "sandbox_mode must be full_access, read_only, workspace_write, or danger_full_access"
            )
        if SANDBOX_RANK[sandbox_mode] > SANDBOX_RANK[default_sandbox]:
            raise BridgeError(
                f"sandbox_mode={sandbox_mode} exceeds permission_level={permission_level}"
            )
        return sandbox_mode

    def _build_delegate_prompt(
        self,
        *,
        task: str,
        permission_level: DelegatePermissionLevel,
        target_paths: list[str],
        expected_result: str | None,
    ) -> str:
        path_block = (
            "\n".join(f"- {path}" for path in target_paths)
            if target_paths
            else "Not specified by caller."
        )
        result_block = (
            expected_result.strip()
            if expected_result and expected_result.strip()
            else ("Complete the delegated task and report the outcome.")
        )
        return "\n\n".join(
            [
                "You are the local execution engine performing a task delegated from the remote AI agent.",
                "Act as the local device agent for this machine.",
                (
                    "AUTONOMOUS EXECUTION MANDATE: You MUST execute and completely finish this delegated task "
                    "immediately in this single run. Do NOT wait for confirmation. Do NOT pause to ask questions. "
                    "Do NOT stop at planning. Execute all necessary shell/powershell commands, code modifications, "
                    "file writes, and verification checks right now to reach the expected result."
                ),
                f"Permission level: {permission_level}",
                f"Target paths:\n{path_block}",
                f"Expected result:\n{result_block}",
                f"Task:\n{task}",
                (
                    "When finished, summarize the actual local actions taken, changed files "
                    "or apps touched, commands or checks run, and any exact blocker if the task "
                    "could not be completed."
                ),
            ]
        )

    async def codex_session_continue(
        self,
        *,
        prompt: str,
        bridge_session_id: str = "latest",
        sandbox_mode: CodexSessionSandboxMode = "danger_full_access",
        wait_timeout_s: float = 600.0,
    ) -> JsonObject:
        """Continue a persisted local Codex session."""

        self._maybe_reload_config()
        if not self._config.enable_codex_tasks:
            raise BridgeError(
                "codex session tools are disabled; enable codex tasks in the bridge config"
            )
        if not prompt.strip():
            raise BridgeError("prompt must not be empty")
        try:
            record = self._session_store.resolve(bridge_session_id)
        except KeyError as exc:
            raise BridgeError(str(exc)) from exc
        if not record.codex_session_id:
            raise BridgeError(f"record has no Codex session id: {record.bridge_session_id}")
        resolved_cwd = self._authorize_codex_cwd(record.cwd, sandbox_mode)
        output_path = self._new_output_path()
        command = [
            self._codex_path(),
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(output_path),
            *self._codex_resume_permission_args(sandbox_mode),
            record.codex_session_id,
            prompt,
        ]
        return await self._finish_codex_session_run(
            command=command,
            output_path=output_path,
            wait_timeout_s=wait_timeout_s,
            prompt=prompt,
            cwd=resolved_cwd,
            existing_record=record,
            sandbox_mode=sandbox_mode,
        )

    async def codex_session_list(self) -> JsonObject:
        """List Codex sessions known to the bridge."""

        return {
            "sessions": [record.model_dump(mode="json") for record in self._session_store.list()]
        }

    async def codex_session_status(self, *, bridge_session_id: str = "latest") -> JsonObject:
        """Return one Codex session record."""

        try:
            record = self._session_store.resolve(bridge_session_id)
        except KeyError as exc:
            raise BridgeError(str(exc)) from exc
        return {"session": record.model_dump(mode="json")}

    async def codex_task_run(
        self,
        *,
        prompt: str,
        cwd: str,
        sandbox_mode: SandboxMode = "full_access",
        wait_timeout_s: float = 120.0,
    ) -> JsonObject:
        """Run a bounded local task in an approved executable directory."""

        self._maybe_reload_config()
        if not self._config.enable_codex_tasks:
            raise BridgeError(
                "task tools are disabled; enable tasks in the bridge config"
            )
        if not prompt.strip():
            raise BridgeError("prompt must not be empty")
        if sandbox_mode not in {"full_access", "danger_full_access", "read_only", "workspace_write"}:
            raise BridgeError("sandbox_mode must be full_access, danger_full_access, read_only, or workspace_write")
        resolved_cwd = self._authorize(cwd, read=True, execute=True)
        if sandbox_mode in {"full_access", "danger_full_access", "workspace_write"}:
            self._authorize(str(resolved_cwd), write=True)
        codex_path = shutil.which("codex")
        if codex_path is None:
            raise BridgeError("codex CLI was not found on PATH")
        with tempfile.NamedTemporaryFile(
            prefix="bridge-",
            suffix=".txt",
            delete=False,
        ) as handle:
            output_path = Path(handle.name)
        if sandbox_mode in {"full_access", "danger_full_access"}:
            sandbox_args = ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            sandbox = "workspace-write" if sandbox_mode == "workspace_write" else "read-only"
            sandbox_args = ["--sandbox", sandbox]
        command = [
            codex_path,
            "exec",
            "--cd",
            str(resolved_cwd),
            *sandbox_args,
            "--output-last-message",
            str(output_path),
            prompt,
        ]
        try:
            returncode, stdout, stderr = await run_argv_captured(
                command,
                cwd=resolved_cwd,
                timeout_s=wait_timeout_s,
            )
        except TimeoutError as exc:
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
            raise BridgeError(f"task timed out after {wait_timeout_s} seconds") from exc

        final_message = ""
        if output_path.exists():
            final_message = await asyncio.to_thread(
                output_path.read_text,
                encoding="utf-8",
                errors="replace",
            )
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
        return {
            "cwd": str(resolved_cwd),
            "sandbox_mode": sandbox_mode,
            "returncode": returncode,
            "final_message": final_message,
            "stdout_tail": stdout[-8000:],
            "stderr_tail": stderr[-8000:],
        }

    def _authorize_codex_cwd(self, cwd: str, sandbox_mode: CodexSessionSandboxMode) -> Path:
        if sandbox_mode not in {"full_access", "read_only", "workspace_write", "danger_full_access"}:
            raise BridgeError(
                "sandbox_mode must be full_access, read_only, workspace_write, or danger_full_access"
            )
        resolved_cwd = self._authorize(cwd, read=True, execute=True)
        if sandbox_mode in {"full_access", "workspace_write", "danger_full_access"}:
            self._authorize(str(resolved_cwd), write=True)
        return resolved_cwd

    def _codex_path(self) -> str:
        codex_path = shutil.which("codex")
        if codex_path is None:
            raise BridgeError("codex CLI was not found on PATH")
        return codex_path

    def _codex_permission_args(self, sandbox_mode: CodexSessionSandboxMode) -> list[str]:
        if sandbox_mode in {"danger_full_access", "full_access"}:
            return ["--dangerously-bypass-approvals-and-sandbox"]
        sandbox = "workspace-write" if sandbox_mode == "workspace_write" else "read-only"
        return ["--sandbox", sandbox]

    def _codex_resume_permission_args(self, sandbox_mode: CodexSessionSandboxMode) -> list[str]:
        if sandbox_mode in {"danger_full_access", "full_access"}:
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return []

    def _new_output_path(self) -> Path:
        with tempfile.NamedTemporaryFile(
            prefix="bridge-",
            suffix=".txt",
            delete=False,
        ) as handle:
            return Path(handle.name)

    async def _finish_codex_session_run(
        self,
        *,
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        prompt: str,
        cwd: Path,
        existing_record: CodexSessionRecord | None,
        sandbox_mode: CodexSessionSandboxMode,
    ) -> JsonObject:
        returncode, stdout, stderr = await self._run_codex_command(
            command,
            output_path,
            wait_timeout_s,
            cwd,
        )
        final_message = ""
        if output_path.exists():
            final_message = await asyncio.to_thread(
                output_path.read_text,
                encoding="utf-8",
                errors="replace",
            )
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
        now = datetime.now(UTC).isoformat()
        codex_session_id = extract_codex_session_id(stdout)
        if codex_session_id is None and existing_record is not None:
            codex_session_id = existing_record.codex_session_id
        bridge_session_id = (
            existing_record.bridge_session_id if existing_record is not None else str(uuid4())
        )
        record = CodexSessionRecord(
            bridge_session_id=bridge_session_id,
            codex_session_id=codex_session_id,
            cwd=str(cwd),
            created_at=existing_record.created_at if existing_record is not None else now,
            updated_at=now,
            last_prompt=prompt,
            last_final_message=final_message,
            last_returncode=returncode,
        )
        self._session_store.upsert(record)
        return {
            "bridge_session_id": record.bridge_session_id,
            "session_id": record.codex_session_id,
            "codex_session_id": record.codex_session_id,
            "cwd": record.cwd,
            "sandbox_mode": sandbox_mode,
            "returncode": returncode,
            "final_message": final_message,
            "stdout_tail": stdout[-8000:],
            "stderr_tail": stderr[-8000:],
        }

    async def _run_codex_command(
        self,
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        cwd: Path,
    ) -> tuple[int, str, str]:
        try:
            return await run_argv_captured(
                command,
                cwd=cwd,
                timeout_s=wait_timeout_s,
            )
        except TimeoutError as exc:
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
            raise BridgeError(f"codex session timed out after {wait_timeout_s} seconds") from exc

    def _readable_roots(self) -> list[Path]:
        roots = [grant.resolved_path for grant in self._config.grants if grant.read]
        if not roots and self._config.trust_mode == "full_delegate":
            return [Path.home()]
        return roots

    def _authorize(
        self,
        raw_path: str,
        *,
        read: bool = False,
        write: bool = False,
        execute: bool = False,
    ) -> Path:
        resolved = resolve_user_path(raw_path)
        if self._config.trust_mode == "full_delegate":
            return resolved
        if is_sensitive_path(resolved):
            raise BridgeError(f"refusing sensitive-looking path: {resolved}")
        grant = self._matching_grant(resolved)
        if grant is None:
            raise BridgeError(f"path is not inside an approved grant: {resolved}")
        if read and not grant.read:
            raise BridgeError(f"read is not allowed for grant: {grant.resolved_path}")
        if write and not grant.write:
            raise BridgeError(f"write is not allowed for grant: {grant.resolved_path}")
        if execute and not grant.execute:
            raise BridgeError(f"execute is not allowed for grant: {grant.resolved_path}")
        return resolved

    def _matching_grant(self, path: Path) -> BridgeGrant | None:
        matches = [
            grant for grant in self._config.grants if is_relative_to(path, grant.resolved_path)
        ]
        if not matches:
            return None
        return max(matches, key=lambda grant: len(grant.resolved_path.parts))

    def _save_runtime_config(self) -> None:
        if not self._has_explicit_config_path:
            return
        save_config(self._config, self._config_path)
        self._config_mtime_ns = self._get_config_mtime_ns()

    async def _run_rg(self, *, query: str, root: Path, remaining: int) -> list[JsonObject]:
        rg_bin = find_rg_path() or "rg"
        command = [
            rg_bin,
            "--json",
            "--line-number",
            "--column",
            "--color",
            "never",
            "--glob",
            "!.git/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!node_modules/**",
            query,
            str(root),
        ]
        try:
            returncode, stdout, stderr = await run_argv_captured(
                command,
                cwd=root if root.is_dir() else root.parent,
                timeout_s=15.0,
            )
        except TimeoutError as exc:
            raise BridgeError("ripgrep search timed out after 15 seconds") from exc

        if returncode not in {0, 1}:
            raise BridgeError(stderr[-2000:])

        enforce_sensitive = self._config.trust_mode != "full_delegate"
        matches: list[JsonObject] = []
        for line in stdout.splitlines():
            if len(matches) >= remaining:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_text = (data.get("path") or {}).get("text")
            if not path_text:
                continue
            candidate = Path(path_text).resolve(strict=False)
            if enforce_sensitive and is_sensitive_path(candidate):
                continue
            line_no = int(data.get("line_number") or 1)
            submatches = data.get("submatches") or []
            col = int(submatches[0].get("start", 0)) + 1 if submatches else 1
            lines_obj = data.get("lines") or {}
            text = str(lines_obj.get("text") or "").rstrip("\r\n")
            matches.append(
                {
                    "path": str(candidate),
                    "line": line_no,
                    "column": col,
                    "text": text[:500],
                }
            )
        return matches

    def _python_search_files(self, *, query: str, root: Path, remaining: int) -> list[JsonObject]:
        """Fast pure-Python regex/text/filename search when ripgrep is not installed."""

        enforce_sensitive = self._config.trust_mode != "full_delegate"
        matches: list[JsonObject] = []
        is_file_glob = query.startswith(("files:", "glob:"))
        glob_pat = query.split(":", 1)[1].strip().lower() if is_file_glob else ""

        pattern: re.Pattern[str] | None = None
        if not is_file_glob:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error:
                pattern = re.compile(re.escape(query), re.IGNORECASE)

        if root.is_file():
            files_to_check = [root]
        else:
            files_to_check = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in _IGNORED_SEARCH_DIRS and not d.startswith(".git")
                ]
                for fname in filenames:
                    files_to_check.append(Path(dirpath) / fname)
                    if len(files_to_check) >= 15_000:
                        break
                if len(files_to_check) >= 15_000:
                    break

        for candidate in files_to_check:
            if len(matches) >= remaining:
                break
            resolved_cand = candidate.resolve(strict=False)
            if enforce_sensitive and is_sensitive_path(resolved_cand):
                continue
            if is_file_glob:
                if not glob_pat or fnmatch.fnmatch(candidate.name.lower(), f"*{glob_pat}*"):
                    matches.append(
                        {
                            "path": str(resolved_cand),
                            "line": 1,
                            "column": 1,
                            "text": f"[file] {candidate.name}",
                        }
                    )
                continue

            if candidate.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            try:
                if candidate.stat().st_size > 4_000_000:
                    continue
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            assert pattern is not None
            for idx, line_text in enumerate(content.splitlines(), start=1):
                m = pattern.search(line_text)
                if m:
                    matches.append(
                        {
                            "path": str(resolved_cand),
                            "line": idx,
                            "column": m.start() + 1,
                            "text": line_text[:500],
                        }
                    )
                    if len(matches) >= remaining:
                        break

        return matches

    # Clean Universal Bridge Aliases
    bridge_delegate = codex_delegate
    bridge_session_start = codex_session_start
    bridge_session_continue = codex_session_continue
    bridge_session_list = codex_session_list
    bridge_session_status = codex_session_status
    bridge_task_run = codex_task_run
