from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prevent tests from reading or mutating the user's live ~/.bridge state."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("BRIDGE_DISABLE_BEEP", "1")
    if sys.platform == "win32":
        import winsound

        monkeypatch.setattr(winsound, "Beep", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(winsound, "MessageBeep", lambda *_args, **_kwargs: None)
    return fake_home
