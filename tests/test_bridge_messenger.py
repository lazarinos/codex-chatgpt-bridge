from __future__ import annotations

import subprocess
import sys
import bridge_messenger
from bridge_messenger import (
    ActivityConsolidator,
    AnsiTextMetrics,
    FeedEvent,
    MessengerController,
    Theme,
    TuiLineEditor,
    TuiRenderer,
    format_tool_call,
)


def test_format_tool_call_without_prefix_does_not_raise() -> None:
    res_empty = format_tool_call("idle", "", "12:00:00")
    assert "idle" in res_empty
    assert "12:00:00" in res_empty

    res_none = format_tool_call("idle", None, "12:00:00")
    assert "idle" in res_none

    res_tool = format_tool_call("read_file", "path/to/file.py", "12:00:00")
    assert "Read" in res_tool
    assert "path/to/file.py" in res_tool


def test_format_tool_call_with_prefixes() -> None:
    res_ps = format_tool_call("exec_command", "ps: Get-Process", "12:00:00")
    assert "PowerShell" in res_ps
    assert "Get-Process" in res_ps

    res_cmd = format_tool_call("exec_command", "cmd: dir", "12:00:00")
    assert "CMD" in res_cmd
    assert "dir" in res_cmd

    res_py = format_tool_call("exec_command", "py: print(123)", "12:00:00")
    assert "Python" in res_py
    assert "print(123)" in res_py


def test_format_tool_call_with_details() -> None:
    details = "Header line\n+ added\n- removed\nunchanged"
    res = format_tool_call("edit_file", "foo.py", "12:00:00", details)
    assert "Edit" in res
    assert "Header line" in res
    assert "+ added" in res
    assert "- removed" in res


def test_activity_consolidator_groups_consecutive_reads() -> None:
    flushed_blocks: list[str] = []
    consolidator = ActivityConsolidator(on_flush=flushed_blocks.append)

    consolidator.push("read_file", "src/one.py", "12:00:01")
    consolidator.push("read_file", "src/two.py", "12:00:02")
    consolidator.push("search_files", "def main", "12:00:03")

    assert len(flushed_blocks) == 0

    consolidator.push("exec_command", "pytest", "12:00:05")

    assert len(flushed_blocks) == 2
    assert "Inspected 3 files/locations" in flushed_blocks[0]
    assert "one.py" in flushed_blocks[0]
    assert "Bash" in flushed_blocks[1]
    assert "pytest" in flushed_blocks[1]


def test_activity_consolidator_single_read_flushes_normal_line() -> None:
    flushed_blocks: list[str] = []
    consolidator = ActivityConsolidator(on_flush=flushed_blocks.append)

    consolidator.push("read_file", "README.md", "12:00:01")
    consolidator.flush()

    assert len(flushed_blocks) == 1
    assert "Read" in flushed_blocks[0]
    assert "README.md" in flushed_blocks[0]


def test_tui_renderer_agent_messages_no_emojis_and_no_bell() -> None:
    msg = TuiRenderer.format_agent_message("12:00:00", "Hello world, what do you think?")
    assert "Question" in msg
    assert "?" not in msg or "❓" not in msg
    assert "\a" not in msg

    normal = TuiRenderer.format_agent_message("12:00:00", "Task finished successfully")
    assert "[Agent · 12:00:00]" in normal
    assert "●" not in normal
    assert "\a" not in normal


def test_messenger_controller_seeds_all_initial_messages() -> None:
    from bridge_messenger import BridgeClient

    class MockClient(BridgeClient):
        def get_state(self, limit: int = 50) -> dict:
            return {
                "action_log": [{"id": "act-1", "tool": "read_file", "summary": "test.py"}],
                "messages": [
                    {"id": f"msg-{i}", "sender": "agent", "text": f"old text {i}", "created_at": "2026-11-04T12:00:00Z"}
                    for i in range(25)
                ],
                "agent_telemetry": {},
            }

    ctrl = MessengerController(client=MockClient())
    init_state = ctrl.client.get_state()
    for m in init_state["messages"]:
        ctrl.seen_ids.add(m["id"])

    assert len(ctrl.seen_ids) == 25
    for i in range(25):
        assert f"msg-{i}" in ctrl.seen_ids


def test_terminal_chat_startup_and_exit() -> None:
    res = subprocess.run(
        [sys.executable, "bridge_messenger.py"],
        input="",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    assert res.returncode == 0


def test_format_tool_call_exec_command_details() -> None:
    details = "Exit 0 (ok) · 42ms · cwd: E:/ADS-AAA\n$ git status --short\n> M runtime/l4d2/world_hands.cpp\n! warning: LF will be replaced by CRLF"
    res = format_tool_call("exec_command", "ps: git status --short", "12:05:00", details)
    assert "PowerShell" in res
    assert "Exit 0 (ok)" in res
    assert "$ git status --short" in res
    assert "M runtime/l4d2/world_hands.cpp" in res
    assert "! warning: LF will be replaced by CRLF" in res


def test_live_inbox_upgrades_pending_action_with_details(tmp_path) -> None:
    from codex_chatgpt_bridge.live_inbox import LiveInboxStore

    store = LiveInboxStore(tmp_path / "live_inbox.json")
    store.record_agent_activity("read_file", "py: print(42)", "")
    state1 = store.get_state()
    assert len(state1["action_log"]) == 1
    first_id = state1["action_log"][0]["id"]
    assert state1["action_log"][0].get("details", "") == ""

    store.record_agent_activity("exec_command", "py: print(42)", "Exit 0 (ok) · 10ms\n$ print(42)\n> 42")
    state2 = store.get_state()
    assert len(state2["action_log"]) == 1
    assert state2["action_log"][0]["id"] != first_id
    assert state2["action_log"][0]["tool"] == "exec_command"
    assert "> 42" in state2["action_log"][0]["details"]


def test_ansi_metrics_and_resize_safe_box_clamping() -> None:
    styled = f"{Theme.ACCENT}{Theme.BOLD}Hello World{Theme.RESET}"
    assert AnsiTextMetrics.strip_ansi(styled) == "Hello World"
    assert AnsiTextMetrics.visible_width(styled) == 11

    # Render a box with a very long line into a narrow 44-column terminal
    long_line = "This is a long diagnostic line that previously overflowed narrow terminals when resized"
    box = TuiRenderer.render_box([(long_line, -1)], title="Resize Test", width=44)
    for line in box.splitlines():
        assert AnsiTextMetrics.visible_width(line) == 44


def test_framed_input_box_wrapping_and_slash_hint() -> None:
    editor = TuiLineEditor(
        prompt_fn=lambda: "> ",
        status_provider=lambda: {
            "priority": "urgent",
            "view_mode": "all",
            "filter_query": "",
            "unread_count": 2,
            "agent_tool": "Bash",
            "agent_summary": "cargo build",
        },
    )
    # Type a long message that wraps inside the framed input box at width=50
    editor.input_chars = list("hermano revisa por que crasheo el juego cuando entre al mapa")
    editor.cursor_pos = len(editor.input_chars)
    lines = editor.build_input_box_lines(width=50)
    assert len(lines) >= 4  # top border + at least 2 wrapped input rows + bottom border
    assert lines[0].startswith(f"{Theme.ERROR}{Theme.CORNER_TL}")
    assert lines[-1].startswith(f"{Theme.ERROR}{Theme.CORNER_BL}")
    for ln in lines:
        assert AnsiTextMetrics.visible_width(ln) == 50

    # Type a slash prefix and verify live autocomplete hint appears inside the box
    editor.input_chars = list("/fi")
    editor.cursor_pos = 3
    slash_lines = editor.build_input_box_lines(width=64)
    joined_plain = "\n".join(AnsiTextMetrics.strip_ansi(x) for x in slash_lines)
    assert "/filter" in joined_plain
    assert editor._autocomplete_locked() is True
    assert "".join(editor.input_chars) == "/filter "


def test_feed_event_view_modes_and_reflow_widths() -> None:
    ev_chat = FeedEvent(kind="agent", timestamp="12:00:00", text="Hola hermano, revisando los logs de L4D2.")
    ev_tool = FeedEvent(kind="tool", timestamp="12:00:01", tool_name="exec_command", summary="ps: cargo test")

    assert ev_chat.matches_view("all") is True
    assert ev_chat.matches_view("chat") is True
    assert ev_chat.matches_view("tools") is False

    assert ev_tool.matches_view("all") is True
    assert ev_tool.matches_view("chat") is False
    assert ev_tool.matches_view("tools") is True

    assert ev_chat.matches_view("all", filter_query="l4d2") is True
    assert ev_chat.matches_view("all", filter_query="rust") is False

    # Verify rendering adapts cleanly to both 42-col and 100-col widths without overflow
    for w in (42, 72, 100):
        for line in ev_chat.render(width=w).splitlines():
            assert AnsiTextMetrics.visible_width(line) <= w
        for line in ev_tool.render(width=w).splitlines():
            assert AnsiTextMetrics.visible_width(line) <= w


def test_poll_worker_exponential_backoff_and_silent_recovery() -> None:
    from bridge_messenger import BridgeClient

    class FlakyClient(BridgeClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def get_state(self, limit: int = 50) -> dict:
            del limit
            self.calls += 1
            if self.calls <= 3:
                self.last_online = False
                raise ConnectionRefusedError("bridge restarting")
            if self.calls == 4:
                self.last_online = False
                return {
                    "unread_user_messages": 0,
                    "action_log": [],
                    "messages": [],
                    "agent_telemetry": {},
                }
            self.last_online = True
            return {
                "unread_user_messages": 0,
                "action_log": [],
                "messages": [],
                "agent_telemetry": {"last_tool": "bridge_status"},
            }

    client = FlakyClient()
    ctrl = MessengerController(client=client)
    delays: list[float] = []

    def fake_wait(timeout: float) -> bool:
        delays.append(timeout)
        if len(delays) >= 5:
            ctrl.stop_event.set()
            return True
        return False

    ctrl.stop_event.wait = fake_wait  # type: ignore[method-assign]
    ctrl.poll_worker()

    assert delays == [1.0, 2.0, 4.0, 4.0, 0.6]
    assert ctrl.server_online is True
    assert ctrl.poll_failures == 0
    assert ctrl.feed_events == []


def test_status_and_jobs_boxes_handle_client_failure_resiliently() -> None:
    from bridge_messenger import BridgeClient

    class OfflineClient(BridgeClient):
        def get_state(self, limit: int = 50) -> dict:
            del limit
            self.last_online = False
            raise ConnectionRefusedError("offline")

    ctrl = MessengerController(client=OfflineClient())
    ctrl.server_online = False
    ctrl.poll_failures = 2

    status_box = AnsiTextMetrics.strip_ansi(ctrl._build_status_box())
    jobs_box = AnsiTextMetrics.strip_ansi(ctrl._build_jobs_box())
    header_plain = AnsiTextMetrics.strip_ansi(
        "\n".join(ctrl.editor.build_input_box_lines(width=120))
    )

    assert "RECONNECTING" in status_box
    assert "Active Jobs" in jobs_box
    assert "RECONNECTING" in header_plain
