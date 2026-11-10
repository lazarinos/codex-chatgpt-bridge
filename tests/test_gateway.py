from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codex_chatgpt_bridge.config import BridgeConfig, BridgeGrant, load_config
from codex_chatgpt_bridge.gateway import BridgeError, LocalGateway


@pytest.mark.asyncio
async def test_read_file_requires_read_grant(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    note = approved / "note.md"
    note.write_text("hello bridge", encoding="utf-8")
    gateway = LocalGateway(
        BridgeConfig(trust_mode="restricted", grants=[BridgeGrant(path=approved, read=True, write=False, execute=False)])
    )

    result = await gateway.read_file(path=str(note))

    assert result["text"] == "hello bridge"


@pytest.mark.asyncio
async def test_read_file_denies_outside_grant(tmp_path: Path) -> None:
    gateway = LocalGateway(BridgeConfig(trust_mode="restricted", grants=[BridgeGrant(path=tmp_path / "approved")]))

    with pytest.raises(BridgeError, match="not inside an approved grant"):
        await gateway.read_file(path=str(tmp_path / "outside.txt"))


@pytest.mark.asyncio
async def test_write_file_requires_write_grant(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    read_only = LocalGateway(
        BridgeConfig(trust_mode="restricted", grants=[BridgeGrant(path=approved, read=True, write=False, execute=False)])
    )

    with pytest.raises(BridgeError, match="write is not allowed"):
        await read_only.write_file(path=str(approved / "new.txt"), content="nope")

    writable = LocalGateway(
        BridgeConfig(trust_mode="restricted", grants=[BridgeGrant(path=approved, read=True, write=True, execute=False)])
    )
    result = await writable.write_file(path=str(approved / "new.txt"), content="ok")

    assert result["bytes"] == 2
    assert (approved / "new.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_full_delegate_can_read_and_write_outside_grants(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_named = outside / ".env"
    secret_named.write_text("full access", encoding="utf-8")
    gateway = LocalGateway(BridgeConfig(trust_mode="full_delegate", grants=[]))

    read_result = await gateway.read_file(path=str(secret_named))
    write_result = await gateway.write_file(path=str(outside / "new.txt"), content="ok")

    assert read_result["text"] == "full access"
    assert write_result["bytes"] == 2
    assert (outside / "new.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_runtime_mode_and_grant_tools_persist_to_config(tmp_path: Path) -> None:
    config_path = tmp_path / "bridge.toml"
    grant_path = tmp_path / "project"
    grant_path.mkdir()
    gateway = LocalGateway(BridgeConfig(), config_path=config_path)

    mode_result = await gateway.set_bridge_mode(mode="full_delegate")
    grant_result = await gateway.grant_path(
        path=str(grant_path),
        name="project",
        read=True,
        write=True,
        execute=True,
    )
    revoke_result = await gateway.revoke_grant(path=str(grant_path))

    loaded = load_config(config_path)
    assert mode_result["trust_mode"] == "full_delegate"
    assert grant_result["grant"]["write"] is True
    assert revoke_result["removed"] is True
    assert loaded.trust_mode == "full_delegate"
    assert loaded.grants == []


@pytest.mark.asyncio
async def test_grant_path_defaults_to_read_only(tmp_path: Path) -> None:
    grant_path = tmp_path / "project"
    grant_path.mkdir()
    gateway = LocalGateway(BridgeConfig(), config_path=tmp_path / "bridge.toml")

    result = await gateway.grant_path(path=str(grant_path))

    assert result["grant"]["read"] is True
    assert result["grant"]["write"] is False
    assert result["grant"]["execute"] is False


@pytest.mark.asyncio
async def test_codex_task_run_requires_execute_grant(tmp_path: Path) -> None:
    gateway = LocalGateway(
        BridgeConfig(
            trust_mode="restricted",
            enable_codex_tasks=True,
            grants=[BridgeGrant(path=tmp_path, read=True, write=True, execute=False)],
        )
    )

    with pytest.raises(BridgeError, match="execute is not allowed"):
        await gateway.codex_task_run(prompt="say hi", cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_codex_task_run_disabled_by_default(tmp_path: Path) -> None:
    gateway = LocalGateway(
        BridgeConfig(
            enable_codex_tasks=False,
            grants=[BridgeGrant(path=tmp_path, read=True, write=True, execute=True)],
        )
    )

    with pytest.raises(BridgeError, match="disabled"):
        await gateway.codex_task_run(prompt="say hi", cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_codex_session_start_records_persistent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )
    commands: list[list[str]] = []

    async def fake_run_codex_command(
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        cwd: Path,
    ) -> tuple[int, str, str]:
        commands.append(command)
        output_path.write_text("done", encoding="utf-8")
        return (
            0,
            '{"type":"session_configured","session_id":"019ee9e9-ecb8-72e3-a495-43674e1d7576"}\n',
            "",
        )

    monkeypatch.setattr(gateway, "_run_codex_command", fake_run_codex_command)
    monkeypatch.setattr(gateway, "_codex_path", lambda: "/usr/local/bin/codex")

    result = await gateway.codex_session_start(prompt="inspect repo", cwd=str(tmp_path))

    assert result["codex_session_id"] == "019ee9e9-ecb8-72e3-a495-43674e1d7576"
    assert result["final_message"] == "done"
    assert commands[0][1] == "exec"
    assert "--dangerously-bypass-approvals-and-sandbox" in commands[0]
    sessions = await gateway.codex_session_list()
    assert sessions["sessions"][0]["codex_session_id"] == result["codex_session_id"]


@pytest.mark.asyncio
async def test_codex_session_continue_resumes_latest_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )
    commands: list[list[str]] = []

    async def fake_run_codex_command(
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        cwd: Path,
    ) -> tuple[int, str, str]:
        commands.append(command)
        output_path.write_text(f"message {len(commands)}", encoding="utf-8")
        return (
            0,
            '{"type":"session_configured","session_id":"019ee9e9-ecb8-72e3-a495-43674e1d7576"}\n',
            "",
        )

    monkeypatch.setattr(gateway, "_run_codex_command", fake_run_codex_command)
    monkeypatch.setattr(gateway, "_codex_path", lambda: "/usr/local/bin/codex")
    await gateway.codex_session_start(prompt="start", cwd=str(tmp_path))

    result = await gateway.codex_session_continue(
        prompt="continue",
        bridge_session_id="latest",
    )

    assert result["final_message"] == "message 2"
    assert commands[1][1:3] == ["exec", "resume"]
    assert "019ee9e9-ecb8-72e3-a495-43674e1d7576" in commands[1]


@pytest.mark.asyncio
async def test_codex_session_continue_omits_sandbox_for_resume_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )
    commands: list[list[str]] = []

    async def fake_run_codex_command(
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        cwd: Path,
    ) -> tuple[int, str, str]:
        commands.append(command)
        output_path.write_text("ok", encoding="utf-8")
        return (
            0,
            '{"type":"session_configured","session_id":"019ee9e9-ecb8-72e3-a495-43674e1d7576"}\n',
            "",
        )

    monkeypatch.setattr(gateway, "_run_codex_command", fake_run_codex_command)
    monkeypatch.setattr(gateway, "_codex_path", lambda: "/usr/local/bin/codex")
    await gateway.codex_session_start(prompt="start", cwd=str(tmp_path))

    await gateway.codex_session_continue(
        prompt="continue",
        bridge_session_id="latest",
    )

    assert commands[1][1:3] == ["exec", "resume"]
    assert "--dangerously-bypass-approvals-and-sandbox" in commands[1]


@pytest.mark.asyncio
async def test_codex_session_continue_runs_resume_in_recorded_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )
    cwd_values: list[Path] = []

    async def fake_run_codex_command(
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        cwd: Path,
    ) -> tuple[int, str, str]:
        cwd_values.append(cwd)
        output_path.write_text("ok", encoding="utf-8")
        return (
            0,
            '{"type":"session_configured","session_id":"019ee9e9-ecb8-72e3-a495-43674e1d7576"}\n',
            "",
        )

    monkeypatch.setattr(gateway, "_run_codex_command", fake_run_codex_command)
    monkeypatch.setattr(gateway, "_codex_path", lambda: "/usr/local/bin/codex")
    await gateway.codex_session_start(prompt="start", cwd=str(project))

    await gateway.codex_session_continue(
        prompt="continue",
        bridge_session_id="latest",
    )

    assert cwd_values == [project.resolve(strict=False), project.resolve(strict=False)]


@pytest.mark.asyncio
async def test_codex_session_continue_rechecks_current_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )

    async def fake_run_codex_command(
        command: list[str],
        output_path: Path,
        wait_timeout_s: float,
        cwd: Path,
    ) -> tuple[int, str, str]:
        output_path.write_text("ok", encoding="utf-8")
        return (
            0,
            '{"type":"session_configured","session_id":"019ee9e9-ecb8-72e3-a495-43674e1d7576"}\n',
            "",
        )

    monkeypatch.setattr(gateway, "_run_codex_command", fake_run_codex_command)
    monkeypatch.setattr(gateway, "_codex_path", lambda: "/usr/local/bin/codex")
    await gateway.codex_session_start(prompt="start", cwd=str(project))
    await gateway.set_bridge_mode(mode="restricted")

    with pytest.raises(BridgeError, match="not inside an approved grant"):
        await gateway.codex_session_continue(prompt="continue", bridge_session_id="latest")


@pytest.mark.asyncio
async def test_codex_task_run_executes_with_direct_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )
    commands: list[list[str]] = []

    async def fake_run_argv(
        command: list[str],
        cwd: Path,
        timeout_s: float,
    ) -> tuple[int, str, str]:
        commands.append(command)
        return (0, "task finished", "")

    monkeypatch.setattr("codex_chatgpt_bridge.gateway.run_argv_captured", fake_run_argv)
    monkeypatch.setattr(gateway, "_codex_path", lambda: "/usr/local/bin/codex")

    result = await gateway.codex_task_run(
        prompt="run build",
        cwd=str(tmp_path),
    )

    assert result["returncode"] == 0
    assert "sandbox_mode" not in result
    assert "--dangerously-bypass-approvals-and-sandbox" in commands[0]
    assert commands[0][-1] == "run build"


def test_codex_exec_permission_args_bypass_sandbox(tmp_path: Path) -> None:
    gateway = LocalGateway(
        BridgeConfig(trust_mode="full_delegate", enable_codex_tasks=True),
        session_store_path=tmp_path / "sessions.json",
    )

    assert gateway._codex_permission_args() == ["--dangerously-bypass-approvals-and-sandbox"]
    assert gateway._codex_resume_permission_args() == ["--dangerously-bypass-approvals-and-sandbox"]


@pytest.mark.asyncio
async def test_search_files_python_fallback_when_rg_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr("codex_chatgpt_bridge.gateway.find_rg_path", lambda: None)
    sample = tmp_path / "hello.py"
    sample.write_text("def magic_bridge_func():\n    return 42\n", encoding="utf-8")
    gateway = LocalGateway(BridgeConfig(trust_mode="full_delegate"))

    res = await gateway.search_files(query="magic_bridge_func", root=str(tmp_path))

    assert res["engine"] == "python_native"
    assert len(res["matches"]) == 1
    assert res["matches"][0]["line"] == 1
    assert "magic_bridge_func" in res["matches"][0]["text"]


@pytest.mark.asyncio
async def test_exec_command_and_auto_background_and_process_manager(tmp_path: Path) -> None:
    gateway = LocalGateway(BridgeConfig(trust_mode="full_delegate"))

    fast = await gateway.exec_command(
        command="print('hello_fast_bridge')",
        shell="python",
        cwd=str(tmp_path),
    )
    assert fast["returncode"] == 0
    assert "hello_fast_bridge" in fast["stdout"]

    # Test auto-background on timeout
    slow = await gateway.exec_command(
        command="import time; print('started', flush=True); time.sleep(10)",
        shell="python",
        cwd=str(tmp_path),
        timeout_s=0.3,
        auto_background_on_timeout=True,
    )
    assert slow["status"] == "running_in_background"
    assert slow["timed_out"] is True
    job_id = slow["job_id"]

    status = await gateway.process_manager(action="job_status", job_id=job_id)
    assert status["job"]["job_id"] == job_id

    killed = await gateway.process_manager(action="kill_job", job_id=job_id)
    assert killed["job"]["status"] == "killed"


@pytest.mark.asyncio
async def test_edit_file_list_dir_file_ops_and_shortcuts(tmp_path: Path) -> None:
    gateway = LocalGateway(BridgeConfig(trust_mode="full_delegate"))
    target = tmp_path / "sub" / "sample.txt"

    await gateway.edit_file(
        path=str(target),
        new_text="line1\nline2\nline3\n",
        create_if_missing=True,
    )
    await gateway.edit_file(
        path=str(target),
        old_text="line2",
        new_text="line2_updated",
    )
    read_res = await gateway.read_file(path=f"{target}#L2-L2")
    assert read_res["text"].strip() == "line2_updated"

    # Directory read shortcut
    dir_res = await gateway.read_file(path=str(tmp_path / "sub"))
    assert dir_res["is_directory"] is True
    parsed_dir = json.loads(dir_res["text"])
    assert parsed_dir["entries"][0]["name"] == "sample.txt"

    # Python exec shortcut through read_file
    py_res = await gateway.read_file(path="py:print(21 * 2)")
    parsed_py = json.loads(py_res["text"])
    assert parsed_py["stdout"].strip() == "42"
