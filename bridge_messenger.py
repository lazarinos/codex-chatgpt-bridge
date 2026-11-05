#!/usr/bin/env python3
"""
Codex ChatGPT Bridge - Minimalist & Elegant Interactive TUI Messenger
Allows real-time two-way communication between the user and the active AI Agent mid-loop
without pressing Stop or interrupting its tool execution workflow.

Architecture: SOLID, SSOT, DRY, Separation of Concerns (SoC).
- Theme: Single Source of Truth for ANSI styling, sober typography, and clean box frames.
- ActivityConsolidator: Groups consecutive read-only actions (read_file, search_files, list_dir)
  into a consolidated dynamic summary to prevent terminal spam ("read read read...").
- TuiRenderer: Minimalist, technical rendering (zero kitsch unicode/emojis).
- TuiLineEditor: Non-destructive line editor with cursor navigation (<-/->, Home/End, Delete)
  and command history (^/v) with draft buffer preservation.
- BridgeClient: Decoupled HTTP transport with atomic offline fallback.
- MessengerController: Command dispatcher and event loop.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

if os.name == "nt":
    import msvcrt
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BRIDGE_URL = "http://127.0.0.1:8766"
FALLBACK_INBOX = Path.home() / ".codex-chatgpt-bridge" / "live_inbox.json"


# -----------------------------------------------------------------------------
# 1. SSOT: Theme & Typographic System
# -----------------------------------------------------------------------------
class Theme:
    """Single Source of Truth for colors, styles, and minimalist symbols."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Minimalist, sober palette (Claude & Codex developer aesthetics)
    ACCENT = "\033[38;5;209m"     # Terracotta / Coral
    PROMPT = "\033[38;5;215m"     # Warm amber
    TOOL = "\033[38;5;117m"       # Sky cyan
    SUCCESS = "\033[38;5;114m"    # Soft emerald (+ additions, ok)
    WARN = "\033[38;5;222m"       # Soft gold (queued, highlights)
    INFO = "\033[38;5;141m"       # Lavender (questions, thinking)
    ERROR = "\033[38;5;203m"      # Soft crimson (- deletions, errors)
    MUTED = "\033[38;5;244m"      # Slate gray (sub-lines, timestamps)
    DARK = "\033[38;5;238m"       # Frame gray
    TEXT = "\033[97m"             # Bright white text

    # Clean, sober symbols (Zero kitsch emojis)
    BULLET = "*"
    CONNECTOR = "|"
    PROMPT_SYMBOL = ">"

    # Box-drawing elements
    CORNER_TL = "╭"
    CORNER_TR = "╮"
    CORNER_BL = "╰"
    CORNER_BR = "╯"
    HLINE = "─"
    VLINE = "│"

    @classmethod
    def term_width(cls) -> int:
        try:
            cols = shutil.get_terminal_size((88, 24)).columns
            return max(64, min(cols - 2, 110))
        except Exception:
            return 86


def enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        h_out = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# 2. Transport Layer: BridgeClient (Decoupled Network & Fallback)
# -----------------------------------------------------------------------------
class BridgeClient:
    """Handles communication with the Codex ChatGPT Bridge HTTP daemon."""

    def __init__(self, base_url: str = BRIDGE_URL, fallback_path: Path = FALLBACK_INBOX) -> None:
        self.base_url = base_url
        self.fallback_path = fallback_path

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
            from codex_chatgpt_bridge.live_inbox import LiveInboxStore

            store = LiveInboxStore(self.fallback_path)
            if path == "/inbox/send" and payload:
                msg = store.send_user_message(payload.get("text", ""), priority=payload.get("priority", "normal"))
                return {"status": "queued_offline", "message": msg, "inbox": store.get_state()}
            if path == "/inbox/clear":
                return store.clear()
            return store.get_state()

    def get_state(self, limit: int = 50) -> dict:
        return self.request(f"/inbox?limit={limit}")

    def send_user_message(self, text: str, priority: str = "normal") -> dict:
        return self.request("/inbox/send", method="POST", payload={"text": text, "priority": priority})

    def clear_inbox(self) -> dict:
        return self.request("/inbox/clear", method="POST")


# Module-level convenience wrapper for compatibility
_default_client = BridgeClient()


def api_request(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    return _default_client.request(path, method=method, payload=payload)


# -----------------------------------------------------------------------------
# 3. Domain Logic: ActivityConsolidator (Anti-Spam Tool Grouping)
# -----------------------------------------------------------------------------
@dataclass
class InspectionItem:
    tool: str
    target: str
    timestamp: str


class ActivityConsolidator:
    """
    Prevents terminal flooding when the agent runs back-to-back read/search operations.
    Consecutive inspections (read_file, search_files, list_dir) are consolidated
    into a single clean summary line instead of 20 separate blocks.
    """

    INSPECTION_TOOLS = {"read_file", "search_files", "list_dir", "file_ops"}

    def __init__(self, on_flush: Callable[[str], None]) -> None:
        self.on_flush = on_flush
        self._pending_inspections: list[InspectionItem] = []
        self._lock = threading.Lock()
        self._last_push_time = 0.0

    def is_inspection(self, tool_name: str, summary: str = "") -> bool:
        if tool_name not in self.INSPECTION_TOOLS:
            return False
        clean = (summary or "").strip().lower()
        if clean.startswith(("ps:", "cmd:", "py:", "powershell:", "python:", "exec:", "sh:", "bg:", "jobs:", "inbox:")):
            return False
        return True

    def push(self, tool_name: str, summary: str, timestamp: str, details: str = "") -> None:
        with self._lock:
            if self.is_inspection(tool_name, summary) and not details:
                clean_target = summary.strip()
                self._pending_inspections.append(InspectionItem(tool=tool_name, target=clean_target, timestamp=timestamp))
                self._last_push_time = time.time()
                return

            # Significant tool (bash, commands, edit, write, or inspection with details): flush pending reads first
            self._flush_locked()
            block = TuiRenderer.format_tool_call(tool_name, summary, timestamp, details)
            self.on_flush(block)

    def check_idle_flush(self, idle_seconds: float = 1.0) -> None:
        """Called by poller loop: silent inspections are discarded to avoid screen jumping."""
        with self._lock:
            if self._pending_inspections and (time.time() - self._last_push_time >= idle_seconds):
                self._pending_inspections.clear()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending_inspections:
            return

        items = list(self._pending_inspections)
        self._pending_inspections.clear()

        if len(items) == 1:
            it = items[0]
            block = TuiRenderer.format_tool_call(it.tool, it.target, it.timestamp)
            self.on_flush(block)
            return

        # Consolidated inspection summary
        count = len(items)
        ts = items[-1].timestamp
        # Extract brief sample of target names
        samples = []
        for it in items:
            name = Path(it.target).name if ("\\" in it.target or "/" in it.target) else it.target
            if name and name not in samples:
                samples.append(name)
        sample_str = ", ".join(samples[:4])
        if len(samples) > 4:
            sample_str += f", +{len(samples) - 4} more"

        header = (
            f"{Theme.TOOL}{Theme.BOLD}{Theme.BULLET} Inspected {count} files/locations"
            f"{Theme.RESET} {Theme.MUTED}({sample_str}){Theme.RESET}"
        )
        sub = f"  {Theme.MUTED}{Theme.CONNECTOR} {count} operations completed · {ts}{Theme.RESET}"
        self.on_flush(f"{header}\n{sub}")


# -----------------------------------------------------------------------------
# 4. Presentation Layer: TuiRenderer (Minimalist, Sober Typography)
# -----------------------------------------------------------------------------
class TuiRenderer:
    """Pure presentation renderer: clean boxes, sober badges, zero emoji clutter."""

    TOOL_LABELS: dict[str, str] = {
        "read_file": "Read",
        "edit_file": "Edit",
        "write_file": "Write",
        "exec_command": "Bash",
        "process_manager": "Process",
        "search_files": "Search",
        "list_dir": "List",
        "file_ops": "FileOps",
        "bridge_status": "Status",
        "codex_delegate": "Task",
        "codex_task_run": "Task",
        "system_info": "SysInfo",
    }

    @classmethod
    def render_box(cls, lines: list[tuple[str, int]], border_color: str = Theme.ACCENT, title: str = "") -> str:
        width = Theme.term_width()
        inner = width - 4
        if title:
            vis_title = f" {title} "
            pad_len = max(0, width - 3 - len(vis_title))
            top = f"{border_color}{Theme.CORNER_TL}{Theme.HLINE}{Theme.BOLD}{vis_title}{Theme.RESET}{border_color}{Theme.HLINE * pad_len}{Theme.CORNER_TR}{Theme.RESET}"
        else:
            top = f"{border_color}{Theme.CORNER_TL}{Theme.HLINE * (width - 2)}{Theme.CORNER_TR}{Theme.RESET}"
        body = []
        for styled, vlen in lines:
            pad = max(0, inner - vlen)
            body.append(f"{border_color}{Theme.VLINE}{Theme.RESET} {styled}{' ' * pad} {border_color}{Theme.VLINE}{Theme.RESET}")
        bot = f"{border_color}{Theme.CORNER_BL}{Theme.HLINE * (width - 2)}{Theme.CORNER_BR}{Theme.RESET}"
        return "\n".join([top, *body, bot])

    @classmethod
    def format_tool_call(cls, tool_name: str, summary: str, ts: str, details: str = "") -> str:
        width = Theme.term_width()
        inner = width - 4
        s_strip = (summary or "").strip()
        clean_sum = s_strip
        if s_strip.startswith("ps:"):
            label = "PowerShell"
            clean_sum = s_strip[3:].strip()
        elif s_strip.startswith("cmd:"):
            label = "CMD"
            clean_sum = s_strip[4:].strip()
        elif s_strip.startswith("py:"):
            label = "Python"
            clean_sum = s_strip[3:].strip()
        elif tool_name in cls.TOOL_LABELS:
            label = cls.TOOL_LABELS[tool_name]
        else:
            label = tool_name

        clean_sum = clean_sum.replace("\r", " ").replace("\n", " ").strip()
        if len(clean_sum) > inner - 16:
            clean_sum = clean_sum[: inner - 19] + "..."

        arg_part = f"({Theme.TEXT}{clean_sum}{Theme.TOOL})" if clean_sum else "()"
        header = f"{Theme.TOOL}{Theme.BOLD}{Theme.BULLET} {label}{arg_part}{Theme.RESET}"

        if details:
            dlines = details.splitlines()
            head_detail = dlines[0] if dlines else f"Executed at {ts}"
            out_lines = [header, f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.TEXT}{head_detail}{Theme.MUTED} · {ts}{Theme.RESET}"]
            for dl in dlines[1:20]:
                if dl.startswith("+"):
                    out_lines.append(f"      {Theme.SUCCESS}+ {dl[1:].strip()[:inner-8]}{Theme.RESET}")
                elif dl.startswith("-"):
                    out_lines.append(f"      {Theme.ERROR}- {dl[1:].strip()[:inner-8]}{Theme.RESET}")
                else:
                    out_lines.append(f"      {Theme.MUTED}{dl[:inner-6]}{Theme.RESET}")
            return "\n".join(out_lines)

        return f"{header}\n  {Theme.MUTED}{Theme.CONNECTOR} {ts}{Theme.RESET}"

    @classmethod
    def format_agent_message(cls, ts: str, txt: str) -> str:
        width = Theme.term_width()
        inner = width - 4
        clean = txt.strip()
        low = clean.lower()
        is_question = "?" in clean or "¿" in clean or "duda" in low or "prefieres" in low or "confirm" in low

        wrapped_lines: list[str] = []
        for para in clean.splitlines():
            p = para.strip()
            if not p:
                wrapped_lines.append("")
            else:
                wrapped_lines.extend(textwrap.wrap(p, width=inner - 2) or [""])

        if is_question:
            box_rows: list[tuple[str, int]] = []
            for wl in wrapped_lines:
                box_rows.append((f"{Theme.TEXT}{wl}{Theme.RESET}", len(wl)))
            box_rows.append(("", 0))
            hint = "> Type your reply below and press ENTER"
            box_rows.append((f"{Theme.WARN}{hint}{Theme.RESET}", len(hint)))
            return cls.render_box(box_rows, border_color=Theme.INFO, title=f"Question · {ts}")

        body = "\n".join(f"  {Theme.TEXT}{wl}{Theme.RESET}" for wl in wrapped_lines)
        return f"{Theme.ACCENT}{Theme.BOLD}[Agent · {ts}]{Theme.RESET}\n{body}"

    @classmethod
    def format_user_message(cls, ts: str, txt: str, delivered_tool: str | None, priority: str = "normal") -> str:
        prio_tag = f" [{priority.upper()}]" if priority in {"high", "urgent"} else ""
        if delivered_tool:
            sub = f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[delivered in {delivered_tool} · {ts}]{Theme.RESET}"
        else:
            sub = f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.WARN}[queued for next agent turn · {ts}]{Theme.RESET}"
        return f"{Theme.PROMPT}{Theme.BOLD}{Theme.PROMPT_SYMBOL}{prio_tag}{Theme.RESET} {Theme.TEXT}{txt}{Theme.RESET}\n{sub}"


# Top-level alias for compatibility with test suites & callers
def format_tool_call(tool_name: str, summary: str, ts: str, details: str = "") -> str:
    return TuiRenderer.format_tool_call(tool_name, summary, ts, details)


# -----------------------------------------------------------------------------
# 5. Interaction Layer: TuiLineEditor (Cursor Navigation & History)
# -----------------------------------------------------------------------------
class TuiLineEditor:
    """
    Robust interactive console line editor with:
    - Cursor navigation: Left, Right, Home, End, Delete, Backspace.
    - Command history: Up, Down with active draft buffer preservation.
    - Anti-flicker protected redraws that preserve cursor position.
    - Clipboard burst & multiline paste protection.
    """

    def __init__(self, prompt_fn: Callable[[], str]) -> None:
        self.prompt_fn = prompt_fn
        self.input_chars: list[str] = []
        self.cursor_pos: int = 0
        self.history: list[str] = []
        self.history_idx: int = -1
        self.saved_draft: str = ""
        self.lock = threading.Lock()

    def get_text(self) -> str:
        with self.lock:
            return "".join(self.input_chars)

    def is_empty(self) -> bool:
        with self.lock:
            return len(self.input_chars) == 0

    def redraw_locked(self) -> None:
        buf = "".join(self.input_chars)
        sys.stdout.write(f"\r\033[K{self.prompt_fn()}{Theme.TEXT}{buf}{Theme.RESET}")
        dist = len(self.input_chars) - self.cursor_pos
        if dist > 0:
            sys.stdout.write(f"\033[{dist}D")
        sys.stdout.flush()

    def read_line(self, stop_event: threading.Event, on_flush_deferred: Callable[[], None]) -> str | None:
        try:
            sys.stdout.write(self.prompt_fn()); sys.stdout.flush(); line = input()
            res = line.strip()
            if res:
                if not self.history or self.history[-1] != res:
                    self.history.append(res)
                    if len(self.history) > 100:
                        self.history.pop(0)
            return res
        except (KeyboardInterrupt, EOFError):
            return None


# -----------------------------------------------------------------------------
# 6. Application Controller: MessengerController
# -----------------------------------------------------------------------------
class MessengerController:
    """Coordinates polling, anti-flicker tool buffering, and command execution."""

    def __init__(self, client: BridgeClient | None = None) -> None:
        self.client = client or _default_client
        self.stop_event = threading.Event()
        self.editor = TuiLineEditor(self.prompt_str)
        self.consolidator = ActivityConsolidator(self._on_consolidated_action)
        self.deferred_tool_blocks: list[str] = []

        self.seen_ids: set[str] = set()
        self.seen_action_ids: set[str] = set()
        self.delivered_ids: set[str] = set()
        self.last_rendered_msg_sig: str = ""
        self.last_rendered_act_sig: str = ""
        self.last_tool_sig: str = ""

    def prompt_str(self) -> str:
        return f"{Theme.PROMPT}{Theme.BOLD}{Theme.PROMPT_SYMBOL} {Theme.RESET}"

    def safe_print_above_prompt(self, block: str, *, is_tool_action: bool = False) -> None:
        with self.editor.lock:
            sys.stdout.write(f"\n{block}\n")
            sys.stdout.flush()

    def flush_deferred(self) -> None:
        if not self.deferred_tool_blocks:
            return
        combined = "\n\n".join(self.deferred_tool_blocks)
        self.deferred_tool_blocks.clear()
        sys.stdout.write(f"\r\033[K{combined}\n\n")
        sys.stdout.flush()

    def _on_consolidated_action(self, block: str) -> None:
        self.safe_print_above_prompt(block, is_tool_action=True)

    def _is_noisy_action(self, tool_name: str, summary: str, details: str = "") -> bool:
        if details:
            return False
        if tool_name in {"read_file", "search_files", "list_dir", "file_ops"}:
            return True
        low_s = (summary or "").strip().lower()
        if low_s.startswith(("inbox:", "jobs:", "sysinfo:", "py:", "ps:")):
            return True
        if low_s.endswith(("agent_outbox.txt", "bridge_chat.txt", "live_inbox.json", "run_task.py")):
            return True
        return False

    def poll_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                state = self.client.get_state()
                # Process action log
                for act in state.get("action_log") or []:
                    aid = act.get("id", "")
                    if aid and aid not in self.seen_action_ids:
                        self.seen_action_ids.add(aid)
                        t_name = act.get("tool", "tool")
                        t_sum = act.get("summary", "")
                        if self._is_noisy_action(t_name, t_sum, act.get("details", "")):
                            continue
                        act_sig = f"{t_name}|{t_sum.strip()}"
                        if act_sig == self.last_rendered_act_sig:
                            continue
                        self.last_rendered_act_sig = act_sig
                        t_ts = (act.get("timestamp") or "")[11:19] or time.strftime("%H:%M:%S")
                        self.consolidator.push(t_name, t_sum, t_ts, act.get("details", ""))

                # Check idle flush for consolidated reading actions
                self.consolidator.check_idle_flush(idle_seconds=1.2)

                # Agent telemetry updated silently (no duplicate printing)
                tele = state.get("agent_telemetry") or {}
                cur_tool = tele.get("last_tool") or ""
                cur_upd = tele.get("updated_at") or ""
                self.last_tool_sig = f"{cur_tool}|{cur_upd}"

                # Process messages
                for m in state.get("messages") or []:
                    mid = m.get("id", "")
                    sender = m.get("sender", "user")
                    txt = m.get("text", "")
                    ts = (m.get("created_at") or "")[11:19]
                    if mid not in self.seen_ids:
                        self.seen_ids.add(mid)
                        msg_sig = f"{sender}|{txt.strip()}"
                        if msg_sig == self.last_rendered_msg_sig:
                            continue
                        self.last_rendered_msg_sig = msg_sig
                        # Flush any pending reading operations before displaying a new message
                        self.consolidator.flush()

                        if sender == "agent":
                            try:
                                import winsound
                                winsound.Beep(880, 140)
                                winsound.Beep(1175, 180)
                            except Exception:
                                pass
                            self.safe_print_above_prompt(TuiRenderer.format_agent_message(ts, txt), is_tool_action=False)
                        else:
                            if m.get("read_by_agent"):
                                self.delivered_ids.add(mid)
                            self.safe_print_above_prompt(
                                TuiRenderer.format_user_message(
                                    ts,
                                    txt,
                                    (m.get("delivered_in_tool") or "tool") if m.get("read_by_agent") else None,
                                    m.get("priority", "normal"),
                                ),
                                is_tool_action=False,
                            )
                    elif sender == "user" and m.get("read_by_agent") and mid not in self.delivered_ids:
                        self.delivered_ids.add(mid)
                        tool_d = m.get("delivered_in_tool") or "tool"
                        self.safe_print_above_prompt(
                            f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[delivered to agent in {tool_d} · {ts}: \"{txt[:60]}\"]{Theme.RESET}",
                            is_tool_action=False,
                        )
            except Exception:
                pass
            self.stop_event.wait(0.65)

    def print_welcome_banner(self) -> None:
        rows = [
            (f"{Theme.ACCENT}{Theme.BOLD}Codex ChatGPT Bridge TUI{Theme.RESET} {Theme.MUTED}v0.2.0 (Consolidated & Anti-Spam){Theme.RESET}", 64),
            (f"{Theme.SUCCESS}[ONLINE]{Theme.RESET} {Theme.TEXT}127.0.0.1:8766 · Bi-directional live channel{Theme.RESET}", 54),
            (f"{Theme.MUTED}Protected buffer: arrows <-/->, Home/End, Delete & history ^/v without flicker.{Theme.RESET}", 79),
            (f"{Theme.MUTED}Commands: {Theme.WARN}/help{Theme.MUTED} · {Theme.WARN}/status{Theme.MUTED} · {Theme.WARN}/jobs{Theme.MUTED} · {Theme.WARN}/actions{Theme.MUTED} · {Theme.WARN}/urgent <txt>{Theme.MUTED} · {Theme.WARN}/clear{Theme.MUTED} · {Theme.WARN}/exit{Theme.RESET}", 80),
        ]
        print(TuiRenderer.render_box(rows, border_color=Theme.ACCENT))

    def run(self) -> None:
        enable_windows_ansi()
        self.print_welcome_banner()

        # Seed initial state: mark all pre-existing actions and messages as seen
        # so historical messages NEVER trigger sound alerts or fake "new message" prints
        init_state = self.client.get_state()
        for act in init_state.get("action_log") or []:
            if act.get("id"):
                self.seen_action_ids.add(act["id"])

        init_msgs = init_state.get("messages") or []
        for m in init_msgs:
            mid = m.get("id", "")
            if mid:
                self.seen_ids.add(mid)
            if m.get("read_by_agent"):
                self.delivered_ids.add(mid)

        # Show concise recent context (maximum 3 recent messages, not ancient history)
        if init_msgs:
            recent = init_msgs[-3:]
            width = Theme.term_width()
            print(f"\n{Theme.MUTED}--- Recent history ({len(recent)}) {'-' * max(4, width - 26)}{Theme.RESET}")
            for m in recent:
                mid = m.get("id", "")
                sender = m.get("sender", "user")
                txt = m.get("text", "")
                ts = (m.get("created_at") or "")[11:19]
                if sender == "agent":
                    print(TuiRenderer.format_agent_message(ts, txt) + "\n")
                else:
                    if m.get("read_by_agent"):
                        print(TuiRenderer.format_user_message(ts, txt, m.get("delivered_in_tool") or "tool", m.get("priority", "normal")) + "\n")
                    else:
                        print(TuiRenderer.format_user_message(ts, txt, None, m.get("priority", "normal")) + "\n")

        tele = init_state.get("agent_telemetry") or {}
        if tele.get("last_tool"):
            self.last_tool_sig = f"{tele.get('last_tool')}|{tele.get('updated_at')}"
            ts_init = (tele.get("updated_at") or "")[11:19] or time.strftime("%H:%M:%S")
            print(TuiRenderer.format_tool_call(tele.get("last_tool", "idle"), tele.get("last_summary", ""), ts_init) + "\n")

        poller = threading.Thread(target=self.poll_worker, daemon=True)
        poller.start()

        # Main command loop
        try:
            while True:
                line = self.editor.read_line(self.stop_event, self.flush_deferred)
                if line is None:
                    break
                txt = line.strip()
                if not txt:
                    continue

                low = txt.lower()
                if low in {"/salir", "/exit", "exit", "quit"}:
                    print(f"  {Theme.MUTED}{Theme.CONNECTOR} Closing TUI session...{Theme.RESET}")
                    break

                if low in {"/limpiar", "/clear"}:
                    res = self.client.clear_inbox()
                    self.seen_ids.clear()
                    self.delivered_ids.clear()
                    sys.stdout.write("\033[2J\033[H")
                    sys.stdout.flush()
                    self.print_welcome_banner()
                    print(f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[ok · screen and history cleared ({res.get('cleared', 0)} messages)]{Theme.RESET}\n")
                    continue

                if low in {"/estado", "/status"}:
                    st = self.client.get_state()
                    t = st.get("agent_telemetry") or {}
                    unread_cnt = st.get("unread_user_messages", 0)
                    tot_msgs = st.get("total_messages", 0)
                    last_t = t.get("last_tool", "idle")
                    last_s = (t.get("last_summary", "") or "")[:60]
                    upd_t = (t.get("updated_at") or "")[11:19] or "recent"

                    status_rows = [
                        (f"{Theme.SUCCESS}[STATUS]{Theme.RESET} {Theme.TEXT}HTTP Gateway active (127.0.0.1:8766){Theme.RESET}", 48),
                        (f"{Theme.TOOL}Unread queue:{Theme.RESET} {Theme.WARN}{unread_cnt} pending messages{Theme.RESET}", 38),
                        (f"{Theme.TOOL}Total history:{Theme.RESET} {Theme.TEXT}{tot_msgs} messages recorded{Theme.RESET}", 36),
                        (f"{Theme.ACCENT}Last tool:{Theme.RESET} {Theme.TEXT}{last_t} {Theme.MUTED}({last_s}){Theme.RESET}", 48),
                        (f"{Theme.MUTED}Updated at: {upd_t}{Theme.RESET}", 26),
                    ]
                    print(TuiRenderer.render_box(status_rows, border_color=Theme.TOOL, title="Bridge Status") + "\n")
                    continue

                if low in {"/acciones", "/actions", "/log"}:
                    st = self.client.get_state()
                    acts = st.get("action_log") or []
                    print(f"  {Theme.MUTED}{Theme.CONNECTOR} Recent agent tool actions ({min(15, len(acts))}):{Theme.RESET}")
                    for a in acts[-15:]:
                        ats = (a.get("timestamp") or "")[11:19]
                        lbl = TuiRenderer.TOOL_LABELS.get(a.get("tool", ""), a.get("tool", ""))
                        print(f"     {Theme.TOOL}{Theme.BULLET} {lbl}{Theme.RESET}({a.get('summary', '')[:68]}) {Theme.MUTED}· {ats}{Theme.RESET}")
                    print()
                    continue

                if low in {"/jobs", "/procesos"}:
                    st = self.client.get_state()
                    tele_j = st.get("agent_telemetry") or {}
                    last_act = tele_j.get("last_tool") or "idle"
                    print(f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.TOOL}Background Process Runner Monitor:{Theme.RESET}")
                    print(f"     {Theme.TEXT}Agent activity:{Theme.RESET} {Theme.SUCCESS}[active]{Theme.RESET} {last_act} ({tele_j.get('last_summary', '')[:60]})")
                    print(f"     {Theme.MUTED}Use /urgent or bridge tools to schedule and inspect execution.{Theme.RESET}\n")
                    continue

                if low in {"/ayuda", "/help"}:
                    help_rows = [
                        (f"{Theme.WARN}<message>{Theme.RESET}         Inject live instruction into active Agent loop", 58),
                        (f"{Theme.WARN}/urgent <txt>{Theme.RESET}     Send message with URGENT priority", 45),
                        (f"{Theme.WARN}/high <txt>{Theme.RESET}       Send message with HIGH priority", 43),
                        (f"{Theme.WARN}/status{Theme.RESET}           Show live connection and queue telemetry", 49),
                        (f"{Theme.WARN}/actions{Theme.RESET}          List recent tools executed by the Agent", 48),
                        (f"{Theme.WARN}/jobs{Theme.RESET}             Inspect background jobs and runners", 44),
                        (f"{Theme.WARN}/gui{Theme.RESET}              Launch floating desktop window in parallel", 51),
                        (f"{Theme.WARN}/clear{Theme.RESET}            Clear terminal and reset message inbox", 47),
                        (f"{Theme.WARN}/exit{Theme.RESET}             Exit interactive session", 33),
                        (f"{Theme.MUTED}Keys: {Theme.TEXT}<-/->{Theme.MUTED} cursor · {Theme.TEXT}^/v{Theme.MUTED} history · {Theme.TEXT}Home/End{Theme.MUTED} · {Theme.TEXT}ESC{Theme.MUTED} clear{Theme.RESET}", 64),
                    ]
                    print(TuiRenderer.render_box(help_rows, border_color=Theme.TOOL, title="TUI Commands") + "\n")
                    continue

                if low == "/gui":
                    threading.Thread(target=launch_gui, daemon=True).start()
                    print(f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[ok · GUI floating window opened in parallel]{Theme.RESET}\n")
                    continue

                # Normal message sending
                prio = "normal"
                if low.startswith("/urgente ") or low.startswith("/urgent "):
                    prio = "urgent"
                    txt = txt.split(" ", 1)[1].strip()
                elif low.startswith("/alta ") or low.startswith("/high "):
                    prio = "high"
                    txt = txt.split(" ", 1)[1].strip()

                if not txt:
                    continue

                res = self.client.send_user_message(txt, priority=prio)
                msg = res.get("message") or {}
                mid = msg.get("id", "")
                if mid:
                    self.seen_ids.add(mid)
                ts = (msg.get("created_at") or "")[11:19] or time.strftime("%H:%M:%S")
                with self.editor.lock:
                    print(f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.WARN}[queued ({prio.upper()}) at {ts} · will be consumed on next agent tool call]{Theme.RESET}\n")

        except KeyboardInterrupt:
            print(f"\n  {Theme.MUTED}{Theme.CONNECTOR} Interactive TUI terminated.{Theme.RESET}")
        finally:
            self.stop_event.set()


def launch_terminal_chat() -> None:
    controller = MessengerController()
    controller.run()


# -----------------------------------------------------------------------------
# 7. GUI Mode (Optional Desktop Window)
# -----------------------------------------------------------------------------
def launch_gui() -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Codex ChatGPT Bridge // Live Messenger")
    root.geometry("760x520")
    root.configure(bg="#0b0e14")

    hdr_frame = tk.Frame(root, bg="#131822", bd=0, highlightthickness=1, highlightbackground="#eeaf46")
    hdr_frame.pack(fill=tk.X, padx=12, pady=(12, 6))

    title_lbl = tk.Label(
        hdr_frame,
        text="CODEX BRIDGE // LIVE CHANNEL (NON-INTERRUPTING)",
        bg="#131822",
        fg="#f5b942",
        font=("Consolas", 11, "bold"),
        anchor="w",
        padx=10,
        pady=6,
    )
    title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

    status_lbl = tk.Label(
        hdr_frame,
        text="[ONLINE 8766]",
        bg="#131822",
        fg="#6ee7b7",
        font=("Consolas", 10, "bold"),
        padx=10,
    )
    status_lbl.pack(side=tk.RIGHT)

    tele_lbl = tk.Label(
        root,
        text="Agent activity: waiting...",
        bg="#0b0e14",
        fg="#7dd3fc",
        font=("Consolas", 9),
        anchor="w",
        padx=14,
    )
    tele_lbl.pack(fill=tk.X)

    hist_frame = tk.Frame(root, bg="#11151c", bd=1, relief=tk.SOLID)
    hist_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

    text_box = tk.Text(
        hist_frame,
        bg="#0f131a",
        fg="#e2e8f0",
        font=("Consolas", 10),
        wrap=tk.WORD,
        state=tk.DISABLED,
        bd=0,
        padx=10,
        pady=8,
    )
    scrollbar = ttk.Scrollbar(hist_frame, command=text_box.yview)
    text_box.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    text_box.tag_config("user_pending", foreground="#fcd34d", font=("Consolas", 10, "bold"))
    text_box.tag_config("user_delivered", foreground="#86efac", font=("Consolas", 10))
    text_box.tag_config("agent_reply", foreground="#7dd3fc", font=("Consolas", 10, "bold"))
    text_box.tag_config("meta", foreground="#94a3b8", font=("Consolas", 9))

    btm_frame = tk.Frame(root, bg="#0b0e14")
    btm_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

    prio_var = tk.StringVar(value="normal")
    prio_menu = tk.OptionMenu(btm_frame, prio_var, "normal", "high", "urgent")
    prio_menu.config(bg="#1e293b", fg="#f8fafc", font=("Consolas", 9, "bold"), bd=0, highlightthickness=0)
    prio_menu.pack(side=tk.LEFT, padx=(0, 8))

    entry = tk.Entry(
        btm_frame,
        bg="#161d2a",
        fg="#f8fafc",
        insertbackground="#f5b942",
        font=("Consolas", 11),
        bd=1,
        relief=tk.SOLID,
    )
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 8))
    entry.focus_set()

    def refresh_feed() -> None:
        state = api_request("/inbox")
        tele = state.get("agent_telemetry") or {}
        last_tool = tele.get("last_tool") or "idle"
        last_sum = tele.get("last_summary") or ""
        unread_cnt = state.get("unread_user_messages", 0)
        tele_lbl.config(
            text=f"Agent Tool: [{last_tool}] {last_sum}   |   Queued unread: {unread_cnt}"
        )

        msgs = state.get("messages") or []
        text_box.config(state=tk.NORMAL)
        text_box.delete("1.0", tk.END)
        if not msgs:
            text_box.insert(
                tk.END,
                "Type any message below and press ENTER.\n"
                "The agent receives it on its next tool call without stopping work.\n",
                "meta",
            )
        for m in msgs:
            sender = m.get("sender", "user")
            txt = m.get("text", "")
            prio = m.get("priority", "normal").upper()
            ts = (m.get("created_at") or "")[11:19]
            if sender == "agent":
                text_box.insert(tk.END, f"[{ts}] AGENT: {txt}\n\n", "agent_reply")
            else:
                if m.get("read_by_agent"):
                    tool_deliv = m.get("delivered_in_tool") or "tool"
                    text_box.insert(
                        tk.END,
                        f"[{ts}] [delivered in {tool_deliv}]: {txt}\n\n",
                        "user_delivered",
                    )
                else:
                    text_box.insert(
                        tk.END,
                        f"[{ts}] [queued ({prio})]: {txt}\n\n",
                        "user_pending",
                    )
        text_box.see(tk.END)
        text_box.config(state=tk.DISABLED)

    def send_msg(event=None) -> None:
        txt = entry.get().strip()
        if not txt:
            return
        entry.delete(0, tk.END)
        api_request("/inbox/send", method="POST", payload={"text": txt, "priority": prio_var.get()})
        refresh_feed()

    entry.bind("<Return>", send_msg)

    send_btn = tk.Button(
        btm_frame,
        text="SEND ⏎",
        command=send_msg,
        bg="#f5b942",
        fg="#0b0e14",
        activebackground="#fcd34d",
        font=("Consolas", 10, "bold"),
        padx=14,
        pady=5,
        bd=0,
    )
    send_btn.pack(side=tk.RIGHT)

    def poll_loop() -> None:
        try:
            refresh_feed()
        except Exception:
            pass
        root.after(1000, poll_loop)

    poll_loop()
    root.mainloop()


# -----------------------------------------------------------------------------
# 8. CLI Entrypoint
# -----------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Bridge Interactive TUI & Live Messenger.")
    parser.add_argument("message", nargs="*", help="Message text to send (omit to launch Interactive TUI).")
    parser.add_argument("--chat", "-c", action="store_true", help="Launch interactive terminal TUI.")
    parser.add_argument("--gui", "-g", action="store_true", help="Launch desktop Tkinter GUI.")
    parser.add_argument("--urgent", action="store_true", help="Mark message as urgent priority.")
    parser.add_argument("--high", action="store_true", help="Mark message as high priority.")
    parser.add_argument("--status", action="store_true", help="Show live inbox & agent activity status.")
    parser.add_argument("--clear", action="store_true", help="Clear live inbox history.")
    args = parser.parse_args()

    if args.clear:
        res = api_request("/inbox/clear", method="POST")
        print(f"[Bridge Inbox] History cleared ({res.get('cleared', 0)} messages).")
        return

    if args.status:
        state = api_request("/inbox")
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return

    if args.gui:
        launch_gui()
        return

    if args.chat:
        launch_terminal_chat()
        return

    if args.message:
        text = " ".join(args.message).strip()
        prio = "urgent" if args.urgent else ("high" if args.high else "normal")
        res = api_request("/inbox/send", method="POST", payload={"text": text, "priority": prio})
        msg = res.get("message") or {}
        print(f"[Bridge Live Inbox] Enqueued ({prio.upper()} - ID: {msg.get('id')}):")
        print(f"  | \"{text}\"")
        return

    launch_terminal_chat()


if __name__ == "__main__":
    main()
