"""Non-interrupting Live User-to-Agent Message Queue for Bridge."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

JsonObject = dict[str, Any]
_T = TypeVar("_T")
_STORE_LOCK = threading.RLock()
_IO_RETRY_ATTEMPTS: int = 3
_IO_BACKOFF_SCHEDULE_S: tuple[float, ...] = (0.015, 0.020)

_SILENT_ACTIVITY_SUFFIXES = (
    "agent_outbox.txt",
    "bridge_chat.txt",
    "live_inbox.json",
    "run_task.py",
)
_SILENT_ACTIVITY_PREFIXES = (
    "inbox:",
    "jobs:",
)


def _run_with_io_retry(
    operation: Callable[[], _T],
    *,
    attempts: int = _IO_RETRY_ATTEMPTS,
    backoff_s: tuple[float, ...] = _IO_BACKOFF_SCHEDULE_S,
) -> _T:
    """Retry transient Windows file lock errors (WinError 32) with short 15-20 ms backoff."""
    total = max(1, attempts)
    for attempt in range(total):
        try:
            return operation()
        except FileNotFoundError:
            raise
        except PermissionError:
            if attempt >= total - 1:
                raise
        except OSError as exc:
            if attempt >= total - 1 or getattr(exc, "winerror", None) not in {5, 32, 33}:
                raise
        time.sleep(backoff_s[min(attempt, len(backoff_s) - 1)])
    raise RuntimeError("unreachable")


def _read_text_with_retry(path: Path) -> str:
    return _run_with_io_retry(lambda: path.read_text(encoding="utf-8", errors="replace"))


def _write_text_with_retry(path: Path, payload: str) -> int:
    return _run_with_io_retry(lambda: path.write_text(payload, encoding="utf-8"))


def _unlink_with_retry(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        _run_with_io_retry(lambda: path.unlink(missing_ok=True))


def default_inbox_path(config_path: Path | None = None) -> Path:
    """Return the path to the persistent live inbox JSON file."""
    if config_path is not None:
        base_dir = config_path.parent
    else:
        primary = Path.home() / ".bridge"
        legacy = Path.home() / ".codex-chatgpt-bridge"
        base_dir = legacy if not primary.exists() and legacy.exists() else primary
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "live_inbox.json"


def _dedup_messages(messages: list[JsonObject]) -> tuple[list[JsonObject], bool]:
    """Collapse consecutive duplicate messages from the same sender with identical text."""
    deduped: list[JsonObject] = []
    changed = False
    for m in messages:
        if (
            deduped
            and deduped[-1].get("sender") == m.get("sender")
            and deduped[-1].get("text") == m.get("text")
        ):
            changed = True
            continue
        deduped.append(m)
    return deduped, changed


def _should_play_beep() -> bool:
    """Suppress hardware audio cues during automated test runs or explicit silent mode."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    flag = os.environ.get("BRIDGE_DISABLE_BEEP", "").strip().lower()
    return flag not in {"1", "true", "yes", "on"}


class LiveInboxStore:
    """Persistent, atomic message queue allowing the user to send live instructions mid-loop."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_inbox_path()
        self._cached_mtime_ns: int | None = None
        self._cached_unread_count: int = 0

    @property
    def path(self) -> Path:
        return self._path

    def _update_unread_cache(self, data: JsonObject) -> None:
        self._cached_unread_count = sum(
            1
            for m in data.get("messages", [])
            if m.get("sender") == "user" and not m.get("read_by_agent", False)
        )
        with contextlib.suppress(OSError):
            if self._path.exists():
                self._cached_mtime_ns = self._path.stat().st_mtime_ns

    def peek_unread_count_cached(self) -> int:
        """Fast O(1) unread check for /healthz that avoids disk I/O when file mtime is unchanged."""
        outbox = self._path.parent / "agent_outbox.txt"
        with contextlib.suppress(OSError):
            if not outbox.exists() and self._path.exists():
                mtime_ns = self._path.stat().st_mtime_ns
                if self._cached_mtime_ns is not None and mtime_ns == self._cached_mtime_ns:
                    return self._cached_unread_count
        return len(self.peek_unread_for_agent())

    def _load(self) -> JsonObject:
        with _STORE_LOCK:
            outbox = self._path.parent / "agent_outbox.txt"
            data: JsonObject | None = None
            if self._path.exists():
                for json_attempt in range(_IO_RETRY_ATTEMPTS):
                    try:
                        raw_text = _read_text_with_retry(self._path)
                        loaded = json.loads(raw_text)
                        if isinstance(loaded, dict):
                            data = loaded
                        break
                    except FileNotFoundError:
                        break
                    except json.JSONDecodeError:
                        if json_attempt < _IO_RETRY_ATTEMPTS - 1:
                            time.sleep(
                                _IO_BACKOFF_SCHEDULE_S[
                                    min(json_attempt, len(_IO_BACKOFF_SCHEDULE_S) - 1)
                                ]
                            )
            if data is None:
                data = {
                    "messages": [],
                    "action_log": [],
                    "agent_telemetry": {
                        "last_tool": "idle",
                        "last_summary": "",
                        "updated_at": "",
                    },
                }
            data.setdefault("messages", [])
            data.setdefault("action_log", [])
            data.setdefault(
                "agent_telemetry",
                {"last_tool": "idle", "last_summary": "", "updated_at": ""},
            )
            deduped, dedup_changed = _dedup_messages(data["messages"])
            if dedup_changed:
                data["messages"] = deduped
            if outbox.exists():
                with contextlib.suppress(FileNotFoundError, PermissionError):
                    raw_out = _read_text_with_retry(outbox).strip()
                    _unlink_with_retry(outbox)
                    if raw_out and not (
                        data["messages"]
                        and data["messages"][-1].get("sender") == "agent"
                        and data["messages"][-1].get("text") == raw_out
                    ):
                        now_iso = datetime.now(UTC).isoformat()
                        data["messages"].append(
                            {
                                "id": str(uuid4())[:8],
                                "sender": "agent",
                                "text": raw_out,
                                "priority": "normal",
                                "created_at": now_iso,
                                "read_by_agent": True,
                                "delivered_in_tool": "agent_outbox",
                                "delivered_at": now_iso,
                            }
                        )
                        dedup_changed = True
            if dedup_changed:
                self._save(data)
            else:
                self._update_unread_cache(data)
            return data

    def _save(self, data: JsonObject) -> None:
        with _STORE_LOCK:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if len(data.get("messages", [])) > 150:
                data["messages"] = data["messages"][-150:]
            if len(data.get("action_log", [])) > 80:
                data["action_log"] = data["action_log"][-80:]
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            for attempt in range(_IO_RETRY_ATTEMPTS):
                tmp = self._path.with_suffix(f".{os.getpid()}.{uuid4().hex[:6]}.tmp")
                try:
                    _write_text_with_retry(tmp, payload)
                    tmp.replace(self._path)
                    self._update_unread_cache(data)
                    return
                except PermissionError:
                    _unlink_with_retry(tmp)
                    if attempt < _IO_RETRY_ATTEMPTS - 1:
                        time.sleep(
                            _IO_BACKOFF_SCHEDULE_S[min(attempt, len(_IO_BACKOFF_SCHEDULE_S) - 1)]
                        )
                except Exception:
                    _unlink_with_retry(tmp)
                    raise
            _write_text_with_retry(self._path, payload)
            self._update_unread_cache(data)

    def send_user_message(self, text: str, *, priority: str = "normal") -> JsonObject:
        """Enqueue a new live message from the user without interrupting the agent loop."""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("message text must not be empty")
        with _STORE_LOCK:
            data = self._load()
            msg: JsonObject = {
                "id": str(uuid4())[:8],
                "sender": "user",
                "text": cleaned,
                "priority": priority if priority in {"normal", "high", "urgent"} else "normal",
                "created_at": datetime.now(UTC).isoformat(),
                "read_by_agent": False,
                "delivered_in_tool": None,
                "delivered_at": None,
            }
            data["messages"].append(msg)
            self._save(data)
            return msg

    def send_agent_reply(self, text: str) -> JsonObject:
        """Enqueue a non-blocking reply from the agent to the user's desktop UI."""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("reply text must not be empty")
        with _STORE_LOCK:
            data = self._load()
            if (
                data["messages"]
                and data["messages"][-1].get("sender") == "agent"
                and data["messages"][-1].get("text") == cleaned
            ):
                return dict(data["messages"][-1])
            now_iso = datetime.now(UTC).isoformat()
            msg: JsonObject = {
                "id": str(uuid4())[:8],
                "sender": "agent",
                "text": cleaned,
                "priority": "normal",
                "created_at": now_iso,
                "read_by_agent": True,
                "delivered_in_tool": "agent_reply",
                "delivered_at": now_iso,
            }
            data["messages"].append(msg)
            self._save(data)

        if _should_play_beep():
            def _beep_async() -> None:
                with contextlib.suppress(Exception):
                    import winsound

                    winsound.Beep(880, 110)
                    winsound.Beep(1175, 140)

            threading.Thread(target=_beep_async, daemon=True).start()
        return msg

    def pop_unread_for_agent(self, *, tool_name: str) -> list[JsonObject]:
        """Atomically fetch and mark unread user messages as delivered during `tool_name`."""
        with _STORE_LOCK:
            data = self._load()
            unread: list[JsonObject] = []
            now = datetime.now(UTC).isoformat()
            changed = False
            for m in data.get("messages", []):
                if m.get("sender") == "user" and not m.get("read_by_agent", False):
                    m["read_by_agent"] = True
                    m["delivered_in_tool"] = tool_name
                    m["delivered_at"] = now
                    unread.append(dict(m))
                    changed = True
            if changed:
                self._save(data)
            return unread

    def peek_unread_for_agent(self) -> list[JsonObject]:
        """Return unread user messages without marking them read."""
        with _STORE_LOCK:
            data = self._load()
            return [
                dict(m)
                for m in data.get("messages", [])
                if m.get("sender") == "user" and not m.get("read_by_agent", False)
            ]

    def record_agent_activity(self, tool_name: str, summary: str = "", details: str = "") -> None:
        """Update the last tool executed and append signal-only entries to action_log."""
        clean_sum = summary.strip()[:240]
        lower_sum = clean_sum.lower()
        if lower_sum.startswith(_SILENT_ACTIVITY_PREFIXES) or lower_sum.endswith(
            _SILENT_ACTIVITY_SUFFIXES
        ):
            return
        with _STORE_LOCK:
            data = self._load()
            now_iso = datetime.now(UTC).isoformat()
            data["agent_telemetry"] = {
                "last_tool": tool_name,
                "last_summary": clean_sum,
                "updated_at": now_iso,
            }
            action_log = data.setdefault("action_log", [])
            if action_log:
                last = action_log[-1]
                same_or_delegated = last.get("tool") == tool_name or (
                    last.get("tool") in {"read_file", "write_file"}
                    and tool_name in {"exec_command", "edit_file", "write_file"}
                )
                if same_or_delegated and last.get("summary") == clean_sum and not details:
                    last["timestamp"] = now_iso
                    self._save(data)
                    return
                if same_or_delegated and details and not last.get("details"):
                    last["id"] = str(uuid4())[:8]
                    last["tool"] = tool_name
                    last["summary"] = clean_sum
                    last["timestamp"] = now_iso
                    last["details"] = details[:2400]
                    self._save(data)
                    return

            entry: JsonObject = {
                "id": str(uuid4())[:8],
                "tool": tool_name,
                "summary": clean_sum,
                "timestamp": now_iso,
            }
            if details:
                entry["details"] = details[:2400]
            action_log.append(entry)
            self._save(data)

    def get_state(self, *, limit: int = 50) -> JsonObject:
        """Return the current inbox state and telemetry."""
        with _STORE_LOCK:
            data = self._load()
            msgs = data.get("messages", [])[-limit:]
            actions = data.get("action_log", [])[-40:]
            unread_count = sum(
                1
                for m in data.get("messages", [])
                if m.get("sender") == "user" and not m.get("read_by_agent", False)
            )
            return {
                "unread_user_messages": unread_count,
                "total_messages": len(data.get("messages", [])),
                "agent_telemetry": data.get("agent_telemetry", {}),
                "action_log": actions,
                "messages": msgs,
            }

    def clear(self) -> JsonObject:
        """Clear all messages from the live inbox."""
        with _STORE_LOCK:
            data = self._load()
            count = len(data.get("messages", []))
            data["messages"] = []
            self._save(data)
            return {"cleared": count}
