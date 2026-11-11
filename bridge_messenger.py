#!/usr/bin/env python3
"""
Codex ChatGPT Bridge - High-Craft Interactive TUI Messenger (v0.3.0)
Allows real-time two-way communication between the user and the active AI Agent mid-loop
without pressing Stop or interrupting its tool execution workflow.

Architecture: AOT, SOLID, SSOT, DRY, Separation of Concerns (SoC), Strong Typing.
- Theme: Single Source of Truth for ANSI styling, sober typography, and responsive geometry.
- AnsiTextMetrics: Cell-exact Unicode/ANSI width calculation, truncation, and wrapping
  so no rendered line ever exceeds terminal columns or overlaps on resize.
- FeedEvent & FeedStore: Canonical SSOT event store enabling full-screen deterministic
  reflow whenever the terminal window is resized or view/filter modes change.
- ActivityConsolidator: Groups consecutive read-only actions (read_file, search_files, list_dir)
  into a consolidated dynamic summary to prevent terminal spam.
- TuiRenderer: Minimalist, technical rendering with auto-wrapping framed boxes.
- TuiLineEditor: True framed multi-line input box with software cursor, slash-command
  autocomplete (Tab), priority cycling (Ctrl+P), view toggling (F2), and history (Up/Down).
- BridgeClient: Decoupled HTTP transport with atomic offline fallback.
- MessengerController: Command dispatcher, resize watcher, and synchronized event loop.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Literal

if os.name == "nt":
    import msvcrt
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BRIDGE_URL: Final[str] = "http://127.0.0.1:8766"


def _get_fallback_inbox() -> Path:
    primary = Path.home() / ".bridge" / "live_inbox.json"
    if primary.exists():
        return primary
    legacy = Path.home() / ".codex-chatgpt-bridge" / "live_inbox.json"
    if legacy.exists():
        return legacy
    return primary


FALLBACK_INBOX: Final[Path] = _get_fallback_inbox()

ViewMode = Literal["all", "chat", "tools"]
PriorityLevel = Literal["normal", "high", "urgent"]
EventKind = Literal["banner", "divider", "agent", "user", "tool", "system"]

_ANSI_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# -----------------------------------------------------------------------------
# 1. SSOT: Theme, ANSI Metrics & Responsive Geometry
# -----------------------------------------------------------------------------
class AnsiTextMetrics:
    """Single Source of Truth for terminal cell width, ANSI stripping, and wrapping."""

    @staticmethod
    def strip_ansi(text: str) -> str:
        if not text:
            return ""
        return _ANSI_ESCAPE_RE.sub("", text)

    @staticmethod
    def char_width(ch: str) -> int:
        if not ch or ch in ("\r", "\n", "\0"):
            return 0
        code = ord(ch)
        if code < 32 or (0x7F <= code < 0xA0):
            return 0
        if unicodedata.combining(ch):
            return 0
        eaw = unicodedata.east_asian_width(ch)
        return 2 if eaw in ("W", "F") else 1

    @classmethod
    def visible_width(cls, text: str) -> int:
        plain = cls.strip_ansi(text)
        return sum(cls.char_width(c) for c in plain)

    @classmethod
    def truncate_plain(cls, text: str, max_cols: int, ellipsis: str = "...") -> str:
        if max_cols <= 0:
            return ""
        clean = text.replace("\r", " ").replace("\n", " ").replace("\t", "  ")
        if cls.visible_width(clean) <= max_cols:
            return clean
        ell_w = cls.visible_width(ellipsis)
        if max_cols <= ell_w:
            out: list[str] = []
            w = 0
            for ch in clean:
                cw = cls.char_width(ch)
                if w + cw > max_cols:
                    break
                out.append(ch)
                w += cw
            return "".join(out)
        target = max_cols - ell_w
        out = []
        w = 0
        for ch in clean:
            cw = cls.char_width(ch)
            if w + cw > target:
                break
            out.append(ch)
            w += cw
        return "".join(out) + ellipsis

    @classmethod
    def wrap_plain(cls, text: str, max_cols: int) -> list[str]:
        """Wraps plain text into lines guaranteed to have visible_width <= max_cols."""
        width = max(8, max_cols)
        expanded = text.replace("\r", "").replace("\t", "  ")
        paragraphs = expanded.split("\n")
        result: list[str] = []

        for para in paragraphs:
            if not para:
                result.append("")
                continue
            words = para.split(" ")
            current_line = ""
            current_w = 0

            for word in words:
                word_w = cls.visible_width(word)
                if current_w == 0:
                    if word_w <= width:
                        current_line = word
                        current_w = word_w
                    else:
                        # Hard-wrap long token character by character
                        chunk: list[str] = []
                        cw_acc = 0
                        for ch in word:
                            chw = cls.char_width(ch)
                            if cw_acc + chw > width and chunk:
                                result.append("".join(chunk))
                                chunk = [ch]
                                cw_acc = chw
                            else:
                                chunk.append(ch)
                                cw_acc += chw
                        current_line = "".join(chunk)
                        current_w = cw_acc
                elif current_w + 1 + word_w <= width:
                    current_line += " " + word
                    current_w += 1 + word_w
                else:
                    result.append(current_line)
                    if word_w <= width:
                        current_line = word
                        current_w = word_w
                    else:
                        chunk = []
                        cw_acc = 0
                        for ch in word:
                            chw = cls.char_width(ch)
                            if cw_acc + chw > width and chunk:
                                result.append("".join(chunk))
                                chunk = [ch]
                                cw_acc = chw
                            else:
                                chunk.append(ch)
                                cw_acc += chw
                        current_line = "".join(chunk)
                        current_w = cw_acc
            result.append(current_line)

        return result or [""]


class Theme:
    """Single Source of Truth for colors, styles, minimalist symbols, and terminal dimensions."""

    RESET: Final[str] = "\033[0m"
    BOLD: Final[str] = "\033[1m"
    DIM: Final[str] = "\033[2m"
    REVERSE: Final[str] = "\033[7m"
    UNREVERSE: Final[str] = "\033[27m"

    # Synchronized output & cursor control sequences
    SYNC_START: Final[str] = "\033[?2026h"
    SYNC_END: Final[str] = "\033[?2026l"
    CLEAR_SCREEN: Final[str] = "\033[2J\033[3J\033[H"
    CLEAR_EOL: Final[str] = "\033[K"
    HIDE_CURSOR: Final[str] = "\033[?25l"
    SHOW_CURSOR: Final[str] = "\033[?25h"

    # Minimalist, sober palette (Claude & Codex developer aesthetics)
    ACCENT: Final[str] = "\033[38;5;209m"     # Terracotta / Coral
    PROMPT: Final[str] = "\033[38;5;215m"     # Warm amber
    TOOL: Final[str] = "\033[38;5;117m"       # Sky cyan
    SUCCESS: Final[str] = "\033[38;5;114m"    # Soft emerald (+ additions, ok)
    WARN: Final[str] = "\033[38;5;222m"       # Soft gold (queued, highlights)
    INFO: Final[str] = "\033[38;5;141m"       # Lavender (questions, thinking)
    ERROR: Final[str] = "\033[38;5;203m"      # Soft crimson (- deletions, errors)
    MUTED: Final[str] = "\033[38;5;244m"      # Slate gray (sub-lines, timestamps)
    DARK: Final[str] = "\033[38;5;238m"       # Frame gray
    TEXT: Final[str] = "\033[97m"             # Bright white text

    # Clean, sober symbols (Zero kitsch emojis)
    BULLET: Final[str] = "*"
    CONNECTOR: Final[str] = "|"
    PROMPT_SYMBOL: Final[str] = ">"

    # Box-drawing elements
    CORNER_TL: Final[str] = "╭"
    CORNER_TR: Final[str] = "╮"
    CORNER_BL: Final[str] = "╰"
    CORNER_BR: Final[str] = "╯"
    TEE_L: Final[str] = "├"
    TEE_R: Final[str] = "┤"
    HLINE: Final[str] = "─"
    VLINE: Final[str] = "│"

    @classmethod
    def term_size(cls) -> tuple[int, int]:
        try:
            sz = shutil.get_terminal_size((88, 24))
            return max(36, sz.columns), max(10, sz.lines)
        except Exception:
            return 88, 24

    @classmethod
    def term_width(cls, cols_override: int | None = None) -> int:
        """
        Computes safe content width that NEVER exceeds `cols - 1` so the terminal
        emulator never auto-wraps box borders onto the next line during resize.
        """
        cols = cols_override if cols_override is not None else cls.term_size()[0]
        return max(34, min(cols - 1, 140))


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
    """Handles communication with the Bridge HTTP daemon with offline disk fallback."""

    def __init__(self, base_url: str = BRIDGE_URL, fallback_path: Path = FALLBACK_INBOX) -> None:
        self.base_url = base_url
        self.fallback_path = fallback_path
        self.last_online: bool = True

    def request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
            self.last_online = True
            return parsed
        except Exception:
            self.last_online = False
            src_dir = str(Path(__file__).resolve().parent / "src")
            if src_dir not in sys.path:
                sys.path.insert(0, src_dir)
            from codex_chatgpt_bridge.live_inbox import LiveInboxStore

            store = LiveInboxStore(self.fallback_path)
            if path == "/inbox/send" and payload:
                msg = store.send_user_message(payload.get("text", ""), priority=payload.get("priority", "normal"))
                return {"status": "queued_offline", "message": msg, "inbox": store.get_state()}
            if path == "/inbox/clear":
                return store.clear()
            return store.get_state()

    def get_state(self, limit: int = 50) -> dict[str, Any]:
        return self.request(f"/inbox?limit={limit}")

    def send_user_message(self, text: str, priority: str = "normal") -> dict[str, Any]:
        return self.request("/inbox/send", method="POST", payload={"text": text, "priority": priority})

    def clear_inbox(self) -> dict[str, Any]:
        return self.request("/inbox/clear", method="POST")


# Module-level convenience wrapper for compatibility
_default_client = BridgeClient()


def api_request(path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return _default_client.request(path, method=method, payload=payload)


# -----------------------------------------------------------------------------
# 3. Domain Logic: ActivityConsolidator & Canonical FeedEvent Store (SSOT)
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class InspectionItem:
    tool: str
    target: str
    timestamp: str


@dataclass
class FeedEvent:
    """
    Canonical representation of a displayable item in the TUI feed (SSOT).
    Allows deterministic re-rendering at any terminal width when resizing.
    """
    kind: EventKind
    timestamp: str = ""
    text: str = ""
    title: str = ""
    tool_name: str = ""
    summary: str = ""
    details: str = ""
    delivered_tool: str | None = None
    priority: str = "normal"
    inspection_items: list[InspectionItem] = field(default_factory=list)

    def matches_view(self, mode: ViewMode, filter_query: str = "") -> bool:
        if mode == "chat" and self.kind == "tool":
            return False
        if mode == "tools" and self.kind in ("agent", "user"):
            return False
        if filter_query and self.kind not in ("banner", "divider"):
            q = filter_query.lower()
            haystack = " ".join([
                self.text,
                self.title,
                self.tool_name,
                self.summary,
                self.details,
                " ".join(it.target for it in self.inspection_items),
            ]).lower()
            if q not in haystack:
                return False
        return True

    def render(self, width: int | None = None) -> str:
        w = width if width is not None else Theme.term_width()
        if self.kind == "banner":
            return TuiRenderer.render_welcome_banner(width=w)
        if self.kind == "divider":
            label = f"--- {self.title} "
            dashes = "-" * max(4, w - AnsiTextMetrics.visible_width(label))
            return f"{Theme.MUTED}{label}{dashes}{Theme.RESET}"
        if self.kind == "agent":
            return TuiRenderer.format_agent_message(self.timestamp, self.text, width=w)
        if self.kind == "user":
            return TuiRenderer.format_user_message(
                self.timestamp,
                self.text,
                self.delivered_tool,
                self.priority,
                width=w,
            )
        if self.kind == "tool":
            if self.inspection_items:
                return TuiRenderer.format_consolidated_inspections(self.inspection_items, width=w)
            return TuiRenderer.format_tool_call(
                self.tool_name,
                self.summary,
                self.timestamp,
                self.details,
                width=w,
            )
        return self.text


class ActivityConsolidator:
    """
    Prevents terminal flooding when the agent runs back-to-back read/search operations.
    Consecutive inspections (read_file, search_files, list_dir) are consolidated
    into a single clean summary line instead of 20 separate blocks.
    """

    INSPECTION_TOOLS: Final[set[str]] = {"read_file", "search_files", "list_dir", "file_ops"}

    def __init__(
        self,
        on_flush: Callable[[str], None],
        on_flush_event: Callable[[FeedEvent], None] | None = None,
    ) -> None:
        self.on_flush = on_flush
        self.on_flush_event = on_flush_event
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
            ev = FeedEvent(
                kind="tool",
                timestamp=timestamp,
                tool_name=tool_name,
                summary=summary or "",
                details=details or "",
            )
            if self.on_flush_event is not None:
                self.on_flush_event(ev)
            else:
                self.on_flush(ev.render())

    def check_idle_flush(self, idle_seconds: float = 1.0) -> None:
        """Called by poller loop: flush accumulated inspections into a single summary block."""
        with self._lock:
            if self._pending_inspections and (time.time() - self._last_push_time >= idle_seconds):
                self._flush_locked()

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
            ev = FeedEvent(
                kind="tool",
                timestamp=it.timestamp,
                tool_name=it.tool,
                summary=it.target,
            )
            if self.on_flush_event is not None:
                self.on_flush_event(ev)
            else:
                self.on_flush(ev.render())
            return

        ev = FeedEvent(
            kind="tool",
            timestamp=items[-1].timestamp,
            tool_name="inspection_group",
            inspection_items=items,
        )
        if self.on_flush_event is not None:
            self.on_flush_event(ev)
        else:
            self.on_flush(ev.render())


# -----------------------------------------------------------------------------
# 4. Presentation Layer: TuiRenderer (Minimalist, Resize-Safe Typography)
# -----------------------------------------------------------------------------
class TuiRenderer:
    """Pure presentation renderer: clean boxes, cell-exact width clamping, zero emoji clutter."""

    TOOL_LABELS: Final[dict[str, str]] = {
        "read_file": "Read",
        "edit_file": "Edit",
        "write_file": "Write",
        "exec_command": "Bash",
        "process_manager": "Process",
        "search_files": "Search",
        "list_dir": "List",
        "file_ops": "FileOps",
        "bridge_status": "Status",
        "bridge_task_run": "Task",
        "bridge_session_start": "Session",
        "bridge_session_continue": "Session",
        "bridge_session_list": "Sessions",
        "bridge_session_status": "Session",
        "codex_task_run": "Task",
        "system_info": "SysInfo",
    }

    @classmethod
    def render_box(
        cls,
        lines: list[tuple[str, int]],
        border_color: str = Theme.ACCENT,
        title: str = "",
        width: int | None = None,
        footer: str = "",
    ) -> str:
        w = width if width is not None else Theme.term_width()
        w = max(34, w)
        inner = max(10, w - 4)

        if title:
            clean_title = AnsiTextMetrics.truncate_plain(title, max(6, w - 6))
            vis_title = f" {clean_title} "
            pad_len = max(0, w - 3 - AnsiTextMetrics.visible_width(vis_title))
            top = (
                f"{border_color}{Theme.CORNER_TL}{Theme.HLINE}{Theme.BOLD}{vis_title}"
                f"{Theme.RESET}{border_color}{Theme.HLINE * pad_len}{Theme.CORNER_TR}{Theme.RESET}"
            )
        else:
            top = f"{border_color}{Theme.CORNER_TL}{Theme.HLINE * (w - 2)}{Theme.CORNER_TR}{Theme.RESET}"

        body: list[str] = []
        for styled, vlen in lines:
            actual_vlen = AnsiTextMetrics.visible_width(styled) if vlen < 0 else vlen
            if actual_vlen <= inner:
                pad = max(0, inner - actual_vlen)
                body.append(
                    f"{border_color}{Theme.VLINE}{Theme.RESET} {styled}{' ' * pad} "
                    f"{border_color}{Theme.VLINE}{Theme.RESET}"
                )
            else:
                # Never allow a box line to exceed inner width and wrap the right border
                plain = AnsiTextMetrics.strip_ansi(styled)
                for chunk in AnsiTextMetrics.wrap_plain(plain, inner):
                    c_w = AnsiTextMetrics.visible_width(chunk)
                    pad = max(0, inner - c_w)
                    body.append(
                        f"{border_color}{Theme.VLINE}{Theme.RESET} {Theme.TEXT}{chunk}{Theme.RESET}{' ' * pad} "
                        f"{border_color}{Theme.VLINE}{Theme.RESET}"
                    )

        if footer:
            clean_foot = AnsiTextMetrics.truncate_plain(footer, max(6, w - 6))
            vis_foot = f" {clean_foot} "
            pad_len = max(0, w - 3 - AnsiTextMetrics.visible_width(vis_foot))
            bot = (
                f"{border_color}{Theme.CORNER_BL}{Theme.HLINE}{Theme.MUTED}{vis_foot}"
                f"{Theme.RESET}{border_color}{Theme.HLINE * pad_len}{Theme.CORNER_BR}{Theme.RESET}"
            )
        else:
            bot = f"{border_color}{Theme.CORNER_BL}{Theme.HLINE * (w - 2)}{Theme.CORNER_BR}{Theme.RESET}"

        return "\n".join([top, *body, bot])

    @classmethod
    def render_welcome_banner(cls, width: int | None = None) -> str:
        w = width if width is not None else Theme.term_width()
        rows = [
            (f"{Theme.ACCENT}{Theme.BOLD}Bridge Live Messenger TUI{Theme.RESET} {Theme.MUTED}v0.5.0 (Agnostic Parallel Engine & Framed UX){Theme.RESET}", -1),
            (f"{Theme.SUCCESS}[ONLINE]{Theme.RESET} {Theme.TEXT}127.0.0.1:8766 · Parallel Worker Pool (32+ threads) · Bi-directional Live Queue{Theme.RESET}", -1),
            (f"{Theme.MUTED}Keys: <-/->, Home/End, Up/Dn history, Tab complete, Ctrl+P prio, F1 Help, F2 View, F3 Jobs, F4 Status{Theme.RESET}", -1),
            (f"{Theme.MUTED}Cmds: {Theme.WARN}/help{Theme.MUTED} · {Theme.WARN}/status{Theme.MUTED} · {Theme.WARN}/jobs{Theme.MUTED} · {Theme.WARN}/mode{Theme.MUTED} · {Theme.WARN}/filter{Theme.MUTED} · {Theme.WARN}/search{Theme.MUTED} · {Theme.WARN}/diff{Theme.MUTED} · {Theme.WARN}/export{Theme.MUTED} · {Theme.WARN}/clear{Theme.RESET}", -1),
        ]
        return cls.render_box(rows, border_color=Theme.ACCENT, title="Bridge Console", width=w)

    @classmethod
    def format_consolidated_inspections(cls, items: list[InspectionItem], width: int | None = None) -> str:
        w = width if width is not None else Theme.term_width()
        inner = max(20, w - 4)
        count = len(items)
        ts = items[-1].timestamp if items else ""
        samples: list[str] = []
        for it in items:
            name = Path(it.target).name if ("\\" in it.target or "/" in it.target) else it.target
            if name and name not in samples:
                samples.append(name)
        sample_str = ", ".join(samples[:4])
        if len(samples) > 4:
            sample_str += f", +{len(samples) - 4} more"
        sample_str = AnsiTextMetrics.truncate_plain(sample_str, max(12, inner - 32))

        header = (
            f"{Theme.TOOL}{Theme.BOLD}{Theme.BULLET} Inspected {count} files/locations"
            f"{Theme.RESET} {Theme.MUTED}({sample_str}){Theme.RESET}"
        )
        sub = f"  {Theme.MUTED}{Theme.CONNECTOR} {count} operations completed · {ts}{Theme.RESET}"
        return f"{header}\n{sub}"

    @classmethod
    def format_tool_call(
        cls,
        tool_name: str,
        summary: str | None,
        ts: str,
        details: str = "",
        width: int | None = None,
    ) -> str:
        w = width if width is not None else Theme.term_width()
        inner = max(20, w - 4)
        s_strip = (summary or "").strip()
        low_s = s_strip.lower()
        clean_sum = s_strip
        if low_s.startswith("ps:"):
            label = "PowerShell"
            clean_sum = s_strip[3:].strip()
        elif low_s.startswith("powershell:"):
            label = "PowerShell"
            clean_sum = s_strip[11:].strip()
        elif low_s.startswith("cmd:"):
            label = "CMD"
            clean_sum = s_strip[4:].strip()
        elif low_s.startswith("py:"):
            label = "Python"
            clean_sum = s_strip[3:].strip()
        elif low_s.startswith("python:"):
            label = "Python"
            clean_sum = s_strip[7:].strip()
        elif low_s.startswith("sh:"):
            label = "Shell"
            clean_sum = s_strip[3:].strip()
        elif low_s.startswith("bg:"):
            label = "Background"
            clean_sum = s_strip[3:].strip()
        elif tool_name in cls.TOOL_LABELS:
            label = cls.TOOL_LABELS[tool_name]
        else:
            label = tool_name

        max_sum_cols = max(10, inner - len(label) - 6)
        clean_sum = AnsiTextMetrics.truncate_plain(clean_sum, max_sum_cols)

        arg_part = f"({Theme.TEXT}{clean_sum}{Theme.TOOL})" if clean_sum else "()"
        header = f"{Theme.TOOL}{Theme.BOLD}{Theme.BULLET} {label}{arg_part}{Theme.RESET}"

        if details:
            dlines = details.splitlines()
            head_detail = dlines[0] if dlines else f"Executed at {ts}"
            head_detail_trunc = AnsiTextMetrics.truncate_plain(head_detail, max(12, inner - 16))
            if head_detail.startswith(("Exit 0", "Created", "Replaced")):
                head_color = Theme.SUCCESS
            elif head_detail.startswith("Exit "):
                head_color = Theme.ERROR
            elif head_detail.startswith("@@"):
                head_color = Theme.ACCENT
            else:
                head_color = Theme.TEXT
            out_lines = [header, f"  {Theme.MUTED}{Theme.CONNECTOR} {head_color}{head_detail_trunc}{Theme.MUTED} · {ts}{Theme.RESET}"]
            line_budget = max(10, inner - 8)
            for dl in dlines[1:22]:
                if dl.startswith("+"):
                    body_txt = AnsiTextMetrics.truncate_plain(dl[1:].strip(), line_budget)
                    out_lines.append(f"      {Theme.SUCCESS}+ {body_txt}{Theme.RESET}")
                elif dl.startswith("-"):
                    body_txt = AnsiTextMetrics.truncate_plain(dl[1:].strip(), line_budget)
                    out_lines.append(f"      {Theme.ERROR}- {body_txt}{Theme.RESET}")
                elif dl.startswith("$ "):
                    body_txt = AnsiTextMetrics.truncate_plain(dl[2:].strip(), line_budget)
                    out_lines.append(f"      {Theme.PROMPT}$ {body_txt}{Theme.RESET}")
                elif dl.startswith("> "):
                    body_txt = AnsiTextMetrics.truncate_plain(dl[2:], line_budget)
                    out_lines.append(f"      {Theme.DARK}{Theme.VLINE}{Theme.RESET} {Theme.TEXT}{body_txt}{Theme.RESET}")
                elif dl.startswith("! "):
                    body_txt = AnsiTextMetrics.truncate_plain(dl[2:], line_budget)
                    out_lines.append(f"      {Theme.ERROR}! {body_txt}{Theme.RESET}")
                else:
                    body_txt = AnsiTextMetrics.truncate_plain(dl, line_budget + 2)
                    out_lines.append(f"      {Theme.MUTED}{body_txt}{Theme.RESET}")
            return "\n".join(out_lines)

        return f"{header}\n  {Theme.MUTED}{Theme.CONNECTOR} {ts}{Theme.RESET}"

    @classmethod
    def format_agent_message(cls, ts: str, txt: str, width: int | None = None) -> str:
        w = width if width is not None else Theme.term_width()
        inner = max(16, w - 4)
        clean = txt.strip()
        low = clean.lower()
        is_question = "?" in clean or "¿" in clean or "duda" in low or "prefieres" in low or "confirm" in low

        wrapped_lines: list[str] = []
        for para in clean.splitlines():
            p = para.strip()
            if not p:
                wrapped_lines.append("")
            else:
                wrapped_lines.extend(AnsiTextMetrics.wrap_plain(p, inner - 2))

        if is_question:
            box_rows: list[tuple[str, int]] = []
            for wl in wrapped_lines:
                box_rows.append((f"{Theme.TEXT}{wl}{Theme.RESET}", AnsiTextMetrics.visible_width(wl)))
            box_rows.append(("", 0))
            hint = AnsiTextMetrics.truncate_plain("> Type your reply below and press ENTER", inner - 2)
            box_rows.append((f"{Theme.WARN}{hint}{Theme.RESET}", AnsiTextMetrics.visible_width(hint)))
            return cls.render_box(box_rows, border_color=Theme.INFO, title=f"Question · {ts}", width=w)

        body = "\n".join(f"  {Theme.TEXT}{wl}{Theme.RESET}" for wl in wrapped_lines)
        return f"{Theme.ACCENT}{Theme.BOLD}[Agent · {ts}]{Theme.RESET}\n{body}"

    @classmethod
    def format_user_message(
        cls,
        ts: str,
        txt: str,
        delivered_tool: str | None,
        priority: str = "normal",
        width: int | None = None,
    ) -> str:
        w = width if width is not None else Theme.term_width()
        inner = max(16, w - 6)
        prio_tag = f" [{priority.upper()}]" if priority in {"high", "urgent"} else ""
        wrapped = AnsiTextMetrics.wrap_plain(txt.strip(), inner)
        first = wrapped[0] if wrapped else ""
        rest = "\n".join(f"  {Theme.TEXT}{line}{Theme.RESET}" for line in wrapped[1:])
        head = f"{Theme.PROMPT}{Theme.BOLD}{Theme.PROMPT_SYMBOL}{prio_tag}{Theme.RESET} {Theme.TEXT}{first}{Theme.RESET}"
        if rest:
            head = f"{head}\n{rest}"
        if delivered_tool:
            sub = f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[delivered in {delivered_tool} · {ts}]{Theme.RESET}"
        else:
            sub = f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.WARN}[queued for next agent turn · {ts}]{Theme.RESET}"
        return f"{head}\n{sub}"


# Top-level alias for compatibility with test suites & callers
def format_tool_call(tool_name: str, summary: str | None, ts: str, details: str = "") -> str:
    return TuiRenderer.format_tool_call(tool_name, summary, ts, details)


# -----------------------------------------------------------------------------
# 5. Interaction Layer: True Framed Input Box & Non-Blocking Editor
# -----------------------------------------------------------------------------
SLASH_COMMANDS: Final[list[tuple[str, str]]] = [
    ("/help", "Show all TUI commands and keyboard shortcuts"),
    ("/status", "Display live bridge gateway & queue telemetry"),
    ("/mode", "Switch view filter: /mode [all|chat|tools] (or press F2)"),
    ("/filter", "Filter feed by keyword: /filter <text> or /filter off"),
    ("/search", "Search across all recorded messages & tool actions"),
    ("/prio", "Set default message priority: /prio [normal|high|urgent] (Ctrl+P)"),
    ("/actions", "List recent tool invocations executed by the Agent"),
    ("/diff", "Show recent file diffs and command outputs"),
    ("/jobs", "Inspect active background runner processes"),
    ("/export", "Export session transcript to Markdown (/export [file.md])"),
    ("/urgent ", "Send a single message with URGENT priority"),
    ("/high ", "Send a single message with HIGH priority"),
    ("/gui", "Open parallel desktop Tkinter window"),
    ("/clear", "Clear terminal screen and reset inbox history"),
    ("/exit", "Close the interactive TUI session"),
]


class TuiLineEditor:
    """
    True Framed Multi-Line Input Box & Interactive Line Editor:
    - Renders an anchored box (`╭─ ... ─╮ / │ > ... │ / ╰─ ... ─╯`) at the bottom of the TUI.
    - Dynamically wraps long input across multiple inner rows without ever exceeding terminal width.
    - Uses an inverse-video software cursor inside the box so multi-line wrapping never triggers
      terminal auto-scroll glitches.
    - Supports Left/Right, Ctrl+Left/Right, Home/End, Delete, Backspace, Ctrl+W, Ctrl+U, ESC,
      Up/Down history, Tab slash-command autocomplete, F1 (/help), F2 (/mode), Ctrl+P (/prio).
    - Automatically detects terminal resize (`(cols, rows)`) and triggers full-screen reflow.
    """

    SPINNER_FRAMES: Final[tuple[str, ...]] = ("-", "\\", "|", "/")

    def __init__(
        self,
        prompt_fn: Callable[[], str],
        on_resize: Callable[[int, int], None] | None = None,
        status_provider: Callable[[], dict[str, Any]] | None = None,
        on_hotkey: Callable[[str], None] | None = None,
    ) -> None:
        self.prompt_fn = prompt_fn
        self.on_resize = on_resize
        self.status_provider = status_provider
        self.on_hotkey = on_hotkey

        self.input_chars: list[str] = []
        self.cursor_pos: int = 0
        self.history: list[str] = []
        self.history_idx: int = -1
        self.saved_draft: str = ""
        self.lock = threading.RLock()

        self._dock_line_count: int = 0
        self._dock_active: bool = False
        self._spinner_idx: int = 0

    def get_text(self) -> str:
        with self.lock:
            return "".join(self.input_chars)

    def is_empty(self) -> bool:
        with self.lock:
            return len(self.input_chars) == 0

    def _matching_slash_hint(self, text: str) -> tuple[str, str] | None:
        if not text.startswith("/"):
            return None
        low = text.lower()
        for cmd, desc in SLASH_COMMANDS:
            if cmd.startswith(low) or low.startswith(cmd.strip()):
                return cmd, desc
        return None

    def _autocomplete_locked(self) -> bool:
        text = "".join(self.input_chars)
        if not text.startswith("/"):
            return False
        low = text.lower()
        matches = [cmd for cmd, _ in SLASH_COMMANDS if cmd.startswith(low)]
        if not matches:
            return False
        chosen = matches[0]
        if not chosen.endswith(" ") and chosen in ("/mode", "/filter", "/search", "/prio", "/export"):
            chosen += " "
        self.input_chars = list(chosen)
        self.cursor_pos = len(self.input_chars)
        return True

    def build_input_box_lines(self, width: int | None = None) -> list[str]:
        """
        Builds the framed input box lines guaranteed to fit strictly within `width` cells.
        """
        w = width if width is not None else Theme.term_width()
        w = max(34, w)
        inner = max(12, w - 4)

        meta = self.status_provider() if self.status_provider else {}
        prio = str(meta.get("priority", "normal")).upper()
        view = str(meta.get("view_mode", "all")).upper()
        filt = str(meta.get("filter_query", "") or "")
        unread = int(meta.get("unread_count", 0) or 0)
        agent_tool = str(meta.get("agent_tool", "idle") or "idle")
        agent_sum = str(meta.get("agent_summary", "") or "")

        if prio == "URGENT":
            border_color = Theme.ERROR
            prio_badge = f"{Theme.ERROR}{Theme.BOLD}[URGENT]{Theme.RESET}"
        elif prio == "HIGH":
            border_color = Theme.WARN
            prio_badge = f"{Theme.WARN}{Theme.BOLD}[HIGH]{Theme.RESET}"
        else:
            border_color = Theme.PROMPT
            prio_badge = f"{Theme.SUCCESS}[NORMAL]{Theme.RESET}"

        spin = self.SPINNER_FRAMES[self._spinner_idx % len(self.SPINNER_FRAMES)]
        agent_part = f"{agent_tool}"
        if agent_sum:
            short_s = AnsiTextMetrics.truncate_plain(agent_sum, max(8, inner // 3))
            agent_part = f"{agent_tool}({short_s})"
        filt_part = f" · Filter:{filt}" if filt else ""
        active_w = int(meta.get("active_workers", 0) or 0)
        max_w = int(meta.get("max_workers", 32) or 32)
        run_jobs = int(meta.get("running_jobs", 0) or 0)
        turn_c = int(meta.get("turn_calls", 0) or 0)
        total_c = int(meta.get("total_calls", 0) or 0)
        char_cnt = len(self.input_chars)
        conn_part = "" if meta.get("server_online", True) else " · RECONNECTING"

        raw_header = (
            f" Input [{prio}] · Calls:{turn_c}/{total_c} · W:{active_w}/{max_w} · "
            f"J:{run_jobs} · Q:{unread} · View:{view}{filt_part}{conn_part} · "
            f"[{spin}] {agent_part} · {char_cnt}c "
        )
        clean_header = AnsiTextMetrics.truncate_plain(raw_header, max(10, w - 4))
        h_vis = AnsiTextMetrics.visible_width(clean_header)
        top_dashes = max(0, w - 3 - h_vis)
        top_line = (
            f"{border_color}{Theme.CORNER_TL}{Theme.HLINE}{Theme.BOLD}{clean_header}"
            f"{Theme.RESET}{border_color}{Theme.HLINE * top_dashes}{Theme.CORNER_TR}{Theme.RESET}"
        )

        # Build wrapped input lines with software cursor at cursor_pos
        prefix = "> "
        prefix_w = 2
        avail_w = max(8, inner - prefix_w)

        chars = list(self.input_chars)
        cpos = max(0, min(self.cursor_pos, len(chars)))

        # Segment characters into wrapped rows of width <= avail_w
        rows_segments: list[list[tuple[int, str]]] = [[]]
        row_w = 0
        for idx, ch in enumerate(chars):
            cw = max(1, AnsiTextMetrics.char_width(ch))
            if row_w + cw > avail_w and rows_segments[-1]:
                rows_segments.append([])
                row_w = 0
            rows_segments[-1].append((idx, ch))
            row_w += cw

        # If cursor is at the end of buffer and the last row is full, wrap cursor to next row
        cursor_at_end = (cpos == len(chars))
        if cursor_at_end and row_w >= avail_w:
            rows_segments.append([])

        # Keep at most 4 visible input rows inside the box (auto-scrolling to the cursor row)
        max_box_rows = 4
        cursor_row_idx = len(rows_segments) - 1
        if not cursor_at_end:
            for r_i, seg in enumerate(rows_segments):
                if any(idx == cpos for idx, _ in seg):
                    cursor_row_idx = r_i
                    break
        if len(rows_segments) > max_box_rows:
            start_r = max(0, min(cursor_row_idx - max_box_rows + 1, len(rows_segments) - max_box_rows))
            visible_rows = rows_segments[start_r : start_r + max_box_rows]
        else:
            visible_rows = rows_segments

        body_lines: list[str] = []
        for r_i, seg in enumerate(visible_rows):
            lead = f"{Theme.PROMPT}{Theme.BOLD}{prefix}{Theme.RESET}" if r_i == 0 else "  "
            rendered_chars: list[str] = []
            vis_w = prefix_w
            for idx, ch in seg:
                cw = max(1, AnsiTextMetrics.char_width(ch))
                if idx == cpos:
                    rendered_chars.append(f"{Theme.REVERSE}{Theme.TEXT}{ch}{Theme.UNREVERSE}{Theme.RESET}")
                else:
                    rendered_chars.append(f"{Theme.TEXT}{ch}{Theme.RESET}")
                vis_w += cw

            if cursor_at_end and (seg is rows_segments[-1]):
                rendered_chars.append(f"{Theme.REVERSE}{Theme.PROMPT} {Theme.UNREVERSE}{Theme.RESET}")
                vis_w += 1

            pad = max(0, inner - vis_w)
            body_lines.append(
                f"{border_color}{Theme.VLINE}{Theme.RESET} {lead}{''.join(rendered_chars)}{' ' * pad} "
                f"{border_color}{Theme.VLINE}{Theme.RESET}"
            )

        # Optional contextual slash-command hint row inside the input box
        buf_text = "".join(chars)
        hint_pair = self._matching_slash_hint(buf_text)
        if hint_pair is not None:
            cmd_name, cmd_desc = hint_pair
            hint_plain = AnsiTextMetrics.truncate_plain(f"-> {cmd_name} : {cmd_desc} [Tab]", inner)
            h_pad = max(0, inner - AnsiTextMetrics.visible_width(hint_plain))
            body_lines.append(
                f"{border_color}{Theme.VLINE}{Theme.RESET} {Theme.INFO}{hint_plain}{Theme.RESET}{' ' * h_pad} "
                f"{border_color}{Theme.VLINE}{Theme.RESET}"
            )

        raw_footer = " [Enter] Send · [Tab] Complete · [Up/Dn] History · [Ctrl+P] Prio · [F2] View · [F3] Jobs · [F4] Status "
        clean_footer = AnsiTextMetrics.truncate_plain(raw_footer, max(10, w - 4))
        f_vis = AnsiTextMetrics.visible_width(clean_footer)
        bot_dashes = max(0, w - 3 - f_vis)
        bot_line = (
            f"{border_color}{Theme.CORNER_BL}{Theme.HLINE}{Theme.MUTED}{clean_footer}"
            f"{Theme.RESET}{border_color}{Theme.HLINE * bot_dashes}{Theme.CORNER_BR}{Theme.RESET}"
        )

        return [top_line, *body_lines, bot_line]

    def clear_dock_locked(self) -> None:
        """Erases the currently rendered framed input box from the bottom of the terminal."""
        if not self._dock_active or self._dock_line_count <= 0:
            return
        moves = self._dock_line_count - 1
        seq = ["\r" + Theme.CLEAR_EOL]
        for _ in range(moves):
            seq.append("\033[1A\r" + Theme.CLEAR_EOL)
        sys.stdout.write("".join(seq))
        sys.stdout.flush()
        self._dock_line_count = 0
        self._dock_active = False

    def redraw_locked(self, advance_spinner: bool = False) -> None:
        """Atomically redraws the framed input box at the bottom of the viewport."""
        if advance_spinner:
            self._spinner_idx = (self._spinner_idx + 1) % len(self.SPINNER_FRAMES)
        if not self._dock_active and not sys.stdin.isatty():
            # Non-TTY fallback compatibility
            buf = "".join(self.input_chars)
            sys.stdout.write(f"\r{Theme.CLEAR_EOL}{self.prompt_fn()}{Theme.TEXT}{buf}{Theme.RESET}")
            dist = len(self.input_chars) - self.cursor_pos
            if dist > 0:
                sys.stdout.write(f"\033[{dist}D")
            sys.stdout.flush()
            return

        box_lines = self.build_input_box_lines()
        out: list[str] = [Theme.SYNC_START, Theme.HIDE_CURSOR]
        if self._dock_active and self._dock_line_count > 0:
            moves = self._dock_line_count - 1
            if moves > 0:
                out.append(f"\033[{moves}A")
            out.append("\r")
            for i, line in enumerate(box_lines):
                out.append(f"{line}{Theme.CLEAR_EOL}")
                if i < len(box_lines) - 1:
                    out.append("\n")
            # If the box shrunk (e.g., from 3 inner rows to 1), clear leftover rows below
            extra = self._dock_line_count - len(box_lines)
            if extra > 0:
                for _ in range(extra):
                    out.append(f"\n{Theme.CLEAR_EOL}")
                out.append(f"\033[{extra}A")
        else:
            for i, line in enumerate(box_lines):
                out.append(f"\r{line}{Theme.CLEAR_EOL}")
                if i < len(box_lines) - 1:
                    out.append("\n")

        out.append(Theme.SYNC_END)
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._dock_line_count = len(box_lines)
        self._dock_active = True

    def _move_word_left_locked(self) -> None:
        while self.cursor_pos > 0 and self.input_chars[self.cursor_pos - 1] == " ":
            self.cursor_pos -= 1
        while self.cursor_pos > 0 and self.input_chars[self.cursor_pos - 1] != " ":
            self.cursor_pos -= 1

    def _move_word_right_locked(self) -> None:
        n = len(self.input_chars)
        while self.cursor_pos < n and self.input_chars[self.cursor_pos] != " ":
            self.cursor_pos += 1
        while self.cursor_pos < n and self.input_chars[self.cursor_pos] == " ":
            self.cursor_pos += 1

    def _delete_word_left_locked(self) -> None:
        if self.cursor_pos == 0:
            return
        old = self.cursor_pos
        self._move_word_left_locked()
        del self.input_chars[self.cursor_pos : old]

    def read_line(self, stop_event: threading.Event, on_flush_deferred: Callable[[], None]) -> str | None:
        # Fallback for non-interactive pipes / automated subprocess tests
        if os.name != "nt" or not sys.stdin.isatty():
            try:
                sys.stdout.write(self.prompt_fn())
                sys.stdout.flush()
                line = sys.stdin.readline()
                if not line:
                    return None
                res = line.strip()
                if res and (not self.history or self.history[-1] != res):
                    self.history.append(res)
                return res
            except (KeyboardInterrupt, EOFError):
                return None

        with self.lock:
            self.input_chars.clear()
            self.cursor_pos = 0
            self.history_idx = -1
            self.saved_draft = ""
            on_flush_deferred()
            self.redraw_locked()

        last_size = Theme.term_size()
        pending_resize_size: tuple[int, int] | None = None
        pending_resize_ts: float = 0.0
        last_spin_ts = time.time()

        try:
            while not stop_event.is_set():
                now = time.time()
                cur_size = Theme.term_size()
                if cur_size != last_size:
                    last_size = cur_size
                    pending_resize_size = cur_size
                    pending_resize_ts = now

                if pending_resize_size is not None and (now - pending_resize_ts >= 0.06):
                    new_cols, new_rows = pending_resize_size
                    pending_resize_size = None
                    if self.on_resize is not None:
                        self.on_resize(new_cols, new_rows)
                    else:
                        with self.lock:
                            self.redraw_locked()

                if now - last_spin_ts >= 0.45:
                    last_spin_ts = now
                    with self.lock:
                        if self._dock_active:
                            self.redraw_locked(advance_spinner=True)

                if not msvcrt.kbhit():
                    time.sleep(0.025)
                    continue

                ch = msvcrt.getwch()

                # Extended key prefix (arrows, Home, End, Delete, F1, F2, etc.)
                if ch in ("\x00", "\xe0"):
                    ext = msvcrt.getwch() if msvcrt.kbhit() else ""
                    with self.lock:
                        if ext == "K":  # Left arrow
                            if self.cursor_pos > 0:
                                self.cursor_pos -= 1
                                self.redraw_locked()
                        elif ext == "M":  # Right arrow
                            if self.cursor_pos < len(self.input_chars):
                                self.cursor_pos += 1
                                self.redraw_locked()
                        elif ext == "s":  # Ctrl+Left
                            self._move_word_left_locked()
                            self.redraw_locked()
                        elif ext == "t":  # Ctrl+Right
                            self._move_word_right_locked()
                            self.redraw_locked()
                        elif ext == "G":  # Home
                            self.cursor_pos = 0
                            self.redraw_locked()
                        elif ext == "O":  # End
                            self.cursor_pos = len(self.input_chars)
                            self.redraw_locked()
                        elif ext == "S":  # Delete
                            if self.cursor_pos < len(self.input_chars):
                                self.input_chars.pop(self.cursor_pos)
                                self.redraw_locked()
                        elif ext == "H":  # Up arrow (History previous)
                            if self.history:
                                if self.history_idx == -1:
                                    self.saved_draft = "".join(self.input_chars)
                                    self.history_idx = len(self.history) - 1
                                elif self.history_idx > 0:
                                    self.history_idx -= 1
                                self.input_chars = list(self.history[self.history_idx])
                                self.cursor_pos = len(self.input_chars)
                                self.redraw_locked()
                        elif ext == "P":  # Down arrow (History next)
                            if self.history_idx != -1:
                                if self.history_idx < len(self.history) - 1:
                                    self.history_idx += 1
                                    self.input_chars = list(self.history[self.history_idx])
                                else:
                                    self.history_idx = -1
                                    self.input_chars = list(self.saved_draft)
                                self.cursor_pos = len(self.input_chars)
                                self.redraw_locked()
                        elif ext == ";":  # F1 -> /help
                            if self.on_hotkey:
                                self.on_hotkey("F1")
                        elif ext == "<":  # F2 -> cycle view mode
                            if self.on_hotkey:
                                self.on_hotkey("F2")
                        elif ext == "=":  # F3 -> /jobs & parallel workers
                            if self.on_hotkey:
                                self.on_hotkey("F3")
                        elif ext == ">":  # F4 -> /status telemetry
                            if self.on_hotkey:
                                self.on_hotkey("F4")
                    continue

                if ch == "\x03":  # Ctrl+C
                    with self.lock:
                        self.clear_dock_locked()
                        sys.stdout.write(Theme.SHOW_CURSOR)
                        sys.stdout.flush()
                    raise KeyboardInterrupt

                if ch in ("\r", "\n"):  # Enter
                    with self.lock:
                        res = "".join(self.input_chars).strip()
                        self.input_chars.clear()
                        self.cursor_pos = 0
                        self.history_idx = -1
                        self.saved_draft = ""
                        if res:
                            if not self.history or self.history[-1] != res:
                                self.history.append(res)
                                if len(self.history) > 100:
                                    self.history.pop(0)
                            self.clear_dock_locked()
                            return res
                        else:
                            self.redraw_locked()
                    continue

                if ch == "\t":  # Tab -> autocomplete slash command
                    with self.lock:
                        if self._autocomplete_locked():
                            self.redraw_locked()
                    continue

                if ch == "\x10":  # Ctrl+P -> cycle priority
                    if self.on_hotkey:
                        self.on_hotkey("CTRL_P")
                    with self.lock:
                        self.redraw_locked()
                    continue

                if ch == "\x1b":  # ESC -> clear current input draft
                    with self.lock:
                        self.input_chars.clear()
                        self.cursor_pos = 0
                        self.history_idx = -1
                        self.redraw_locked()
                    continue

                if ch == "\x15":  # Ctrl+U -> clear line
                    with self.lock:
                        self.input_chars.clear()
                        self.cursor_pos = 0
                        self.redraw_locked()
                    continue

                if ch == "\x17":  # Ctrl+W -> delete word left
                    with self.lock:
                        self._delete_word_left_locked()
                        self.redraw_locked()
                    continue

                if ch in ("\x08", "\x7f"):  # Backspace
                    with self.lock:
                        if self.cursor_pos > 0:
                            self.input_chars.pop(self.cursor_pos - 1)
                            self.cursor_pos -= 1
                            self.redraw_locked()
                    continue

                if ord(ch) >= 32:
                    with self.lock:
                        self.input_chars.insert(self.cursor_pos, ch)
                        self.cursor_pos += 1
                        # Drain any fast clipboard paste burst before redrawing once
                        while msvcrt.kbhit():
                            nxt = msvcrt.getwch()
                            if nxt in ("\x00", "\xe0"):
                                if msvcrt.kbhit():
                                    msvcrt.getwch()
                                break
                            if nxt in ("\r", "\n"):
                                self.input_chars.insert(self.cursor_pos, " ")
                                self.cursor_pos += 1
                            elif ord(nxt) >= 32:
                                self.input_chars.insert(self.cursor_pos, nxt)
                                self.cursor_pos += 1
                            else:
                                break
                        self.redraw_locked()
        finally:
            if sys.stdin.isatty():
                sys.stdout.write(Theme.SHOW_CURSOR)
                sys.stdout.flush()
        return None


# -----------------------------------------------------------------------------
# 6. Application Controller: MessengerController (Resize Reflow & Commands)
# -----------------------------------------------------------------------------
class MessengerController:
    """Coordinates polling, SSOT feed storage, resize reflow, and command execution."""

    _POLL_INTERVAL_S: Final[float] = 0.60
    _POLL_BACKOFF_SCHEDULE_S: Final[tuple[float, ...]] = (1.0, 2.0, 4.0)

    def __init__(self, client: BridgeClient | None = None) -> None:
        self.client = client or _default_client
        self.stop_event = threading.Event()
        self.server_online: bool = True
        self.poll_failures: int = 0
        self.view_mode: ViewMode = "all"
        self.default_priority: PriorityLevel = "normal"
        self.filter_query: str = ""
        self.unread_count: int = 0
        self.active_agent_tool: str = "idle"
        self.active_agent_summary: str = ""
        self.active_workers: int = 0
        self.max_workers: int = 32
        self.running_jobs_count: int = 0
        self.turn_calls: int = 0
        self.total_calls: int = 0

        self.feed_events: list[FeedEvent] = []
        self.deferred_tool_blocks: list[str] = []

        self.seen_ids: set[str] = set()
        self.seen_action_ids: set[str] = set()
        self.delivered_ids: set[str] = set()
        self.last_rendered_msg_sig: str = ""
        self.last_rendered_act_sig: str = ""
        self.last_tool_sig: str = ""

        self.editor = TuiLineEditor(
            self.prompt_str,
            on_resize=self.handle_terminal_resize,
            status_provider=self._editor_status_snapshot,
            on_hotkey=self._handle_hotkey,
        )
        self.consolidator = ActivityConsolidator(
            self._on_consolidated_action,
            on_flush_event=self._on_consolidated_event,
        )

    def prompt_str(self) -> str:
        return f"{Theme.PROMPT}{Theme.BOLD}{Theme.PROMPT_SYMBOL} {Theme.RESET}"

    def _editor_status_snapshot(self) -> dict[str, Any]:
        return {
            "priority": self.default_priority,
            "view_mode": self.view_mode,
            "filter_query": self.filter_query,
            "unread_count": self.unread_count,
            "agent_tool": self.active_agent_tool,
            "agent_summary": self.active_agent_summary,
            "active_workers": self.active_workers,
            "max_workers": self.max_workers,
            "running_jobs": self.running_jobs_count,
            "turn_calls": self.turn_calls,
            "total_calls": self.total_calls,
            "server_online": self.server_online,
            "poll_failures": self.poll_failures,
        }

    def _handle_hotkey(self, key: str) -> None:
        if key == "CTRL_P":
            order: tuple[PriorityLevel, ...] = ("normal", "high", "urgent")
            idx = (order.index(self.default_priority) + 1) % len(order)
            self.default_priority = order[idx]
        elif key == "F2":
            modes: tuple[ViewMode, ...] = ("all", "chat", "tools")
            idx = (modes.index(self.view_mode) + 1) % len(modes)
            self.view_mode = modes[idx]
            self.reflow_screen()
        elif key == "F1":
            self._emit_system_box(self._build_help_box())
        elif key == "F3":
            self._emit_system_box(self._build_jobs_box())
        elif key == "F4":
            self._emit_system_box(self._build_status_box())

    def handle_terminal_resize(self, cols: int, rows: int) -> None:
        """Triggered automatically when the terminal window is resized."""
        del rows
        self.reflow_screen(cols_override=cols)

    def reflow_screen(self, cols_override: int | None = None) -> None:
        """
        Clears the screen & scrollback and re-renders all visible FeedEvents from the
        canonical SSOT store at the exact current terminal width, eliminating any resize overlap.
        """
        with self.editor.lock:
            w = Theme.term_width(cols_override)
            self.editor._dock_active = False
            self.editor._dock_line_count = 0

            rendered_blocks: list[str] = []
            # Keep the most recent 80 visible events during reflow to keep screen snappy
            visible_events = [
                ev for ev in self.feed_events
                if ev.matches_view(self.view_mode, self.filter_query)
            ][-80:]

            for ev in visible_events:
                block = ev.render(width=w)
                if block:
                    cleared = "\n".join(f"{line}{Theme.CLEAR_EOL}" for line in block.splitlines())
                    rendered_blocks.append(cleared)

            body = "\n\n".join(rendered_blocks)
            sys.stdout.write(f"{Theme.SYNC_START}{Theme.CLEAR_SCREEN}")
            if body:
                sys.stdout.write(body + "\n\n")
            sys.stdout.write(Theme.SYNC_END)
            sys.stdout.flush()
            self.editor.redraw_locked()

    def record_and_print_event(self, ev: FeedEvent) -> None:
        with self.editor.lock:
            self.feed_events.append(ev)
            if len(self.feed_events) > 400:
                self.feed_events = self.feed_events[-350:]
            if not ev.matches_view(self.view_mode, self.filter_query):
                return
            block = ev.render()
            self.editor.clear_dock_locked()
            cleared = "\n".join(f"{line}{Theme.CLEAR_EOL}" for line in block.splitlines())
            sys.stdout.write(f"\r{Theme.CLEAR_EOL}{cleared}\n\n")
            sys.stdout.flush()
            if sys.stdin.isatty():
                self.editor.redraw_locked()

    def safe_print_above_prompt(self, block: str, *, is_tool_action: bool = False) -> None:
        kind: EventKind = "tool" if is_tool_action else "system"
        ev = FeedEvent(kind=kind, text=block)
        self.record_and_print_event(ev)

    def _emit_system_box(self, box_str: str) -> None:
        with self.editor.lock:
            self.editor.clear_dock_locked()
            cleared = "\n".join(f"{line}{Theme.CLEAR_EOL}" for line in box_str.splitlines())
            sys.stdout.write(f"\r{Theme.CLEAR_EOL}{cleared}\n\n")
            sys.stdout.flush()
            if sys.stdin.isatty():
                self.editor.redraw_locked()

    def flush_deferred(self) -> None:
        if not self.deferred_tool_blocks:
            return
        combined = "\n\n".join(self.deferred_tool_blocks)
        self.deferred_tool_blocks.clear()
        sys.stdout.write(f"\r{Theme.CLEAR_EOL}{combined}\n\n")
        sys.stdout.flush()

    def _on_consolidated_action(self, block: str) -> None:
        self.safe_print_above_prompt(block, is_tool_action=True)

    def _on_consolidated_event(self, ev: FeedEvent) -> None:
        self.record_and_print_event(ev)

    def _is_noisy_action(self, tool_name: str, summary: str, details: str = "") -> bool:
        low_s = (summary or "").strip().lower()
        if low_s.startswith(("inbox:", "jobs:", "sysinfo:")):
            return True
        if low_s.endswith(("agent_outbox.txt", "bridge_chat.txt", "live_inbox.json", "run_task.py")):
            return True
        if details:
            return False
        if tool_name == "read_file" and low_s.startswith(("py:", "ps:", "cmd:", "powershell:", "python:", "exec:", "sh:", "bg:")):
            return True
        return False

    @classmethod
    def _compute_poll_delay(cls, failures: int) -> float:
        if failures <= 0:
            return cls._POLL_INTERVAL_S
        idx = min(failures - 1, len(cls._POLL_BACKOFF_SCHEDULE_S) - 1)
        return cls._POLL_BACKOFF_SCHEDULE_S[idx]

    def _apply_polled_state(self, state: dict[str, Any]) -> None:
        self.unread_count = int(state.get("unread_user_messages", 0) or 0)

        # Process action log
        for act in state.get("action_log") or []:
            aid = act.get("id", "")
            if aid and aid not in self.seen_action_ids:
                self.seen_action_ids.add(aid)
                t_name = act.get("tool", "tool")
                t_sum = act.get("summary", "")
                t_det = act.get("details", "")
                if self._is_noisy_action(t_name, t_sum, t_det):
                    continue
                act_sig = f"{t_name}|{t_sum.strip()}|{bool(t_det)}|{t_det[:64]}"
                if act_sig == self.last_rendered_act_sig:
                    continue
                self.last_rendered_act_sig = act_sig
                t_ts = (act.get("timestamp") or "")[11:19] or time.strftime("%H:%M:%S")
                self.consolidator.push(t_name, t_sum, t_ts, t_det)

        # Check idle flush for consolidated reading actions
        self.consolidator.check_idle_flush(idle_seconds=1.2)

        # Live Agent & Parallel Worker Pool telemetry updated in the framed input box header
        tele = state.get("agent_telemetry") or {}
        p_eng = state.get("parallel_engine") or {}
        self.active_workers = int(p_eng.get("active_workers", 0) or 0)
        self.max_workers = int(p_eng.get("max_workers", 32) or 32)
        self.running_jobs_count = int(p_eng.get("running_jobs", len(state.get("active_jobs") or [])) or 0)
        cur_tool = tele.get("last_tool") or "idle"
        cur_sum = tele.get("last_summary") or ""
        cur_upd = tele.get("updated_at") or ""
        new_sig = f"{cur_tool}|{cur_upd}"
        if new_sig != self.last_tool_sig and cur_upd:
            self.turn_calls += 1
            self.total_calls += 1
        if p_eng.get("total_calls") is not None:
            self.turn_calls = int(p_eng.get("turn_calls", self.turn_calls) or 0)
            self.total_calls = max(self.total_calls, int(p_eng.get("total_calls", 0) or 0))
        self.active_agent_tool = TuiRenderer.TOOL_LABELS.get(cur_tool, cur_tool)
        self.active_agent_summary = Path(cur_sum).name if ("\\" in cur_sum or "/" in cur_sum) else cur_sum[:28]
        self.last_tool_sig = new_sig

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
                self.consolidator.flush()

                if sender == "agent":
                    def _beep_agent() -> None:
                        try:
                            import winsound
                            winsound.Beep(880, 110)
                            winsound.Beep(1175, 140)
                        except Exception:
                            pass
                    threading.Thread(target=_beep_agent, daemon=True).start()
                    self.record_and_print_event(FeedEvent(kind="agent", timestamp=ts, text=txt))
                else:
                    if m.get("read_by_agent"):
                        self.delivered_ids.add(mid)
                    self.record_and_print_event(
                        FeedEvent(
                            kind="user",
                            timestamp=ts,
                            text=txt,
                            delivered_tool=(m.get("delivered_in_tool") or "tool") if m.get("read_by_agent") else None,
                            priority=m.get("priority", "normal"),
                        )
                    )
            elif sender == "user" and m.get("read_by_agent") and mid not in self.delivered_ids:
                self.delivered_ids.add(mid)
                tool_d = m.get("delivered_in_tool") or "tool"
                # Update existing user event in feed_events for future reflows
                for ev in reversed(self.feed_events):
                    if ev.kind == "user" and ev.text == txt and ev.delivered_tool is None:
                        ev.delivered_tool = tool_d
                        break
                ack_line = (
                    f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}"
                    f"[delivered to agent in {tool_d} · {ts}: \"{AnsiTextMetrics.truncate_plain(txt, 54)}\"]{Theme.RESET}"
                )
                self._emit_system_box(ack_line)

    def poll_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                state = self.client.get_state()
                client_online = bool(getattr(self.client, "last_online", True))
                self._apply_polled_state(state)
                if client_online:
                    self.poll_failures = 0
                    self.server_online = True
                else:
                    self.poll_failures += 1
                    self.server_online = False
            except Exception:
                self.poll_failures += 1
                self.server_online = False
            wait_s = self._compute_poll_delay(self.poll_failures)
            self.stop_event.wait(wait_s)

    def _get_state_resilient(self, limit: int = 50) -> dict[str, Any] | None:
        try:
            state = self.client.get_state(limit=limit)
            if not getattr(self.client, "last_online", True):
                self.server_online = False
            return state
        except Exception as exc:
            self.server_online = False
            self.poll_failures += 1
            self._emit_system_box(
                f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.WARN}"
                f"[bridge offline/reconnecting: {exc}]{Theme.RESET}"
            )
            return None

    def _build_jobs_box(self) -> str:
        try:
            st = self.client.get_state()
        except Exception:
            st = {}
        act_jobs = st.get("active_jobs") or []
        rows = [
            (
                f"{Theme.TOOL}Tool Calls:{Theme.RESET} {Theme.TEXT}"
                f"Turn: {self.turn_calls} · Session Total: {self.total_calls}{Theme.RESET}",
                -1,
            ),
            (
                f"{Theme.TOOL}Worker Pool:{Theme.RESET} {Theme.TEXT}"
                f"{self.active_workers}/{self.max_workers} active threads{Theme.RESET}",
                -1,
            ),
            (
                f"{Theme.TOOL}Active Jobs:{Theme.RESET} {Theme.TEXT}"
                f"{len(act_jobs)} running{Theme.RESET}",
                -1,
            ),
        ]
        for j in act_jobs[:8]:
            rows.append(
                (
                    f"  {Theme.WARN}* [{j.get('job_id')}] PID {j.get('pid')} "
                    f"({j.get('elapsed_s')}s): {j.get('command')}{Theme.RESET}",
                    -1,
                )
            )
        return TuiRenderer.render_box(
            rows,
            border_color=Theme.TOOL,
            title="Parallel Engine & Call Telemetry (F3)",
        )

    def _build_status_box(self) -> str:
        try:
            st = self.client.get_state()
            client_online = bool(getattr(self.client, "last_online", True))
        except Exception:
            st = {}
            client_online = False
        is_online = self.server_online and client_online
        t = st.get("agent_telemetry") or {}
        unread_cnt = st.get("unread_user_messages", 0)
        tot_msgs = st.get("total_messages", 0)
        tot_acts = len(st.get("action_log") or [])
        last_t = t.get("last_tool", "idle")
        last_s = AnsiTextMetrics.truncate_plain(t.get("last_summary", "") or "", 48)
        upd_t = (t.get("updated_at") or "")[11:19] or "recent"
        cols, rows = Theme.term_size()
        gateway_line = (
            f"{Theme.SUCCESS}[STATUS]{Theme.RESET} {Theme.TEXT}"
            f"HTTP Gateway active (127.0.0.1:8766){Theme.RESET}"
            if is_online
            else f"{Theme.WARN}[RECONNECTING]{Theme.RESET} {Theme.TEXT}"
            f"Offline fallback (retry backoff #{self.poll_failures}){Theme.RESET}"
        )
        status_rows = [
            (gateway_line, -1),
            (
                f"{Theme.TOOL}Tool Calls:{Theme.RESET} {Theme.SUCCESS}"
                f"Turn: {self.turn_calls}{Theme.RESET} · {Theme.TEXT}"
                f"Total: {self.total_calls} · Actions logged: {tot_acts}{Theme.RESET}",
                -1,
            ),
            (
                f"{Theme.TOOL}Viewport:{Theme.RESET} {Theme.TEXT}{cols}x{rows} cols/rows · "
                f"Mode: {self.view_mode.upper()} · Prio: {self.default_priority.upper()}{Theme.RESET}",
                -1,
            ),
            (
                f"{Theme.TOOL}Unread queue:{Theme.RESET} {Theme.WARN}"
                f"{unread_cnt} pending{Theme.RESET} · {Theme.TEXT}{tot_msgs} msgs{Theme.RESET}",
                -1,
            ),
            (
                f"{Theme.ACCENT}Last tool:{Theme.RESET} {Theme.TEXT}{last_t}{Theme.RESET} "
                f"{Theme.MUTED}({last_s}) · {upd_t}{Theme.RESET}",
                -1,
            ),
        ]
        return TuiRenderer.render_box(status_rows, border_color=Theme.TOOL, title="Bridge Status (F4)")

    def _build_help_box(self) -> str:
        help_rows = [
            (f"{Theme.WARN}<message>{Theme.RESET}            Inject live instruction into active Agent loop", -1),
            (f"{Theme.WARN}/urgent <txt>{Theme.RESET}        Send message with URGENT priority", -1),
            (f"{Theme.WARN}/high <txt>{Theme.RESET}          Send message with HIGH priority", -1),
            (f"{Theme.WARN}/prio [norm|high|urg]{Theme.RESET}Set sticky priority (or press Ctrl+P)", -1),
            (f"{Theme.WARN}/mode [all|chat|tools]{Theme.RESET}Switch view mode & reflow (or press F2)", -1),
            (f"{Theme.WARN}/filter [text|off]{Theme.RESET}   Live-filter viewport by keyword & reflow", -1),
            (f"{Theme.WARN}/search <text>{Theme.RESET}       Search across all recorded messages & actions", -1),
            (f"{Theme.WARN}/status{Theme.RESET}              Show live connection, geometry & queue telemetry", -1),
            (f"{Theme.WARN}/actions{Theme.RESET}             List recent tools executed by the Agent", -1),
            (f"{Theme.WARN}/diff{Theme.RESET}                Display recent file diffs & command outputs", -1),
            (f"{Theme.WARN}/jobs{Theme.RESET}                Inspect background jobs and runners", -1),
            (f"{Theme.WARN}/export [path.md]{Theme.RESET}    Export session transcript to Markdown file", -1),
            (f"{Theme.WARN}/gui{Theme.RESET}                 Launch floating desktop window in parallel", -1),
            (f"{Theme.WARN}/clear{Theme.RESET}               Clear terminal and reset message inbox", -1),
            (f"{Theme.WARN}/exit{Theme.RESET}                Exit interactive session", -1),
            (f"{Theme.MUTED}Keys: <-/-> · Ctrl+<-/-> word · Up/Dn history · Tab complete · ESC clear{Theme.RESET}", -1),
        ]
        return TuiRenderer.render_box(help_rows, border_color=Theme.TOOL, title="TUI Commands & Shortcuts")

    def print_welcome_banner(self) -> None:
        self.record_and_print_event(FeedEvent(kind="banner"))

    def run(self) -> None:
        enable_windows_ansi()
        self.print_welcome_banner()

        # Seed initial state: mark all pre-existing actions and messages as seen
        # so historical messages NEVER trigger sound alerts or fake "new message" prints
        try:
            init_state = self.client.get_state()
            if getattr(self.client, "last_online", True):
                self.server_online = True
                self.poll_failures = 0
            else:
                self.server_online = False
                self.poll_failures = 1
        except Exception:
            init_state = {}
            self.server_online = False
            self.poll_failures = 1
        self.unread_count = int(init_state.get("unread_user_messages", 0) or 0)
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
            self.record_and_print_event(FeedEvent(kind="divider", title=f"Recent history ({len(recent)})"))
            for m in recent:
                sender = m.get("sender", "user")
                txt = m.get("text", "")
                ts = (m.get("created_at") or "")[11:19]
                if sender == "agent":
                    self.record_and_print_event(FeedEvent(kind="agent", timestamp=ts, text=txt))
                else:
                    self.record_and_print_event(
                        FeedEvent(
                            kind="user",
                            timestamp=ts,
                            text=txt,
                            delivered_tool=(m.get("delivered_in_tool") or "tool") if m.get("read_by_agent") else None,
                            priority=m.get("priority", "normal"),
                        )
                    )

        tele = init_state.get("agent_telemetry") or {}
        if tele.get("last_tool"):
            cur_t = tele.get("last_tool", "idle")
            cur_s = tele.get("last_summary", "")
            self.active_agent_tool = TuiRenderer.TOOL_LABELS.get(cur_t, cur_t)
            self.active_agent_summary = Path(cur_s).name if ("\\" in cur_s or "/" in cur_s) else cur_s[:28]
            self.last_tool_sig = f"{cur_t}|{tele.get('updated_at')}"
            ts_init = (tele.get("updated_at") or "")[11:19] or time.strftime("%H:%M:%S")
            self.record_and_print_event(
                FeedEvent(kind="tool", timestamp=ts_init, tool_name=cur_t, summary=cur_s)
            )

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
                    self._emit_system_box(f"  {Theme.MUTED}{Theme.CONNECTOR} Closing TUI session...{Theme.RESET}")
                    break

                if low in {"/limpiar", "/clear"}:
                    try:
                        res = self.client.clear_inbox()
                    except Exception as exc:
                        self._emit_system_box(
                            f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.ERROR}"
                            f"[failed to clear inbox: {exc}]{Theme.RESET}"
                        )
                        continue
                    self.seen_ids.clear()
                    self.delivered_ids.clear()
                    self.feed_events = [FeedEvent(kind="banner")]
                    self.reflow_screen()
                    self._emit_system_box(
                        f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}"
                        f"[ok · screen and history cleared ({res.get('cleared', 0)} messages)]"
                        f"{Theme.RESET}"
                    )
                    continue

                if low.startswith("/mode") or low.startswith("/vista"):
                    parts = low.split(None, 1)
                    if len(parts) == 1:
                        modes: tuple[ViewMode, ...] = ("all", "chat", "tools")
                        self.view_mode = modes[(modes.index(self.view_mode) + 1) % len(modes)]
                    else:
                        arg = parts[1].strip()
                        if arg in ("all", "todo", "split"):
                            self.view_mode = "all"
                        elif arg in ("chat", "mensajes", "msgs"):
                            self.view_mode = "chat"
                        elif arg in ("tools", "actions", "acciones", "log"):
                            self.view_mode = "tools"
                    self.reflow_screen()
                    self._emit_system_box(
                        f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[view mode set to: {self.view_mode.upper()}]{Theme.RESET}"
                    )
                    continue

                if low.startswith("/filter") or low.startswith("/filtro"):
                    parts = txt.split(None, 1)
                    if len(parts) == 1 or parts[1].strip().lower() in ("off", "clear", "none", "all"):
                        self.filter_query = ""
                        self.reflow_screen()
                        self._emit_system_box(
                            f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[live filter cleared · showing all events]{Theme.RESET}"
                        )
                    else:
                        self.filter_query = parts[1].strip()
                        self.reflow_screen()
                        self._emit_system_box(
                            f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.WARN}[live filter active: \"{self.filter_query}\" · type '/filter off' to reset]{Theme.RESET}"
                        )
                    continue

                if low.startswith("/prio"):
                    parts = low.split(None, 1)
                    if len(parts) == 1:
                        order: tuple[PriorityLevel, ...] = ("normal", "high", "urgent")
                        self.default_priority = order[(order.index(self.default_priority) + 1) % len(order)]
                    else:
                        arg = parts[1].strip()
                        if arg.startswith("urg"):
                            self.default_priority = "urgent"
                        elif arg.startswith(("high", "alt")):
                            self.default_priority = "high"
                        else:
                            self.default_priority = "normal"
                    self._emit_system_box(
                        f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[default priority set to: {self.default_priority.upper()}]{Theme.RESET}"
                    )
                    continue

                if low.startswith("/search ") or low.startswith("/buscar "):
                    query = txt.split(None, 1)[1].strip()
                    q_low = query.lower()
                    st = self._get_state_resilient(limit=100)
                    if st is None:
                        continue
                    matches_rows: list[tuple[str, int]] = []
                    for m in st.get("messages") or []:
                        m_txt = m.get("text", "")
                        if q_low in m_txt.lower():
                            mts = (m.get("created_at") or "")[11:19]
                            who = "Agent" if m.get("sender") == "agent" else "You"
                            snippet = AnsiTextMetrics.truncate_plain(m_txt.replace("\n", " "), 62)
                            matches_rows.append(
                                (
                                    f"{Theme.ACCENT}[{who} · {mts}]{Theme.RESET} "
                                    f"{Theme.TEXT}{snippet}{Theme.RESET}",
                                    -1,
                                )
                            )
                    for a in st.get("action_log") or []:
                        a_sum = a.get("summary", "")
                        a_det = a.get("details", "")
                        if q_low in a_sum.lower() or q_low in a_det.lower():
                            ats = (a.get("timestamp") or "")[11:19]
                            lbl = TuiRenderer.TOOL_LABELS.get(a.get("tool", ""), a.get("tool", ""))
                            snippet = AnsiTextMetrics.truncate_plain(a_sum, 56)
                            matches_rows.append(
                                (
                                    f"{Theme.TOOL}[{lbl} · {ats}]{Theme.RESET} "
                                    f"{Theme.MUTED}{snippet}{Theme.RESET}",
                                    -1,
                                )
                            )
                    if not matches_rows:
                        matches_rows.append(
                            (f"{Theme.MUTED}No matches found for \"{query}\".{Theme.RESET}", -1)
                        )
                    self._emit_system_box(
                        TuiRenderer.render_box(
                            matches_rows[-18:],
                            border_color=Theme.INFO,
                            title=f"Search Results: {query}",
                        )
                    )
                    continue

                if low.startswith("/export"):
                    parts = txt.split(None, 1)
                    out_path = (
                        Path(parts[1].strip())
                        if len(parts) > 1
                        else Path("bridge_session_export.md")
                    )
                    st = self._get_state_resilient(limit=200)
                    if st is None:
                        continue
                    md_lines = [
                        "# Bridge — Session Export",
                        f"- **Exported at**: `{time.strftime('%Y-%m-%d %H:%M:%S')}`",
                        "",
                        "## Messages",
                    ]
                    for m in st.get("messages") or []:
                        mts = (m.get("created_at") or "")[:19]
                        who = (
                            "Agent"
                            if m.get("sender") == "agent"
                            else f"User ({m.get('priority', 'normal')})"
                        )
                        md_lines.append(f"### [{mts}] {who}\n\n{m.get('text', '')}\n")
                    md_lines.append("## Recent Tool Actions\n")
                    for a in st.get("action_log") or []:
                        ats = (a.get("timestamp") or "")[:19]
                        md_lines.append(
                            f"- `{ats}` **{a.get('tool', '')}**: `{a.get('summary', '')}`"
                        )
                    try:
                        out_path.write_text("\n".join(md_lines), encoding="utf-8")
                    except OSError as exc:
                        self._emit_system_box(
                            f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.ERROR}"
                            f"[failed to export session: {exc}]{Theme.RESET}"
                        )
                        continue
                    self._emit_system_box(
                        f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}"
                        f"[ok · session exported to {out_path.resolve()}]{Theme.RESET}"
                    )
                    continue

                if low in {"/estado", "/status", "/stats"}:
                    self._emit_system_box(self._build_status_box())
                    continue

                if low in {"/acciones", "/actions", "/log"}:
                    st = self._get_state_resilient()
                    if st is None:
                        continue
                    acts = st.get("action_log") or []
                    lines_out = [
                        f"  {Theme.MUTED}{Theme.CONNECTOR} "
                        f"Recent agent tool actions ({min(15, len(acts))}):{Theme.RESET}"
                    ]
                    for a in acts[-15:]:
                        ats = (a.get("timestamp") or "")[11:19]
                        lbl = TuiRenderer.TOOL_LABELS.get(a.get("tool", ""), a.get("tool", ""))
                        det = (a.get("details") or "").splitlines()
                        det_head = (
                            f" {Theme.ACCENT}[{AnsiTextMetrics.truncate_plain(det[0], 36)}]"
                            f"{Theme.RESET}"
                            if det
                            else ""
                        )
                        sum_trunc = AnsiTextMetrics.truncate_plain(a.get("summary", ""), 48)
                        lines_out.append(
                            f"     {Theme.TOOL}{Theme.BULLET} {lbl}{Theme.RESET}"
                            f"({sum_trunc}){det_head} {Theme.MUTED}· {ats}{Theme.RESET}"
                        )
                    self._emit_system_box("\n".join(lines_out))
                    continue

                if low in {"/diff", "/diffs", "/edits", "/cambios"}:
                    st = self._get_state_resilient()
                    if st is None:
                        continue
                    acts = [
                        a
                        for a in (st.get("action_log") or [])
                        if a.get("details")
                        and a.get("tool") in {"edit_file", "write_file", "exec_command"}
                    ]
                    if not acts:
                        self._emit_system_box(
                            f"  {Theme.MUTED}{Theme.CONNECTOR} "
                            f"No recent file diffs or command outputs recorded yet.{Theme.RESET}"
                        )
                    else:
                        blocks = [
                            f"  {Theme.MUTED}{Theme.CONNECTOR} "
                            f"Recent diffs & command executions ({min(6, len(acts))}):{Theme.RESET}"
                        ]
                        for a in acts[-6:]:
                            ats = (a.get("timestamp") or "")[11:19]
                            blocks.append(
                                TuiRenderer.format_tool_call(
                                    a.get("tool", "tool"),
                                    a.get("summary", ""),
                                    ats,
                                    a.get("details", ""),
                                )
                            )
                        self._emit_system_box("\n\n".join(blocks))
                    continue

                if low in {"/jobs", "/procesos"}:
                    self._emit_system_box(self._build_jobs_box())
                    continue

                if low in {"/ayuda", "/help"}:
                    self._emit_system_box(self._build_help_box())
                    continue

                if low == "/gui":
                    threading.Thread(target=launch_gui, daemon=True).start()
                    self._emit_system_box(
                        f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.SUCCESS}[ok · GUI floating window opened in parallel]{Theme.RESET}"
                    )
                    continue

                # Normal message sending (respecting sticky default_priority or prefix override)
                prio: str = self.default_priority
                if low.startswith("/urgente ") or low.startswith("/urgent "):
                    prio = "urgent"
                    txt = txt.split(" ", 1)[1].strip()
                elif low.startswith("/alta ") or low.startswith("/high "):
                    prio = "high"
                    txt = txt.split(" ", 1)[1].strip()

                if not txt:
                    continue

                self.turn_calls = 0
                try:
                    res = self.client.send_user_message(txt, priority=prio)
                except Exception as exc:
                    self.server_online = False
                    self.poll_failures += 1
                    self._emit_system_box(
                        f"  {Theme.MUTED}{Theme.CONNECTOR} {Theme.ERROR}"
                        f"[failed to send message: {exc}]{Theme.RESET}"
                    )
                    continue
                msg = res.get("message") or {}
                mid = msg.get("id", "")
                if mid:
                    self.seen_ids.add(mid)
                ts = (msg.get("created_at") or "")[11:19] or time.strftime("%H:%M:%S")
                self.record_and_print_event(
                    FeedEvent(
                        kind="user",
                        timestamp=ts,
                        text=txt,
                        delivered_tool=None,
                        priority=prio,
                    )
                )

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
    root.title("Bridge // Live Agent Messenger")
    root.geometry("760x520")
    root.configure(bg="#0b0e14")

    hdr_frame = tk.Frame(root, bg="#131822", bd=0, highlightthickness=1, highlightbackground="#eeaf46")
    hdr_frame.pack(fill=tk.X, padx=12, pady=(12, 6))

    title_lbl = tk.Label(
        hdr_frame,
        text="BRIDGE // LIVE CHANNEL (NON-INTERRUPTING)",
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

    def send_msg(event: Any = None) -> None:
        del event
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
    parser = argparse.ArgumentParser(description="Bridge Interactive TUI & Live Messenger.")
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
