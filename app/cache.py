"""Redis cache. If Redis is not running, an in-process cache is used so the app still works."""
import hashlib
import json
import time

from . import config

try:
    import redis
except ImportError:  # redis package not installed
    redis = None


class Cache:
    def __init__(self):
        self._mem = {}
        self._r = None
        if redis:
            try:
                r = redis.Redis.from_url(
                    config.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5, decode_responses=True
                )
                r.ping()
                self._r = r
            except Exception:
                self._r = None

    @property
    def backend(self):
        return "redis" if self._r else "memory"

    def get(self, key):
        if self._r:
            try:
                v = self._r.get(key)
                return json.loads(v) if v else None
            except Exception:
                pass
        item = self._mem.get(key)
        if item and item[0] > time.time():
            return item[1]
        return None

    def set(self, key, value, ttl=300):
        if self._r:
            try:
                self._r.setex(key, ttl, json.dumps(value))
                return
            except Exception:
                pass
        self._mem[key] = (time.time() + ttl, value)

    def delete_prefix(self, prefix):
        if self._r:
            try:
                for k in self._r.scan_iter(match=prefix + "*"):
                    self._r.delete(k)
            except Exception:
                pass
        for k in [k for k in self._mem if k.startswith(prefix)]:
            del self._mem[k]


cache = Cache()


def digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]

