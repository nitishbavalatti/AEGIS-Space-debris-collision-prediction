"""
AEGIS Simple In-Memory Cache
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lightweight TTL cache for external API responses.
Prevents rate-limiting Space-Track, NOAA SWPC, and N2YO.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import time
import threading
import logging

logger = logging.getLogger(__name__)


class Cache:
    """Thread-safe TTL cache."""

    def __init__(self, default_ttl: int = 300):
        self._store = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl

    def get(self, key: str):
        with self._lock:
            if key not in self._store:
                return None
            entry = self._store[key]
            if time.time() > entry["expires_at"]:
                del self._store[key]
                logger.debug(f"[Cache] Expired: {key}")
                return None
            logger.debug(f"[Cache] Hit: {key}")
            return entry["value"]

    def set(self, key: str, value, ttl: int = None):
        ttl = ttl or self.default_ttl
        with self._lock:
            self._store[key] = {
                "value": value,
                "expires_at": time.time() + ttl,
            }
            logger.debug(f"[Cache] Set: {key} (TTL={ttl}s)")

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

    def cleanup(self):
        """Remove all expired entries."""
        with self._lock:
            now = time.time()
            expired = [k for k, v in self._store.items() if now > v["expires_at"]]
            for k in expired:
                del self._store[k]
            if expired:
                logger.debug(f"[Cache] Cleaned up {len(expired)} expired entries")


# Global cache instance
cache = Cache()

# Periodic cleanup
def _start_cleanup_thread():
    def cleanup_loop():
        while True:
            time.sleep(600)  # Every 10 minutes
            cache.cleanup()
    t = threading.Thread(target=cleanup_loop, daemon=True)
    t.start()

_start_cleanup_thread()
