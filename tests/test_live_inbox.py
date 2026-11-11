from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import codex_chatgpt_bridge.live_inbox as live_inbox
from codex_chatgpt_bridge.live_inbox import LiveInboxStore


class _TrackingRLock:
    def __init__(self) -> None:
        self._inner = threading.RLock()
        self.depth = 0
        self.acquires = 0

    def __enter__(self) -> _TrackingRLock:
        self._inner.acquire()
        self.depth += 1
        self.acquires += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.depth -= 1
        self._inner.release()


def test_load_acquires_global_store_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox_path = tmp_path / "live_inbox.json"
    inbox_path.write_text(json.dumps({"messages": [], "action_log": []}), encoding="utf-8")

    tracker = _TrackingRLock()
    monkeypatch.setattr(live_inbox, "_STORE_LOCK", tracker)

    depths_during_read: list[int] = []
    orig_read_text = Path.read_text

    def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == inbox_path:
            depths_during_read.append(tracker.depth)
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_read_text)

    store = LiveInboxStore(inbox_path)
    state = store._load()

    assert state["messages"] == []
    assert tracker.acquires >= 1
    assert depths_during_read == [1]


def test_load_and_save_retry_winerror32_with_15_20ms_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox_path = tmp_path / "live_inbox.json"
    inbox_path.write_text(
        json.dumps({"messages": [{"id": "1", "sender": "user", "text": "hola"}]}),
        encoding="utf-8",
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", sleep_calls.append)

    orig_read_text = Path.read_text
    read_attempts = {"count": 0}

    def flaky_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == inbox_path:
            read_attempts["count"] += 1
            if read_attempts["count"] <= 2:
                raise PermissionError(13, "WinError 32 locked", str(self))
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    store = LiveInboxStore(inbox_path)
    loaded = store._load()
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0]["text"] == "hola"
    assert sleep_calls == [0.015, 0.020]

    sleep_calls.clear()
    orig_write_text = Path.write_text
    write_attempts = {"count": 0}

    def flaky_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.parent == tmp_path and self.suffix == ".tmp":
            write_attempts["count"] += 1
            if write_attempts["count"] <= 2:
                raise PermissionError(13, "WinError 32 temp write locked", str(self))
        return orig_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)
    msg = store.send_user_message("segundo mensaje", priority="urgent")
    assert msg["text"] == "segundo mensaje"
    assert sleep_calls == [0.015, 0.020]


def test_load_propagates_persistent_permission_error_instead_of_wiping_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox_path = tmp_path / "live_inbox.json"
    inbox_path.write_text(
        json.dumps({"messages": [{"id": "1", "sender": "user", "text": "keep me"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    def always_locked(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == inbox_path:
            raise PermissionError(13, "WinError 32 persistent lock", str(self))
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "read_text", always_locked)

    store = LiveInboxStore(inbox_path)
    with pytest.raises(PermissionError):
        store._load()


def test_concurrent_readers_and_writers_preserve_messages_without_errors(
    tmp_path: Path,
) -> None:
    store = LiveInboxStore(tmp_path / "live_inbox.json")
    popped_texts: list[str] = []
    pop_lock = threading.Lock()

    def worker(worker_idx: int) -> None:
        for i in range(12):
            store.send_user_message(f"w{worker_idx}-m{i}", priority="high")
            store.record_agent_activity("read_file", f"file_{worker_idx}_{i}.py")
            state = store.get_state(limit=50)
            assert "messages" in state
            unread = store.pop_unread_for_agent(tool_name=f"worker_{worker_idx}")
            if unread:
                with pop_lock:
                    popped_texts.extend(item["text"] for item in unread)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(worker, idx) for idx in range(6)]
        for fut in futures:
            fut.result()

    remaining_unread = [m["text"] for m in store.pop_unread_for_agent(tool_name="final")]
    total_delivered = len(popped_texts) + len(remaining_unread)
    assert total_delivered == 6 * 12
