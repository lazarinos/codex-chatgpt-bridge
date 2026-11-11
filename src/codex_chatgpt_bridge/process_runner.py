"""Non-blocking, hang-proof process execution and background job manager."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

JsonObject = dict[str, Any]
ShellKind = Literal["powershell", "cmd", "python", "sh", "auto"]

_WINDOWS_CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)
_WINDOWS_NORMAL_PRIORITY = 0x00000020
_WINDOWS_BELOW_NORMAL_PRIORITY = 0x00004000
_HEAVY_BUILD_CMD_RE = re.compile(
    r"\b(?:cmake|msbuild|ninja|cargo|cl\.exe|link\.exe|gradle|mvn|pytest|ctest)\b",
    re.IGNORECASE,
)


def _select_windows_creation_flags(command: str = "", *, background: bool = False) -> int:
    """Ensure child processes never inherit ABOVE_NORMAL priority and heavy builds run BELOW_NORMAL."""
    if os.name != "nt":
        return 0
    if background or _HEAVY_BUILD_CMD_RE.search(command or ""):
        return _WINDOWS_CREATE_FLAGS | _WINDOWS_BELOW_NORMAL_PRIORITY
    return _WINDOWS_CREATE_FLAGS | _WINDOWS_NORMAL_PRIORITY


def default_non_interactive_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dictionary configured to prevent interactive CLI hangs."""

    env = os.environ.copy()
    env.update(
        {
            "CI": "1",
            "TERM": "dumb",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "PIP_NO_INPUT": "1",
            "NPM_CONFIG_YES": "true",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "MSBUILDDISABLENODEREUSE": "1",
            "DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER": "1",
        }
    )
    if extra_env:
        for key, value in extra_env.items():
            env[str(key)] = str(value)
    return env


def build_shell_command(command: str, shell: ShellKind = "auto") -> tuple[list[str], str]:
    """Build an executable argv list for the requested shell and OS."""

    effective_shell: str = shell
    if effective_shell == "auto":
        effective_shell = "powershell" if os.name == "nt" else "sh"

    if effective_shell == "python":
        return [sys.executable, "-u", "-c", command], "python"

    if effective_shell == "cmd":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", command], "cmd"

    if effective_shell == "powershell":
        ps_bin = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        utf8_prelude = (
            "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$ProgressPreference = 'SilentlyContinue';"
        )
        full_script = f"{utf8_prelude}\n{command}"
        encoded = base64.b64encode(full_script.encode("utf-16-le")).decode("ascii")
        if len(encoded) <= 24_000:
            return (
                [
                    ps_bin,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                "powershell",
            )
        return (
            [
                ps_bin,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"{utf8_prelude} {command}",
            ],
            "powershell",
        )

    sh_bin = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return [sh_bin, "-c", command], "sh"


async def kill_process_tree(process: asyncio.subprocess.Process | int) -> None:
    """Force-kill a process and all of its descendants without hanging."""

    pid = process if isinstance(process, int) else process.pid
    if pid is None:
        return

    if os.name == "nt":
        await asyncio.to_thread(_taskkill_windows, pid)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, 9)

    if not isinstance(process, int):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            process.kill()


def _taskkill_windows(pid: int) -> None:
    with contextlib.suppress(Exception):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )


def _is_os_pid_alive(pid: int | None) -> bool:
    """Return False if the OS process is confirmed dead; True if running or unknown."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_info = 0x1000
        still_active = 259
        error_access_denied = 5
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(process_query_limited_info, False, int(pid))
            if not handle:
                return ctypes.GetLastError() == error_access_denied
            try:
                exit_code = ctypes.c_uint32()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                if not ok:
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


@dataclass
class StreamBuffer:
    """Bounded character buffer for live stdout/stderr streaming."""

    max_chars: int = 200_000
    chunks: list[str] = field(default_factory=list)
    total_chars: int = 0
    truncated: bool = False

    def append(self, text: str) -> None:
        if not text:
            return
        self.chunks.append(text)
        self.total_chars += len(text)
        while self.total_chars > self.max_chars and len(self.chunks) > 1:
            removed = self.chunks.pop(0)
            self.total_chars -= len(removed)
            self.truncated = True
        if self.total_chars > self.max_chars and self.chunks:
            self.chunks[0] = self.chunks[0][-self.max_chars :]
            self.total_chars = len(self.chunks[0])
            self.truncated = True

    def get_text(self, tail_chars: int | None = None) -> str:
        joined = "".join(self.chunks)
        if tail_chars is not None and tail_chars > 0 and len(joined) > tail_chars:
            return joined[-tail_chars:]
        return joined


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    buffer: StreamBuffer,
) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buffer.append(chunk.decode("utf-8", errors="replace"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        buffer.append(f"\n[stream error: {exc}]\n")


@dataclass
class BackgroundJob:
    """A tracked background or long-running local process."""

    job_id: str
    command: str
    shell: str
    cwd: str
    pid: int | None
    started_at: str
    started_monotonic: float
    process: asyncio.subprocess.Process
    stdout_buf: StreamBuffer
    stderr_buf: StreamBuffer
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    finished_at: str | None = None
    ended_monotonic: float | None = None
    returncode: int | None = None
    notified_completion: bool = False
    loop: asyncio.AbstractEventLoop | None = None
    _handles_released: bool = False

    def refresh_state(self) -> None:
        if self.returncode is None and self.process.returncode is not None:
            self.returncode = self.process.returncode
            self.ended_monotonic = time.monotonic()
            self.finished_at = datetime.now(UTC).isoformat()

    def release_handles(self) -> None:
        """Cancel finished/dangling stream tasks and close underlying subprocess transport."""
        if self._handles_released:
            return
        self._handles_released = True
        for task in (self.stdout_task, self.stderr_task):
            if not task.done():
                with contextlib.suppress(Exception):
                    task.cancel()
        transport = getattr(self.process, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()

    def schedule_handle_cleanup(self) -> None:
        """Safely release job handles from any thread (loop thread or cleanup thread)."""
        if self._handles_released:
            return
        if self.loop is not None and not self.loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.release_handles)
                return
        with contextlib.suppress(Exception):
            self.release_handles()

    @property
    def is_running(self) -> bool:
        self.refresh_state()
        return self.returncode is None

    @property
    def elapsed_s(self) -> float:
        self.refresh_state()
        end = self.ended_monotonic if self.ended_monotonic is not None else time.monotonic()
        return round(max(0.0, end - self.started_monotonic), 3)

    def to_dict(self, *, include_output: bool = True, tail_chars: int = 16_000) -> JsonObject:
        self.refresh_state()
        payload: JsonObject = {
            "job_id": self.job_id,
            "status": "running" if self.is_running else "completed",
            "pid": self.pid,
            "command": self.command,
            "shell": self.shell,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": self.elapsed_s,
            "returncode": self.returncode,
            "stdout_truncated": self.stdout_buf.truncated,
            "stderr_truncated": self.stderr_buf.truncated,
        }
        if include_output:
            payload["stdout"] = self.stdout_buf.get_text(tail_chars=tail_chars)
            payload["stderr"] = self.stderr_buf.get_text(tail_chars=tail_chars)
        return payload


class BackgroundJobManager:
    """Registry and controller for background commands and non-blocking executions."""

    def __init__(
        self,
        *,
        max_buffer_chars: int = 200_000,
        cleanup_interval_s: float = 30.0,
        finished_ttl_s: float = 180.0,
        start_cleanup_thread: bool = True,
    ) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        self._lock = threading.RLock()
        self._max_buffer_chars = max_buffer_chars
        self._cleanup_interval_s = max(0.05, float(cleanup_interval_s))
        self._finished_ttl_s = max(0.0, float(finished_ttl_s))
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._finalizer: weakref.finalize | None = None
        if start_cleanup_thread:
            self._start_cleanup_thread()

    @staticmethod
    def _cleanup_loop(
        manager_ref: weakref.ref[BackgroundJobManager],
        stop_event: threading.Event,
        interval_s: float,
    ) -> None:
        while not stop_event.wait(interval_s):
            manager = manager_ref()
            if manager is None:
                break
            with contextlib.suppress(Exception):
                manager.clear_finished(max_age_s=manager._finished_ttl_s)
            del manager

    def _start_cleanup_thread(self) -> None:
        thread = threading.Thread(
            target=self._cleanup_loop,
            args=(weakref.ref(self), self._stop_event, self._cleanup_interval_s),
            name="bridge_job_cleanup",
            daemon=True,
        )
        self._cleanup_thread = thread
        self._finalizer = weakref.finalize(self, self._stop_event.set)
        thread.start()

    def close(self) -> None:
        """Stop the periodic background cleanup thread and release tracked finished handles."""
        self._stop_event.set()
        if (
            self._cleanup_thread is not None
            and self._cleanup_thread.is_alive()
            and threading.current_thread() is not self._cleanup_thread
        ):
            self._cleanup_thread.join(timeout=0.5)
        with self._lock:
            for job in list(self._jobs.values()):
                if not job.is_running:
                    job.schedule_handle_cleanup()

    async def start_job(
        self,
        *,
        command: str,
        cwd: Path,
        shell: ShellKind = "auto",
        env: dict[str, str] | None = None,
        allow_stdin: bool = True,
    ) -> BackgroundJob:
        argv, resolved_shell = build_shell_command(command, shell=shell)
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE if allow_stdin else asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(cwd),
            "env": default_non_interactive_env(env),
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _select_windows_creation_flags(
                command,
                background=allow_stdin,
            )
        else:
            kwargs["start_new_session"] = True

        loop = asyncio.get_running_loop()
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        stdout_buf = StreamBuffer(max_chars=self._max_buffer_chars)
        stderr_buf = StreamBuffer(max_chars=self._max_buffer_chars)
        stdout_task = asyncio.create_task(_pump_stream(process.stdout, stdout_buf))
        stderr_task = asyncio.create_task(_pump_stream(process.stderr, stderr_buf))
        job = BackgroundJob(
            job_id=str(uuid4())[:8],
            command=command,
            shell=resolved_shell,
            cwd=str(cwd),
            pid=process.pid,
            started_at=datetime.now(UTC).isoformat(),
            started_monotonic=time.monotonic(),
            process=process,
            stdout_buf=stdout_buf,
            stderr_buf=stderr_buf,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
            loop=loop,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def run_command(
        self,
        *,
        command: str,
        cwd: Path,
        shell: ShellKind = "auto",
        timeout_s: float = 30.0,
        background: bool = False,
        auto_background_on_timeout: bool = True,
        env: dict[str, str] | None = None,
        tail_chars: int = 32_000,
    ) -> JsonObject:
        """Execute a command with hang protection and optional auto-backgrounding."""

        job = await self.start_job(
            command=command,
            cwd=cwd,
            shell=shell,
            env=env,
            allow_stdin=background,
        )
        if background:
            await asyncio.sleep(0.05)
            result = job.to_dict(include_output=True, tail_chars=tail_chars)
            result["background"] = True
            result["timed_out"] = False
            return result

        effective_wait = (
            min(float(timeout_s), 15.0) if auto_background_on_timeout else min(float(timeout_s), 20.0)
        )
        try:
            await asyncio.wait_for(job.process.wait(), timeout=effective_wait)
        except TimeoutError:
            if auto_background_on_timeout:
                result = job.to_dict(include_output=True, tail_chars=tail_chars)
                result["status"] = "running_in_background"
                result["background"] = True
                result["timed_out"] = True
                result["note"] = (
                    f"Command exceeded timeout_s={timeout_s}s and continues running in the "
                    f"background as job_id='{job.job_id}' (pid={job.pid}). Poll with "
                    "process_manager(action='job_status', job_id=...) or stop with "
                    "process_manager(action='kill_job', job_id=...)."
                )
                return result

            await self.kill_job(job.job_id)
            result = job.to_dict(include_output=True, tail_chars=tail_chars)
            result["status"] = "timed_out_and_killed"
            result["background"] = False
            result["timed_out"] = True
            return result

        # Process exited; give stream readers a brief window to flush, then detach if grandchildren
        # kept inherited handles open.
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(
                asyncio.gather(job.stdout_task, job.stderr_task),
                timeout=0.4,
            )
        for task in (job.stdout_task, job.stderr_task):
            if not task.done():
                task.cancel()

        job.refresh_state()
        result = job.to_dict(include_output=True, tail_chars=tail_chars)
        result["background"] = False
        result["timed_out"] = False
        # Clean up completed foreground jobs automatically so registry and handles stay tidy
        with self._lock:
            self._jobs.pop(job.job_id, None)
        job.release_handles()
        return result

    def list_jobs(self) -> list[JsonObject]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [
            job.to_dict(include_output=False)
            for job in sorted(
                jobs,
                key=lambda item: item.started_monotonic,
                reverse=True,
            )
        ]

    def get_job(self, job_id: str) -> BackgroundJob:
        with self._lock:
            if job_id == "latest" and self._jobs:
                return max(self._jobs.values(), key=lambda item: item.started_monotonic)
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job_id: {job_id}")
        return job

    async def wait_for_job(
        self,
        job_id: str,
        *,
        timeout_s: float = 18.0,
        tail_chars: int = 16_000,
    ) -> JsonObject:
        """Wait up to `timeout_s` for `job_id` to finish and return immediately on exit."""
        job = self.get_job(job_id)
        effective_wait = max(0.1, min(float(timeout_s), 22.0))
        if job.is_running:
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(job.process.wait(), timeout=effective_wait)
        if not job.is_running:
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(job.stdout_task, job.stderr_task),
                    timeout=0.3,
                )
            job.notified_completion = True
        return job.to_dict(include_output=True, tail_chars=tail_chars)

    def pop_newly_completed_jobs(self, *, tail_chars: int = 1500) -> list[JsonObject]:
        """Return one-shot completion alerts for background jobs that finished since last check."""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.started_monotonic)
        alerts: list[JsonObject] = []
        for job in jobs:
            if not job.is_running and not job.notified_completion:
                job.notified_completion = True
                alerts.append(
                    {
                        "job_id": job.job_id,
                        "status": "completed",
                        "returncode": job.returncode,
                        "elapsed_s": job.elapsed_s,
                        "finished_at": job.finished_at,
                        "stdout_tail": job.stdout_buf.get_text(tail_chars=tail_chars),
                        "stderr_tail": job.stderr_buf.get_text(tail_chars=600),
                    }
                )
        return alerts

    async def kill_job(self, job_id: str) -> JsonObject:
        job = self.get_job(job_id)
        if job.is_running:
            await kill_process_tree(job.process)
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(job.process.wait(), timeout=2.0)
        job.refresh_state()
        if job.returncode is None:
            job.returncode = -9
            job.ended_monotonic = time.monotonic()
            job.finished_at = datetime.now(UTC).isoformat()
        data = job.to_dict(include_output=True)
        job.release_handles()
        data["status"] = "killed"
        return data

    async def send_input(self, job_id: str, input_text: str) -> JsonObject:
        job = self.get_job(job_id)
        if not job.is_running:
            raise RuntimeError(f"job {job.job_id} is not running")
        if job.process.stdin is None:
            raise RuntimeError(f"job {job.job_id} does not have an open stdin pipe")
        payload = input_text if input_text.endswith("\n") else f"{input_text}\n"
        job.process.stdin.write(payload.encode("utf-8", errors="replace"))
        await job.process.stdin.drain()
        await asyncio.sleep(0.1)
        return job.to_dict(include_output=True)

    def clear_finished(self, *, max_age_s: float | None = None) -> int:
        """Reap orphaned or finished jobs, releasing pipe handles and pruning stale entries."""
        now = time.monotonic()
        with self._lock:
            items = list(self._jobs.items())

        to_remove: list[str] = []
        handles_to_release: list[BackgroundJob] = []
        for jid, job in items:
            running = job.is_running
            if (
                running
                and job.pid is not None
                and (now - job.started_monotonic >= 2.0)
                and not _is_os_pid_alive(job.pid)
            ):
                job.returncode = -1
                job.ended_monotonic = now
                job.finished_at = datetime.now(UTC).isoformat()
                running = False

            if not running:
                handles_to_release.append(job)
                if job.ended_monotonic is None:
                    job.ended_monotonic = now
                if max_age_s is None or (now - job.ended_monotonic) >= max_age_s:
                    to_remove.append(jid)

        for job in handles_to_release:
            job.schedule_handle_cleanup()

        if to_remove:
            with self._lock:
                for jid in to_remove:
                    self._jobs.pop(jid, None)
        return len(to_remove)


_NPM_CMD_SHIM_RE = re.compile(
    r'"%_prog%"\s+"%(?:~)?dp0%\\([^"\r\n]+\.(?:js|mjs|cjs))"\s+%\*',
    re.IGNORECASE,
)


def _resolve_windows_command_argv(command: list[str]) -> list[str]:
    """Bypass cmd.exe for npm .cmd JS wrappers when node and script resolve directly."""
    effective_cmd = list(command)
    if (
        os.name != "nt"
        or not effective_cmd
        or not effective_cmd[0].lower().endswith((".cmd", ".bat"))
    ):
        return effective_cmd

    cmd_path = Path(effective_cmd[0])
    with contextlib.suppress(OSError):
        if cmd_path.is_file() and cmd_path.stat().st_size <= 8192:
            shim_text = cmd_path.read_text(encoding="utf-8", errors="replace")
            match = _NPM_CMD_SHIM_RE.search(shim_text)
            if match:
                rel_script = match.group(1).replace("\\", os.sep)
                script_path = (cmd_path.parent / rel_script).resolve(strict=False)
                local_node = cmd_path.parent / "node.exe"
                node_bin = str(local_node) if local_node.is_file() else shutil.which("node")
                if node_bin and script_path.is_file():
                    return [node_bin, str(script_path), *effective_cmd[1:]]

    comspec = os.environ.get("COMSPEC", "cmd.exe")
    return [comspec, "/d", "/s", "/c", *effective_cmd]


async def _feed_stdin(process: asyncio.subprocess.Process, text: str) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.write(text.encode("utf-8", errors="replace"))
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return
    finally:
        with contextlib.suppress(Exception):
            process.stdin.close()


async def run_argv_captured(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> tuple[int, str, str]:
    """Run an explicit argv command with hang-proof stream collection and tree-kill on timeout."""

    effective_cmd = _resolve_windows_command_argv(command)

    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": str(cwd),
        "env": default_non_interactive_env(env),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _WINDOWS_CREATE_FLAGS
    else:
        kwargs["start_new_session"] = True

    process = await asyncio.create_subprocess_exec(*effective_cmd, **kwargs)
    stdout_buf = StreamBuffer(max_chars=400_000)
    stderr_buf = StreamBuffer(max_chars=400_000)
    stdout_task = asyncio.create_task(_pump_stream(process.stdout, stdout_buf))
    stderr_task = asyncio.create_task(_pump_stream(process.stderr, stderr_buf))
    stdin_task = (
        asyncio.create_task(_feed_stdin(process, stdin_text)) if stdin_text is not None else None
    )

    async def _wait_for_completion() -> None:
        if stdin_task is not None:
            await stdin_task
        await process.wait()

    try:
        await asyncio.wait_for(_wait_for_completion(), timeout=timeout_s)
    except TimeoutError:
        if stdin_task is not None and not stdin_task.done():
            stdin_task.cancel()
        await kill_process_tree(process)
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=2.0)
        if stdin_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stdin_task
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        raise

    with contextlib.suppress(TimeoutError, Exception):
        await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=0.4)
    for task in (stdout_task, stderr_task):
        if not task.done():
            task.cancel()

    return (
        process.returncode or 0,
        stdout_buf.get_text(),
        stderr_buf.get_text(),
    )
