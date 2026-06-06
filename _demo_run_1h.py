# =============================================================================
# _demo_run_1h.py — runner sementara: jalankan bot demo 1 jam lalu stop bersih.
# Dipakai untuk smoke test "berjalan normal". Boleh dihapus setelah selesai.
# =============================================================================
import os
import signal
import subprocess
import sys
import time

DURATION = 3600  # 1 jam

os.makedirs("logs", exist_ok=True)
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
logf = open("logs/demo_run_1h.log", "w", encoding="utf-8")

start = time.time()
logf.write(f"[wrapper] start {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
logf.flush()

creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
proc = subprocess.Popen(
    [sys.executable, "user/main.py"],
    stdout=logf, stderr=subprocess.STDOUT, env=env, creationflags=creationflags,
)

try:
    proc.wait(timeout=DURATION)
    msg = f"[wrapper] main.py EXITED EARLY code={proc.returncode} after {time.time()-start:.0f}s"
except subprocess.TimeoutExpired:
    logf.write("[wrapper] 1h elapsed — stopping bot gracefully\n")
    logf.flush()
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # → SIGINT-like, graceful
        else:
            proc.send_signal(signal.SIGINT)
    except Exception as e:
        logf.write(f"[wrapper] signal err: {e}\n")
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    msg = f"[wrapper] bot stopped (code={proc.returncode}) after {time.time()-start:.0f}s"

logf.write(msg + "\n")
logf.flush()
logf.close()
print(msg)
