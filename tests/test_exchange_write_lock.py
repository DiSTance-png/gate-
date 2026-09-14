from __future__ import annotations

import threading

import pytest

from gate_quant.exchange_write_lock import GateWriteBusy, gate_write_lock


def test_gate_write_lock_is_reentrant_in_same_thread(tmp_path):
    path = tmp_path / "gate-write.db"
    with gate_write_lock(path=path):
        with gate_write_lock(path=path):
            pass


def test_gate_write_lock_rejects_parallel_writer_without_sending(tmp_path):
    path = tmp_path / "gate-write.db"
    entered = threading.Event()
    release = threading.Event()

    def owner():
        with gate_write_lock(path=path):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=owner)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(GateWriteBusy, match="当前请求未发送到交易所"):
            with gate_write_lock(path=path, timeout_seconds=0.1):
                pass
    finally:
        release.set()
        thread.join(2)
