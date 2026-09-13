from __future__ import annotations
import hashlib
import hmac
import time


def gate_signature(secret: str, method: str, path: str, query: str = "", body: str = "", timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp or int(time.time()))
    body_hash = hashlib.sha512(body.encode()).hexdigest()
    payload = "\n".join([method.upper(), path, query, body_hash, ts])
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest(), ts
