"""Single-owner process supervisor for the R20 Gateway worker."""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PID_FILE = ROOT / "data" / "r20_gateway.pid"
LOG_FILE = ROOT / "logs" / "r20_gateway_supervisor.log"
_stop = threading.Event()
_thread: threading.Thread | None = None
_owned_pid = 0
WORKER_LOCK_FILE = ROOT / "data" / ".r20_gateway.lock"


def _alive(pid: int) -> bool:
    if pid <= 0: return False
    try: os.kill(pid, 0); return True
    except OSError: return False


def _windows_process_command_line(pid: int) -> str:
    """Return the command line for a Windows PID without extra dependencies."""
    try:
        hidden_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                f'(Get-CimInstance Win32_Process -Filter "ProcessId = {int(pid)}").CommandLine',
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=hidden_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_gateway_worker(pid: int) -> bool:
    if os.name == "nt":
        # PID values are reused. Never trust a stale PID file without checking
        # the target process, otherwise shutdown can terminate an unrelated app.
        command_line = _windows_process_command_line(pid)
        # The module name plus this project's ASCII junction identifies the
        # worker without relying on legacy PowerShell Unicode path output.
        return "r20_gateway.worker" in command_line and "gate-quant" in command_line.lower()
    if not _alive(pid): return False
    try:
        cmdline=(Path("/proc")/str(pid)/"cmdline").read_bytes().replace(b"\0",b" ").decode(errors="replace")
        cwd=(Path("/proc")/str(pid)/"cwd").resolve()
        return "r20_gateway.worker" in cmdline and cwd == ROOT.resolve()
    except OSError: return False


def _discover_gateway_worker() -> int:
    """Find the Gate worker by its project-local interpreter path on Windows."""
    if os.name != "nt":
        return 0
    script = (
        "Get-CimInstance Win32_Process | Where-Object {"
        "$_.CommandLine -match 'r20_gateway\\.worker' -and "
        "$_.CommandLine -match 'gate-quant|gate量化'} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    try:
        hidden_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=hidden_flags,
        )
        value = result.stdout.strip().splitlines()
        return int(value[-1].strip()) if value and value[-1].strip().isdigit() else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def current_pid() -> int:
    try: pid=int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError,ValueError): pid=0
    if _is_gateway_worker(pid): return pid
    try: PID_FILE.unlink(missing_ok=True)
    except OSError: pass
    discovered = _discover_gateway_worker()
    if discovered:
        try:
            PID_FILE.write_text(str(discovered), encoding="utf-8")
            os.chmod(PID_FILE, 0o600)
        except OSError:
            pass
        return discovered
    return 0


def _worker_lock_held() -> bool:
    """Use the worker's OS lock when Windows process metadata is unavailable."""
    if not WORKER_LOCK_FILE.exists():
        return False
    try:
        handle = WORKER_LOCK_FILE.open("r+", encoding="utf-8")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def ensure_worker() -> int:
    global _owned_pid
    pid=current_pid()
    if pid: return pid
    # A live worker owns this kernel lock. Do not spawn duplicates merely
    # because CIM/PowerShell process inspection timed out after resume.
    if _worker_lock_held(): return 0
    LOG_FILE.parent.mkdir(parents=True,exist_ok=True)
    with LOG_FILE.open("a",encoding="utf-8") as log:
        process=subprocess.Popen([sys.executable,"-m","r20_gateway.worker"],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
    PID_FILE.write_text(str(process.pid),encoding="utf-8"); os.chmod(PID_FILE,0o600); _owned_pid=process.pid
    return process.pid


def _run() -> None:
    while not _stop.is_set(): ensure_worker(); _stop.wait(10)


def start_supervisor() -> None:
    global _thread
    if _thread and _thread.is_alive(): return
    _stop.clear(); ensure_worker(); _thread=threading.Thread(target=_run,name="r20-gateway-supervisor",daemon=True); _thread.start()


def stop_supervisor() -> None:
    global _owned_pid
    _stop.set()
    pid=_owned_pid
    if pid and _is_gateway_worker(pid):
        try: os.kill(pid,signal.SIGTERM)
        except OSError: pass
        deadline=time.time()+8
        while _alive(pid) and time.time()<deadline: time.sleep(.1)
    if pid and not _alive(pid):
        try: PID_FILE.unlink(missing_ok=True)
        except OSError: pass
    _owned_pid=0
