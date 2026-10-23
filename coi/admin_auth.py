"""Admin sign-in: scrypt password hash (ADMIN_PASSWORD_HASH), failed-attempt lockout, session lifetime.

Only the Admin page uses this; every other page is open. It fails closed: with no valid hash configured, nobody can sign in.
Create a hash:  python -m coi.admin_auth          (prompts for a password)
                printf '%s' "$PW" | python -m coi.admin_auth --stdin
Stored format (no '$', so Docker Compose / dotenv never try to expand it):  scrypt:N:r:p:salt_hex:hash_hex
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
import threading

N, R, P = 2 ** 15, 8, 1
MIN_LENGTH = 12
SESSION_SECONDS = 30 * 60      # sliding: extended on each interaction
MAX_FAILURES = 5               # failures within FAILURE_WINDOW (counted across ALL sessions) trigger a lockout
FAILURE_WINDOW = 15 * 60
LOCKOUT_SECONDS = 5 * 60


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * n * r * 2 + 1024 * 1024, dklen=32)


def hash_password(password: str, n: int = N) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt:{n}:{R}:{P}:{salt.hex()}:{_scrypt(password, salt, n, R, P).hex()}"


def _parse(stored: str):
    try:
        scheme, n, r, p, salt, digest = (stored or "").strip().split(":")
        if scheme != "scrypt":
            return None
        return int(n), int(r), int(p), bytes.fromhex(salt), bytes.fromhex(digest)
    except ValueError:
        return None


def is_configured(stored: str) -> bool:
    return _parse(stored) is not None


def verify_password(password: str, stored: str) -> bool:
    parsed = _parse(stored)
    if parsed is None or not password:
        return False
    n, r, p, salt, digest = parsed
    try:
        return hmac.compare_digest(_scrypt(password, salt, n, r, p), digest)
    except (ValueError, MemoryError):
        return False


class Throttle:
    """Process-wide failed-login counter: shared by every browser session, so many tabs don't multiply the guesses."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self._locked_until = 0.0

    def locked_for(self, now: float) -> int:
        """Seconds left on the lockout (0 = not locked)."""
        with self._lock:
            return max(0, int(self._locked_until - now + 0.999))

    def record_failure(self, now: float) -> None:
        with self._lock:
            self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW] + [now]
            if len(self._failures) >= MAX_FAILURES:
                self._locked_until = now + LOCKOUT_SECONDS
                self._failures = []

    def record_success(self) -> None:
        with self._lock:
            self._failures = []
            self._locked_until = 0.0


THROTTLE = Throttle()


def _main(argv: list[str]) -> int:
    if "--stdin" in argv:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        import getpass
        password = getpass.getpass("New admin password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < MIN_LENGTH:
        print(f"Password must be at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
