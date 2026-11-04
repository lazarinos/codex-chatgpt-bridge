from __future__ import annotations

import socket
import sys
import pytest
from codex_chatgpt_bridge.cli import _apply_windows_socket_inheritance_protection


def test_windows_socket_inheritance_protection() -> None:
    _apply_windows_socket_inheritance_protection()
    from uvicorn.config import Config

    cfg = Config(lambda scope, receive, send: None, host="127.0.0.1", port=0)
    sock = cfg.bind_socket()
    try:
        assert isinstance(sock, socket.socket)
        # On Windows, socket should have get_inheritable() == False
        if sys.platform == "win32":
            assert sock.get_inheritable() is False
    finally:
        sock.close()
