"""Keep the local OKX-to-VPS SSH forward alive with the backend process."""
from __future__ import annotations

import os
import socket
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

from scripts.okx_runtime import selected_environment

ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "logs" / "okx_proxy_tunnel.log"
_stop = threading.Event()
_thread: threading.Thread | None = None
_process: subprocess.Popen[bytes] | None = None


def _local_proxy_address() -> tuple[str, int] | None:
    parsed = urlparse(selected_environment().proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return parsed.hostname, parsed.port


def _listening(address: tuple[str, int], timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


def _ssh_command(address: tuple[str, int]) -> list[str]:
    host, port = address
    ssh_host = os.getenv("R20_OKX_PROXY_SSH_HOST", "okx-vps").strip() or "okx-vps"
    remote = os.getenv("R20_OKX_PROXY_REMOTE", "127.0.0.1:8888").strip() or "127.0.0.1:8888"
    return [
        "ssh.exe" if os.name == "nt" else "ssh",
        "-NT", "-L", f"{host}:{port}:{remote}",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        ssh_host,
    ]


def _start_process(address: tuple[str, int]) -> subprocess.Popen[bytes]:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("ab")
    try:
        return subprocess.Popen(
            _ssh_command(address), cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        log.close()


def _run() -> None:
    global _process
    address = _local_proxy_address()
    if not address:
        return
    while not _stop.is_set():
        if _process is not None and _process.poll() is not None:
            _process = None
        if not _listening(address):
            if _process is not None:
                _process.terminate()
                try:
                    _process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _process.kill()
            _process = _start_process(address)
        _stop.wait(5)


def start_supervisor() -> None:
    global _thread
    enabled = os.getenv("R20_OKX_PROXY_TUNNEL_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled or _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="r20-okx-proxy-supervisor", daemon=True)
    _thread.start()


def stop_supervisor() -> None:
    global _process, _thread
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=6)
    if _process is not None and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _process.kill()
    _process = None
    _thread = None
