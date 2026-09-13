"""Single-owner R20 Gateway delivery worker."""
from __future__ import annotations
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from r20_gateway.channels import NotificationChannelAdapter
from r20_gateway.publisher import DB_PATH
from r20_gateway.scheduler import GatewayScheduler
from r20_gateway.store import GatewayStore

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "data" / ".r20_gateway.lock"
LOG_FILE = ROOT / "logs" / "r20_gateway.log"
PID_FILE = ROOT / "data" / "r20_gateway.pid"
BJ_TZ = timezone(timedelta(hours=8))
RUNNING = True

def acquire_lock(handle) -> bool:
    if os.name == "nt":
        import msvcrt
        handle.seek(0); handle.write("0"); handle.flush(); handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def stop(*_: object) -> None:
    global RUNNING
    RUNNING = False


def format_message(row: dict[str, object]) -> str:
    created = str(row.get("created_at", ""))
    # Format cleaner timestamp if ISO format
    if "T" in created:
        created = created.replace("T", " ")[:19]
    title = str(row.get("title", "")).strip()
    body = str(row.get("message", "")).strip()
    return f"【Gate Quantum】{title}\n⏱️ 时间：{created}\n━━━━━━━━━━━━━━\n{body}"


def run() -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        if not acquire_lock(lock_handle):
            raise BlockingIOError
    except BlockingIOError:
        log("gateway worker already running; exiting")
        return
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    try:
        os.chmod(PID_FILE, 0o600)
    except OSError:
        pass
    store = GatewayStore(DB_PATH)
    store.recover_processing()
    scheduler = GatewayScheduler(store)
    scheduler.initialize_migration_baseline()
    log("gateway worker started with scheduler ownership")
    while RUNNING:
        launched = scheduler.tick()
        for job_name in launched:
            log(f"scheduled job={job_name}")
        deliveries = store.claim_due(20)
        if not deliveries:
            time.sleep(1)
            continue
        for delivery in deliveries:
            try:
                result = NotificationChannelAdapter(str(delivery["channel"])).send(format_message(delivery))
                if result.success:
                    store.complete(int(delivery["id"]), result.status, result.detail)
                    log(f"{result.status} event={delivery['event_id']} channel={delivery['channel']} detail={result.detail}")
                else:
                    store.fail(int(delivery["id"]), int(delivery["attempts"]), result.detail)
                    log(f"delivery failed event={delivery['event_id']} channel={delivery['channel']} detail={result.detail}")
            except Exception as exc:
                store.fail(int(delivery["id"]), int(delivery["attempts"]), str(exc))
                log(f"delivery exception event={delivery['event_id']} channel={delivery['channel']} type={type(exc).__name__}")
    scheduler.shutdown()
    try:
        if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    log("gateway worker stopped")


if __name__ == "__main__":
    run()
