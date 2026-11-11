from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import codex_chatgpt_bridge.process_runner as process_runner
from codex_chatgpt_bridge.process_runner import BackgroundJob, BackgroundJobManager, StreamBuffer


@pytest.mark.asyncio
async def test_background_job_manager_periodic_cleanup_reaps_finished_jobs(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager(
        cleanup_interval_s=0.05,
        finished_ttl_s=0.05,
        start_cleanup_thread=True,
    )
    try:
        fast_job = await manager.start_job(
            command="print('done')",
            cwd=tmp_path,
            shell="python",
            allow_stdin=False,
        )
        slow_job = await manager.start_job(
            command="import time; time.sleep(5)",
            cwd=tmp_path,
            shell="python",
            allow_stdin=False,
        )

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            ids = {j["job_id"] for j in manager.list_jobs()}
            if fast_job.job_id not in ids:
                break

        remaining_ids = {j["job_id"] for j in manager.list_jobs()}
        assert fast_job.job_id not in remaining_ids
        assert fast_job._handles_released is True
        assert slow_job.job_id in remaining_ids

        await manager.kill_job(slow_job.job_id)
    finally:
        manager.close()

    assert manager._cleanup_thread is not None
    assert not manager._cleanup_thread.is_alive()


@pytest.mark.asyncio
async def test_clear_finished_reaps_dead_orphaned_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = BackgroundJobManager(start_cleanup_thread=False)
    loop = asyncio.get_running_loop()

    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_transport = MagicMock()
    fake_proc._transport = fake_transport

    task1: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(10))
    task2: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(10))

    orphan = BackgroundJob(
        job_id="orphan01",
        command="echo orphan",
        shell="python",
        cwd=str(Path.cwd()),
        pid=999_999,
        started_at="2026-11-10T00:00:00+00:00",
        started_monotonic=time.monotonic() - 10.0,
        process=fake_proc,
        stdout_buf=StreamBuffer(),
        stderr_buf=StreamBuffer(),
        stdout_task=task1,
        stderr_task=task2,
        loop=loop,
    )
    manager._jobs[orphan.job_id] = orphan
    monkeypatch.setattr(process_runner, "_is_os_pid_alive", lambda _pid: False)

    removed = manager.clear_finished(max_age_s=0.0)
    await asyncio.gather(task1, task2, return_exceptions=True)

    assert removed == 1
    assert orphan.job_id not in manager._jobs
    assert orphan.returncode == -1
    assert orphan._handles_released is True
    assert fake_transport.close.called
    assert task1.cancelled()
    assert task2.cancelled()
    assert sys.version_info >= (3, 11)
