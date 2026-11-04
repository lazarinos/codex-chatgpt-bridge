from __future__ import annotations

import subprocess
import sys
import bridge_messenger
from bridge_messenger import ActivityConsolidator, TuiRenderer, format_tool_call


def test_format_tool_call_without_prefix_does_not_raise() -> None:
    # Empty or none summaries
    res_empty = format_tool_call("idle", "", "12:00:00")
    assert "idle" in res_empty
    assert "12:00:00" in res_empty

    res_none = format_tool_call("idle", None, "12:00:00")
    assert "idle" in res_none

    # Standard tool without command prefix
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

    # Push 3 consecutive reads
    consolidator.push("read_file", "src/one.py", "12:00:01")
    consolidator.push("read_file", "src/two.py", "12:00:02")
    consolidator.push("search_files", "def main", "12:00:03")

    # Nothing flushed yet because they are batched
    assert len(flushed_blocks) == 0

    # Significant action arrives: triggers flush of consolidated inspections + immediate action
    consolidator.push("exec_command", "pytest", "12:00:05")

    assert len(flushed_blocks) == 2
    # First block is the consolidated summary of the 3 inspections
    assert "Inspected 3 files/locations" in flushed_blocks[0]
    assert "one.py" in flushed_blocks[0]
    # Second block is the bash command
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
    # Check that it formats as a question without emoji characters or ASCII bells
    assert "Question" in msg
    assert "?" not in msg or "❓" not in msg
    assert "\a" not in msg

    normal = TuiRenderer.format_agent_message("12:00:00", "Task finished successfully")
    assert "[Agent · 12:00:00]" in normal
    assert "●" not in normal
    assert "\a" not in normal


def test_messenger_controller_seeds_all_initial_messages() -> None:
    from bridge_messenger import MessengerController, BridgeClient

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
    # Mock terminal width and stdout write to avoid printing during test
    ctrl.client = MockClient()
    init_state = ctrl.client.get_state()
    for m in init_state["messages"]:
        ctrl.seen_ids.add(m["id"])

    assert len(ctrl.seen_ids) == 25
    # Verify every pre-existing message ID is in seen_ids
    for i in range(25):
        assert f"msg-{i}" in ctrl.seen_ids


def test_terminal_chat_startup_and_exit() -> None:
    # Run bridge_messenger with empty input via pipe; it should exit with code 0 cleanly
    res = subprocess.run(
        [sys.executable, "bridge_messenger.py"],
        input="",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    assert res.returncode == 0
