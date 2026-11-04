"""Autonomous self-healing watchdog for Codex ChatGPT Bridge (no hairpin reset)."""
import os, subprocess, time, urllib.request

BRIDGE_DIR = r"C:\Users\LAZARINOS\codex-chatgpt-bridge-clean"
LOCAL_HEALTH_URL = "http://127.0.0.1:8766/healthz"

def check_local():
    try:
        with urllib.request.urlopen(LOCAL_HEALTH_URL, timeout=3.5) as r:
            return r.status == 200
    except Exception:
        return False

def check_funnel_cli():
    try:
        res = subprocess.run(["tailscale", "funnel", "status"], capture_output=True, text=True, timeout=5)
        out = (res.stdout or "") + (res.stderr or "")
        return "8766" in out and "Funnel on" in out
    except Exception:
        return True

def main():
    fails = 0
    tick = 0
    while True:
        time.sleep(8.0)
        tick += 1
        if not check_local():
            fails += 1
            if fails >= 2:
                ps = os.path.join(BRIDGE_DIR, "restart-bridge.ps1")
                subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps],
                               cwd=BRIDGE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                fails = 0
        else:
            fails = 0
        if tick % 4 == 0 and not check_funnel_cli():
            subprocess.run(["tailscale", "funnel", "--bg", "8766"], cwd=BRIDGE_DIR,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

if __name__ == "__main__":
    main()
