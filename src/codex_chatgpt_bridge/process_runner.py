"""Non-blocking, hang-proof process execution and background job manager."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
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
            "$ProgressPreference = 'SilentlyContinue'; "
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
                f"{utf8_prelude}{command}",
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

    def refresh_state(self) -> None:
        if self.returncode is None and self.process.returncode is not None:
            self.returncode = self.process.returncode
            self.ended_monotonic = time.monotonic()
            self.finished_at = datetime.now(UTC).isoformat()

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

    def __init__(self, *, max_buffer_chars: int = 200_000) -> None:
        self._jobs: dict[str, BackgroundJob] = {}
        self._max_buffer_chars = max_buffer_chars

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
            kwargs["creationflags"] = _WINDOWS_CREATE_FLAGS
        else:
            kwargs["start_new_session"] = True

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
        )
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

        effective_wait = min(float(timeout_s), 12.0) if auto_background_on_timeout else float(timeout_s)
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
        # Clean up completed foreground jobs automatically so registry stays tidy
        self._jobs.pop(job.job_id, None)
        return result

    def list_jobs(self) -> list[JsonObject]:
        return [
            job.to_dict(include_output=False)
            for job in sorted(
                self._jobs.values(),
                key=lambda item: item.started_monotonic,
                reverse=True,
            )
        ]

    def get_job(self, job_id: str) -> BackgroundJob:
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
        """Reactive long-polling: wait up to `timeout_s` for `job_id` to finish and return immediately on exit."""
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
        """Return one-shot completion alerts for any background jobs that finished since last check."""
        alerts: list[JsonObject] = []
        for job in sorted(self._jobs.values(), key=lambda item: item.started_monotonic):
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
        for task in (job.stdout_task, job.stderr_task):
            if not task.done():
                task.cancel()
        job.refresh_state()
        if job.returncode is None:
            job.returncode = -9
            job.ended_monotonic = time.monotonic()
            job.finished_at = datetime.now(UTC).isoformat()
        data = job.to_dict(include_output=True)
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

    def clear_finished(self) -> int:
        to_remove = [jid for jid, job in self._jobs.items() if not job.is_running]
        for jid in to_remove:
            self._jobs.pop(jid, None)
        return len(to_remove)


async def run_argv_captured(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run an explicit argv command with hang-proof stream collection and tree-kill on timeout."""

    effective_cmd = list(command)
    if os.name == "nt" and effective_cmd and effective_cmd[0].lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        effective_cmd = [comspec, "/d", "/s", "/c", *effective_cmd]

    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
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
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except TimeoutError:
        await kill_process_tree(process)
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=2.0)
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
