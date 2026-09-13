"""Small cross-platform advisory file-lock helpers."""
from __future__ import annotations
import os

def acquire(handle, non_blocking: bool = False) -> bool:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        handle.write("0")
        handle.flush()
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if non_blocking else msvcrt.LK_LOCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0))
        return True
    except BlockingIOError:
        return False

def release(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError: pass
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
