"""Version counters behind the live updates. The browser listens on /events and reloads when a counter changes."""
import threading

_lock = threading.Lock()
_versions = {"products": 0, "orders": 0}


def bump(*kinds):
    with _lock:
        for k in kinds:
            _versions[k] = _versions.get(k, 0) + 1


def snapshot():
    with _lock:
        return dict(_versions)

