from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass


def _enabled(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class TunnelStatus:
    enabled: bool
    running: bool
    local_host: str
    local_port: int
    ssh_host: str
    remote_port: int
    pid: int | None = None


class GateSshTunnel:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return _enabled("GATE_SSH_TUNNEL_ENABLED")

    @property
    def local_host(self) -> str:
        return os.getenv("GATE_SSH_TUNNEL_LOCAL_HOST", "127.0.0.1")

    @property
    def local_port(self) -> int:
        return int(os.getenv("GATE_SSH_TUNNEL_LOCAL_PORT", "18081"))

    @property
    def ssh_host(self) -> str:
        return os.getenv("GATE_SSH_TUNNEL_HOST", "my-vps")

    @property
    def remote_port(self) -> int:
        return int(os.getenv("GATE_SSH_TUNNEL_REMOTE_PORT", "8888"))

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.local_host, self.local_port), timeout=0.4):
                return True
        except OSError:
            return False

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self.process and self.process.poll() is None and self._port_open():
                return
            if self._port_open():
                # The Gate scheduler may own the shared forward already. Reuse
                # a responsive tunnel instead of creating a duplicate SSH link.
                return
            ssh = shutil.which("ssh")
            if not ssh:
                raise RuntimeError("OpenSSH client was not found; Gate proxy is fail-closed")
            command = [
                ssh,
                "-NT",
                "-L", f"{self.local_host}:{self.local_port}:127.0.0.1:{self.remote_port}",
                "-o", "BatchMode=yes",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                self.ssh_host,
            ]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    code = self.process.returncode
                    self.process = None
                    raise RuntimeError(f"Gate SSH tunnel exited during startup (code {code})")
                if self._port_open():
                    return
                time.sleep(0.1)
            self.stop()
            raise RuntimeError("Gate SSH tunnel did not become ready within 12 seconds")

    def stop(self) -> None:
        with self._lock:
            process, self.process = self.process, None
            if not process or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def status(self) -> TunnelStatus:
        running = bool(self.process and self.process.poll() is None and self._port_open())
        return TunnelStatus(
            enabled=self.enabled,
            running=running,
            local_host=self.local_host,
            local_port=self.local_port,
            ssh_host=self.ssh_host,
            remote_port=self.remote_port,
            pid=self.process.pid if running and self.process else None,
        )


gate_tunnel = GateSshTunnel()
