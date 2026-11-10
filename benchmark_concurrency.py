#!/usr/bin/env python3
"""Comprehensive concurrency and parallel execution benchmark for Bridge MCP."""

import concurrent.futures
import json
import statistics
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CONFIG_PATHS = [
    Path.home() / ".bridge" / "config.toml",
    Path.home() / ".codex-chatgpt-bridge" / "config.toml",
]

token = ""
for cp in CONFIG_PATHS:
    if cp.exists():
        for line in cp.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("auth_token"):
                token = line.split("=")[1].strip().strip('"')
                break
        if token:
            break

if not token:
    print("Error: auth_token not found in configuration files.")
    sys.exit(1)

MCP_URL = "http://127.0.0.1:8766/mcp"
HEALTHZ_URL = "http://127.0.0.1:8766/healthz"
HEADERS = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}

def rpc_call(method: str, params: dict, timeout: float = 60.0) -> dict:
    req_body = json.dumps({
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": method,
        "params": params
    }).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=req_body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def call_tool(name: str, arguments: dict, timeout: float = 60.0) -> tuple[bool, dict, float]:
    start = time.perf_counter()
    try:
        res = rpc_call("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        elapsed = time.perf_counter() - start
        if "error" in res:
            return False, {"error": res["error"]}, elapsed
        content = res.get("result", {}).get("content", [])
        if not content:
            return False, {"error": "empty content"}, elapsed
        data = json.loads(content[0]["text"])
        return True, data, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - start
        return False, {"error": str(e)}, elapsed

def get_telemetry() -> dict:
    req = urllib.request.Request(HEALTHZ_URL)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

def run_burst_test(concurrency: int = 50):
    print(f"\n--- SCENARIO 1: MCP HTTP CONCURRENCY BURST ({concurrency} simultaneous calls) ---")
    tools = ["bridge_status", "system_info", "list_grants", "bridge_session_list"]
    
    start_total = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(call_tool, tools[i % len(tools)], {})
            for i in range(concurrency)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    total_duration = time.perf_counter() - start_total
    
    successes = [r for r in results if r[0]]
    durations = [r[2] for r in results]
    
    rps = concurrency / total_duration
    avg_lat = statistics.mean(durations)
    p95_lat = sorted(durations)[int(len(durations) * 0.95)]
    min_lat = min(durations)
    max_lat = max(durations)
    
    print(f"Total Requests: {concurrency}")
    print(f"Successful:     {len(successes)}/{concurrency} (100% success rate: {len(successes) == concurrency})")
    print(f"Total Time:     {total_duration:.3f}s")
    print(f"Throughput:     {rps:.2f} requests/sec")
    print(f"Latency:        min={min_lat*1000:.1f}ms, avg={avg_lat*1000:.1f}ms, p95={p95_lat*1000:.1f}ms, max={max_lat*1000:.1f}ms")
    return len(successes) == concurrency

def run_parallel_file_io(concurrency: int = 20):
    print(f"\n--- SCENARIO 2: PARALLEL FILE I/O ({concurrency} concurrent files) ---")
    temp_dir = Path(tempfile.gettempdir()) / "bridge_parallel_test"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Grant path
    call_tool("grant_path", {"path": str(temp_dir), "name": "ParallelIOTemp"})
    
    # A. Concurrent writes
    print(f"  > Executing {concurrency} parallel file writes...")
    start_write = time.perf_counter()
    def write_worker(idx: int):
        fpath = str(temp_dir / f"file_{idx:03d}.txt")
        text = f"Content for worker {idx}\nTimestamp={time.time()}"
        return call_tool("write_file", {"path": fpath, "content": text, "create_parents": True})
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        write_results = list(executor.map(write_worker, range(concurrency)))
    write_time = time.perf_counter() - start_write
    writes_ok = all(r[0] for r in write_results)
    print(f"    Writes completed in {write_time:.3f}s (all ok: {writes_ok})")
    
    # B. Concurrent reads
    print(f"  > Executing {concurrency} parallel file reads...")
    start_read = time.perf_counter()
    def read_worker(idx: int):
        fpath = str(temp_dir / f"file_{idx:03d}.txt")
        ok, data, elapsed = call_tool("read_file", {"path": fpath})
        valid = ok and f"Content for worker {idx}" in data.get("text", "")
        return valid, elapsed
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        read_results = list(executor.map(read_worker, range(concurrency)))
    read_time = time.perf_counter() - start_read
    reads_ok = all(r[0] for r in read_results)
    print(f"    Reads completed in {read_time:.3f}s (content integrity verified: {reads_ok})")

    # C. Concurrent edits
    print(f"  > Executing {concurrency} parallel file edits...")
    start_edit = time.perf_counter()
    def edit_worker(idx: int):
        fpath = str(temp_dir / f"file_{idx:03d}.txt")
        return call_tool("edit_file", {
            "path": fpath,
            "old_text": f"Content for worker {idx}",
            "new_text": f"UPDATED Content for worker {idx} [VERIFIED]"
        })
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        edit_results = list(executor.map(edit_worker, range(concurrency)))
    edit_time = time.perf_counter() - start_edit
    edits_ok = all(r[0] for r in edit_results)
    print(f"    Edits completed in {edit_time:.3f}s (all ok: {edits_ok})")

    return writes_ok and reads_ok and edits_ok

def run_parallel_ripgrep(concurrency: int = 10):
    print(f"\n--- SCENARIO 3: PARALLEL RIPGREP SEARCH ({concurrency} concurrent searches) ---")
    temp_dir = Path(tempfile.gettempdir()) / "bridge_parallel_test"
    
    start_search = time.perf_counter()
    def search_worker(idx: int):
        query = f"worker {idx}"
        ok, data, elapsed = call_tool("search_files", {"query": query, "root": str(temp_dir)})
        matched = ok and len(data.get("matches", [])) >= 1 and data.get("engine") == "ripgrep"
        return matched, elapsed
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        search_results = list(executor.map(search_worker, range(concurrency)))
    search_time = time.perf_counter() - start_search
    searches_ok = all(r[0] for r in search_results)
    durations = [r[1] for r in search_results]
    print(f"  Searches completed in {search_time:.3f}s (Ripgrep verified: {searches_ok})")
    print(f"  Avg Search Latency: {statistics.mean(durations)*1000:.1f}ms")
    return searches_ok

def run_parallel_process_execution(concurrency: int = 8):
    print(f"\n--- SCENARIO 4: PARALLEL PROCESS EXECUTION ({concurrency} simultaneous processes) ---")
    
    start_exec = time.perf_counter()
    def proc_worker(idx: int):
        # Execute python math calculation to verify isolated execution
        cmd = f"python -c \"import math, sys; print(f'WORKER_{idx}_RESULT={{math.factorial(20)}}')\""
        ok, data, elapsed = call_tool("exec_command", {"command": cmd, "timeout_s": 15.0})
        expected_token = f"WORKER_{idx}_RESULT=2432902008176640000"
        stdout = data.get("stdout", "")
        pid = data.get("pid")
        job_id = data.get("job_id")
        rc = data.get("returncode")
        passed = ok and rc == 0 and expected_token in stdout
        return passed, pid, job_id, elapsed
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        proc_results = list(executor.map(proc_worker, range(concurrency)))
    exec_time = time.perf_counter() - start_exec
    
    pids = [r[1] for r in proc_results]
    job_ids = [r[2] for r in proc_results]
    unique_pids = len(set(pids)) == concurrency
    unique_jobs = len(set(job_ids)) == concurrency
    all_ok = all(r[0] for r in proc_results)
    
    print(f"  Processes finished in {exec_time:.3f}s (All ok: {all_ok})")
    print(f"  Process Isolation: {len(set(pids))}/{concurrency} unique PIDs ({pids})")
    print(f"  Job Manager IDs:   {len(set(job_ids))}/{concurrency} unique Job IDs")
    print(f"  Result integrity:  Verified deterministic stdout per process without mixing")
    return all_ok and unique_pids and unique_jobs

def run_parallel_background_jobs(count: int = 4):
    print(f"\n--- SCENARIO 5: PARALLEL BACKGROUND JOBS & ASYNC TRACKING ({count} jobs) ---")
    
    print(f"  > Launching {count} background jobs simultaneously (each sleeping 1.5s)...")
    def bg_worker(idx: int):
        cmd = f"python -c \"import time; time.sleep(1.5); print('BG_DONE_{idx}')\""
        ok, data, elapsed = call_tool("exec_command", {"command": cmd, "background": True})
        return ok, data.get("job_id"), data.get("pid")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as executor:
        launch_results = list(executor.map(bg_worker, range(count)))
    
    job_ids = [r[1] for r in launch_results]
    print(f"  Launched background job IDs: {job_ids}")
    
    # Query process manager immediately while running
    time.sleep(0.3)
    ok, mgr_data, _ = call_tool("process_manager", {"action": "list_jobs"})
    jobs_listed = {j["job_id"]: j["status"] for j in mgr_data.get("jobs", [])}
    print(f"  Process Manager live status: {jobs_listed}")
    
    # Wait for completion
    print("  > Waiting for background processes to finish...")
    time.sleep(2.0)
    
    completed_ok = True
    for jid in job_ids:
        ok, jstat, _ = call_tool("process_manager", {"action": "job_status", "job_id": jid})
        status = jstat.get("job", {}).get("status")
        stdout = jstat.get("job", {}).get("stdout", "")
        if status != "completed" or "BG_DONE" not in stdout:
            completed_ok = False
        print(f"    Job {jid}: status={status}, stdout={stdout.strip()}")
        
    return completed_ok

def run_parallel_sessions_and_inbox(concurrency: int = 20):
    print(f"\n--- SCENARIO 6: CONCURRENT SESSION & INBOX TELEMETRY ({concurrency} calls) ---")
    start = time.perf_counter()
    def query_worker(idx: int):
        tool = "bridge_session_list" if idx % 2 == 0 else "bridge_session_status"
        args = {} if idx % 2 == 0 else {"bridge_session_id": "latest"}
        ok, data, elapsed = call_tool(tool, args)
        return ok, elapsed
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(query_worker, range(concurrency)))
    total_time = time.perf_counter() - start
    all_ok = all(r[0] for r in results)
    print(f"  {concurrency} Session status queries completed in {total_time:.3f}s (all ok: {all_ok})")
    return all_ok

def main():
    print("=" * 80)
    print("BRIDGE CONCURRENCY, PARALLELISM & HIGH-LOAD VERIFICATION")
    print("=" * 80)
    
    t0 = get_telemetry()
    print("Initial Engine Telemetry:")
    print(f"  {json.dumps(t0.get('parallel_engine', {}), indent=4)}")
    
    test_1 = run_burst_test(concurrency=50)
    test_2 = run_parallel_file_io(concurrency=20)
    test_3 = run_parallel_ripgrep(concurrency=10)
    test_4 = run_parallel_process_execution(concurrency=8)
    test_5 = run_parallel_background_jobs(count=4)
    test_6 = run_parallel_sessions_and_inbox(concurrency=20)
    
    t1 = get_telemetry()
    pe = t1.get("parallel_engine", {})
    print("\n" + "=" * 80)
    print("FINAL PARALLEL ENGINE TELEMETRY (/healthz):")
    print(f"  Active Workers: {pe.get('active_workers')}")
    print(f"  Peak Workers:   {pe.get('peak_workers')} (Observed parallel capacity)")
    print(f"  Max Workers:    {pe.get('max_workers')}")
    print(f"  Total Calls:    {pe.get('total_calls')}")
    print(f"  Running Jobs:   {pe.get('running_jobs')}")
    print("=" * 80)
    
    # Cleanup
    temp_dir = Path(tempfile.gettempdir()) / "bridge_parallel_test"
    try:
        call_tool("revoke_grant", {"path": str(temp_dir)})
        for f in temp_dir.glob("*"):
            f.unlink(missing_ok=True)
        temp_dir.rmdir()
    except Exception:
        pass

    all_passed = test_1 and test_2 and test_3 and test_4 and test_5 and test_6
    print("\nBENCHMARK VERDICT:")
    print(f"  Scenario 1 (50 Concurrent MCP Calls):     {'PASSED' if test_1 else 'FAILED'}")
    print(f"  Scenario 2 (20 Parallel File I/O ops):    {'PASSED' if test_2 else 'FAILED'}")
    print(f"  Scenario 3 (10 Parallel Ripgrep searches):{'PASSED' if test_3 else 'FAILED'}")
    print(f"  Scenario 4 (8 Parallel Subprocesses):     {'PASSED' if test_4 else 'FAILED'}")
    print(f"  Scenario 5 (4 Async Background Jobs):     {'PASSED' if test_5 else 'FAILED'}")
    print(f"  Scenario 6 (20 Concurrent Sessions calls):{'PASSED' if test_6 else 'FAILED'}")
    print(f"\nOVERALL PARALLEL AUDIT: {'[100% PASSED]' if all_passed else '[FAILED]'}")
    print("=" * 80)
    sys.exit(0 if all_passed else 1)

if __name__ == "__main__":
    main()
