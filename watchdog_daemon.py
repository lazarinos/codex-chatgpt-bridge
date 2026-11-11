"""Autonomous self-healing watchdog for Bridge (zero false-positive kills under heavy CPU load)."""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BRIDGE_DIR = Path(r"C:\Users\LAZARINOS\codex-chatgpt-bridge-clean")
PID_FILE = BRIDGE_DIR / ".watchdog.pid"
LOG_FILE = BRIDGE_DIR / "watchdog.log"
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8766
LOCAL_HEALTH_URL = f"http://{LOCAL_HOST}:{LOCAL_PORT}/healthz"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [Watchdog PID={os.getpid()}] {msg}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except Exception:
        pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return ctypes.GetLastError() == 5
            try:
                code = ctypes.c_uint32()
                if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True
                return code.value == 259
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _ensure_singleton() -> bool:
    try:
        if PID_FILE.exists():
            old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if old_pid != os.getpid() and _is_pid_alive(old_pid):
                return False
    except Exception:
        pass
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    return True


def is_port_listening() -> bool:
    try:
        with socket.create_connection((LOCAL_HOST, LOCAL_PORT), timeout=1.5):
            return True
    except Exception:
        return False


def check_local_health() -> bool:
    try:
        with urllib.request.urlopen(LOCAL_HEALTH_URL, timeout=6.0) as r:
            return r.status == 200
    except Exception:
        return False


def check_funnel_cli() -> bool:
    try:
        res = subprocess.run(
            ["tailscale", "funnel", "status"],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (res.stdout or "") + (res.stderr or "")
        return "8766" in out and "Funnel on" in out
    except Exception:
        return True


def trigger_restart(reason: str) -> None:
    _log(f"Triggering Bridge recovery ({reason})...")
    ps = BRIDGE_DIR / "restart-bridge.ps1"
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
            cwd=str(BRIDGE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=35,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _log("Bridge recovery completed.")
    except Exception as exc:
        _log(f"Bridge recovery error: {exc}")


def main() -> None:
    if not _ensure_singleton():
        sys.exit(0)

    _log("Watchdog daemon started.")
    http_fails = 0
    tick = 0
    while True:
        time.sleep(5.0)
        tick += 1
        try:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass

        if check_local_health():
            http_fails = 0
        else:
            # If the TCP port is not listening at all, the Bridge process exited -> recover immediately!
            if not is_port_listening():
                http_fails = 0
                trigger_restart("TCP port 8766 closed / process exited")
                time.sleep(3.0)
                continue

            # Port is open (process is alive), wait for 6 consecutive slow healthchecks (30s+)
            # so heavy compilation spikes never cause false-positive kills.
            http_fails += 1
            if http_fails >= 6:
                http_fails = 0
                trigger_restart("6 consecutive HTTP /healthz timeouts while port open")
                time.sleep(3.0)
                continue

        if tick % 6 == 0 and not check_funnel_cli():
            _log("Re-enabling Tailscale Funnel on port 8766...")
            try:
                subprocess.run(
                    ["tailscale", "funnel", "--bg", "--yes", "8766"],
                    cwd=str(BRIDGE_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=12,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass


if __name__ == "__main__":
    main()
