"""Passwords, session tokens, delivery OTPs and a small rate limiter. Standard library only."""
import hashlib
import hmac
import os
import secrets
import threading
import time

from . import config

_ITER = 200_000
_secret_cache = None


def _secret():
    """Server secret used to derive delivery OTPs. Created once and kept in data/secret.key."""
    global _secret_cache
    if _secret_cache is None:
        p = config.DATA_DIR / "secret.key"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(secrets.token_bytes(32))
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        _secret_cache = p.read_bytes()
    return _secret_cache


# ------------------------------------------------------------ passwords
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER)
    return f"pbkdf2${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        _, it, salt, want = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


_DUMMY = hash_password("not-a-real-password")


def burn_time(password):
    """Spend the same time as a real check so a login for an unknown phone number is not faster."""
    verify_password(password, _DUMMY)


# ------------------------------------------------------------ sessions
def new_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ delivery OTP
def otp_for(order_id, nonce):
    """6-digit OTP derived from the server secret. Nothing readable is stored in the database."""
    mac = hmac.new(_secret(), f"otp:{order_id}:{nonce}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:4], 'big') % 1_000_000:06d}"


def check_otp(order_id, nonce, given):
    if not nonce:
        return False
    return hmac.compare_digest(otp_for(order_id, nonce), str(given).strip())


def new_nonce():
    return secrets.token_hex(8)


# ------------------------------------------------------------ rate limiting
class RateLimiter:
    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def _recent(self, key, window):
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < window]
        self._hits[key] = hits
        return hits

    def blocked(self, key, limit, window):
        with self._lock:
            return len(self._recent(key, window)) >= limit

    def hit(self, key, window):
        with self._lock:
            self._recent(key, window).append(time.time())

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


limiter = RateLimiter()

