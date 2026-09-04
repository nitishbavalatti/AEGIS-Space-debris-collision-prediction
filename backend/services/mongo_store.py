"""
AEGIS MongoDB Persistence Layer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Stores sensor readings (mag + rf + solar + hardware_status, bundled —
matches what app.py's broadcast_sensor_reading() actually sends),
debris snapshots, and ISS positions for persistence across restarts.

Activated when MONGO_URI is set and MongoDB is reachable.
Gracefully degrades to in-memory if unavailable.

CHANGE LOG (this revision):
  - store_* methods log failures at ERROR level (with traceback)
    instead of DEBUG, so write failures are actually visible in normal
    logs instead of vanishing silently.
  - Added last_write_ok_at / last_error, updated on every successful /
    failed write, so the app can report its own persistence health.
  - Added is_healthy() — connected alone doesn't prove writes are
    still succeeding, since it's set once at boot and never rechecked.
    is_healthy() also checks how long it's been since the last
    successful write.
  - Fixed __init__ leaving connected=False forever whenever MONGO_URI
    had no database name in its path: get_database() raised
    ConfigurationError, which fell into the except below and logged as
    an ordinary "in-memory mode" fallback, so every store_* call
    no-opped from then on with no per-write error. Now falls back to
    the "aegis" database explicitly, and logs the resolved database
    name instead of the raw URI (also keeps credentials out of logs).

NOTE (readings not updating — ALL collections at once): sensor_readings
is written from app.py's broadcast_sensor_reading(), on every sensor
tick — that path has nothing to do with tasks.py's debris pipeline or
ISS updater. If sensor_readings, debris_snapshots, AND iss_positions
all went stale at the same moment, there's no single code path in this
repo that connects all three failing together except the backend
process itself stopping (crash, host restart, redeploy without a
process manager/restart policy, etc.) — that's outside what any of
these files can diagnose. Worth checking directly: is app.py's process
actually running right now, and what's the last line in its logs
around when things went stale? If the process IS running and only
Mongo itself is unreachable, is_healthy() / last_error below will show
that instead.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

try:
    from pymongo import MongoClient, ASCENDING
    from pymongo.errors import ConnectionFailure, ConfigurationError
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False
    logger.warning("pymongo not installed — persistence disabled")

DEFAULT_DB_NAME = "aegis"


class MongoStore:
    """
    Thin wrapper around MongoDB for AEGIS data persistence.
    Collections:
      - sensor_readings: time-series of magnetometer + RF + solar data
      - debris_snapshots: periodic snapshots of scored debris
      - iss_positions: ISS position history
    """

    def __init__(self, uri: str = f"mongodb://localhost:27017/{DEFAULT_DB_NAME}"):
        self.client = None
        self.db = None
        self.connected = False

        # Write-health tracking — lets the app tell whether persistence
        # is actually still working, not just whether it connected once
        # at startup.
        self.last_write_ok_at = None
        self.last_error = None

        if not MONGO_AVAILABLE:
            logger.info("[MongoStore] pymongo not available — in-memory mode")
            return

        try:
            self.client = MongoClient(uri, serverSelectionTimeoutMS=3000)
            self.client.admin.command("ping")
            try:
                self.db = self.client.get_database()
            except ConfigurationError:
                # URI had no database name in its path (e.g. Atlas's default
                # "Connect > Drivers" string) — fall back to the known
                # default instead of leaving `connected` False forever.
                self.db = self.client[DEFAULT_DB_NAME]
            self._create_indexes()
            self.connected = True
            logger.info(f"[MongoStore] Connected to MongoDB — database='{self.db.name}'")
        except Exception as e:
            logger.warning(f"[MongoStore] MongoDB unavailable ({e}) — in-memory mode")

    def _create_indexes(self):
        """Create indexes for efficient time-range queries."""
        self.db.sensor_readings.create_index([("timestamp", ASCENDING)])
        self.db.debris_snapshots.create_index([("timestamp", ASCENDING)])
        self.db.iss_positions.create_index([("timestamp", ASCENDING)])

    def _write_ok(self):
        self.last_write_ok_at = datetime.now(timezone.utc)
        self.last_error = None

    def _write_failed(self, context: str, e: Exception):
        self.last_error = str(e)
        logger.error(f"[MongoStore] Failed to store {context}: {e}", exc_info=True)

    def is_healthy(self, max_staleness_minutes: int = 10) -> bool:
        """
        True if Mongo is connected AND writes have actually succeeded
        recently. `connected` alone can't tell you this — it's set once
        at boot and never rechecked, so a store that "connected fine"
        15 days ago but has had every write silently rejected since
        would still report connected == True.
        """
        if not self.connected:
            return False
        if self.last_write_ok_at is None:
            # Nothing attempted yet — not necessarily broken.
            return True
        age = datetime.now(timezone.utc) - self.last_write_ok_at
        return age < timedelta(minutes=max_staleness_minutes)

    # ------------------------------------------------------------------
    # Sensor readings (mag + rf + solar + hardware_status, bundled)
    # ------------------------------------------------------------------
    def store_sensor_reading(self, reading: dict):
        """
        Store a single sensor reading. Called directly from app.py's
        broadcast_sensor_reading() on every tick — matches whatever
        mock_sensor.py / hardware_sensor.py emit as-is (mag, rf, solar,
        hardware_status all bundled in one doc), not filtered here.
        """
        if not self.connected:
            return
        try:
            doc = {**reading, "stored_at": datetime.now(timezone.utc)}
            self.db.sensor_readings.insert_one(doc)
            self._write_ok()
        except Exception as e:
            self._write_failed("sensor reading", e)

    def get_sensor_history(self, hours: int = 24) -> list[dict]:
        """Retrieve recent sensor readings."""
        if not self.connected:
            return []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cursor = self.db.sensor_readings.find(
                {"timestamp": {"$gte": cutoff}}
            ).sort("timestamp", ASCENDING)
            return list(cursor)
        except Exception as e:
            logger.error(f"[MongoStore] Failed to query sensor history: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Debris snapshots
    # ------------------------------------------------------------------
    def store_debris_snapshot(self, debris: list[dict], risk_score: float):
        """Store a full debris snapshot."""
        if not self.connected:
            return
        try:
            doc = {
                "timestamp": datetime.now(timezone.utc),
                "debris_count": len(debris),
                "mean_risk": risk_score,
                "top_10": sorted(
                    debris, key=lambda o: o.get("risk_score", 0), reverse=True
                )[:10],
            }
            self.db.debris_snapshots.insert_one(doc)
            self._write_ok()
        except Exception as e:
            self._write_failed("debris snapshot", e)

    def get_risk_history(self, hours: int = 48) -> list[tuple[str, float]]:
        """
        Retrieve (timestamp, mean_risk) pairs from debris_snapshots for the
        Solar Correlation panel's risk-score line. This is what makes that
        line survive a server restart — previously it lived only in an
        in-memory deque (backend/services/risk_history.py) that got wiped
        on every process restart, even though every pipeline run was
        ALREADY being persisted here via store_debris_snapshot(). The data
        existed the whole time; nothing was reading it back out for the
        chart.

        Projects out `top_10` (each entry is a full scored debris object —
        10 of them per snapshot adds up fast and the chart doesn't need
        them) to keep this query cheap even over a wide window.

        Returns ascending list of (iso_timestamp_str, risk_score). The
        timestamp format intentionally matches NOAA's geomag time_tag
        precision (whole seconds, no microseconds, no offset suffix) so
        it's directly string-comparable against geomag "time_tag" values
        in solar.py's nearest-lookup — mixing formats (e.g. leaving in
        stored_at's UTC offset or microseconds) still sorts correctly
        lexicographically since the date/time digits that matter come
        first, but matching precision avoids relying on that subtlety.
        """
        if not self.connected:
            return []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cursor = self.db.debris_snapshots.find(
                {"timestamp": {"$gte": cutoff}},
                {"timestamp": 1, "mean_risk": 1, "_id": 0},
            ).sort("timestamp", ASCENDING)

            out = []
            for doc in cursor:
                ts = doc.get("timestamp")
                risk = doc.get("mean_risk")
                if ts is None or risk is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ts_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    out.append((ts_str, float(risk)))
                except (TypeError, ValueError):
                    continue
            return out
        except Exception as e:
            logger.error(f"[MongoStore] Failed to query risk history: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # ISS position
    # ------------------------------------------------------------------
    def store_iss_position(self, pos: dict):
        """Store ISS position."""
        if not self.connected:
            return
        try:
            doc = {**pos, "stored_at": datetime.now(timezone.utc)}
            self.db.iss_positions.insert_one(doc)
            self._write_ok()
        except Exception as e:
            self._write_failed("ISS position", e)

    def close(self):
        if self.client:
            self.client.close()
            self.connected = False