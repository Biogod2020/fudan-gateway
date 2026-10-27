"""Short-lived, single-use request signatures; the relay key never crosses the wire."""

import hashlib
import hmac
import secrets
import time

HEADER = "X-Fudan-Gateway-Auth"


def signature(key: bytes, method: str, path: str, timestamp: int, nonce: str) -> str:
    payload = f"v1\n{timestamp}\n{nonce}\n{method.upper()}\n{path}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def sign_request(key: bytes, method: str, path: str) -> str:
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    return f"v1:{timestamp}:{nonce}:{signature(key, method, path, timestamp, nonce)}"


class RequestVerifier:
    def __init__(self, key: bytes, window: int = 60):
        if len(key) < 32:
            raise ValueError("Relay keys must contain at least 32 bytes.")
        self.key = key
        self.window = window
        self.used: dict[str, int] = {}

    def verify(self, header: str, method: str, path: str) -> bool:
        try:
            version, raw_time, nonce, supplied = header.split(":")
            timestamp = int(raw_time)
            now = int(time.time())
            if version != "v1" or len(nonce) != 32 or abs(now - timestamp) > self.window:
                return False
            bytes.fromhex(nonce)
            expected = signature(self.key, method, path, timestamp, nonce)
            if not hmac.compare_digest(expected, supplied):
                return False
            self.used = {n: t for n, t in self.used.items() if t >= now - self.window}
            if nonce in self.used or len(self.used) >= 10000:
                return False
            self.used[nonce] = timestamp
            return True
        except (ValueError, TypeError):
            return False
