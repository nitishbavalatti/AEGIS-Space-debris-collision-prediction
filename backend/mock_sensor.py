"""
AEGIS Mock Sensor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Simulates the RTL-SDR RF noise floor only.

The magnetometer signal previously simulated here has been replaced by
real NOAA SWPC (DSCOVR) interplanetary field data — see
backend/services/noaa_swpc.py:get_geomag_realtime(). This mock now only
covers RF, for when no RTL-SDR dongle is attached.

When hardware arrives:
  → Run rtlsdr_monitor.py alongside the backend
  → Set USE_MOCK_SENSOR=false in .env
  → This file is no longer used — everything else stays the same.

The mock emits to the same WebSocket endpoint the real RTL-SDR uses.
Occasional solar storm spikes are injected to demonstrate the
sensor→GNN correlation graph on the dashboard.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import time
import math
import random
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ─── RTL-SDR baseline RF noise floor ─────────────────────────────────────────
BASELINE_RF_NOISE = -92.0   # dBm  (quiet ionosphere)
RF_NOISE_RANGE = 8.0         # ±8 dBm normal variation


class MockSensor:
    """
    Produces realistic RF sensor readings every 30 seconds.
    Injects solar storm events probabilistically to drive the correlation demo.
    """

    def __init__(self, emit_callback):
        """
        emit_callback(data: dict) — called every reading interval.
        In production this is replaced by real RTL-SDR/MQTT data.
        """
        self.emit = emit_callback
        self._running = False
        self._thread = None

        # Storm state
        self._storm_active = False
        self._storm_intensity = 0.0   # 0-1
        self._storm_remaining_ticks = 0
        self._tick = 0

        # Historical readings buffer (for the correlation graph)
        self.history: list[dict] = []
        self._max_history = 2880  # 24h at 30s intervals

    def _inject_storm(self):
        """Randomly trigger a simulated solar storm. ~3% chance per tick."""
        if not self._storm_active and random.random() < 0.03:
            self._storm_active = True
            self._storm_intensity = random.uniform(0.4, 1.0)
            duration_minutes = random.uniform(30, 180)
            self._storm_remaining_ticks = int(duration_minutes * 60 / 30)
            logger.info(
                f"[MOCK] Solar storm injected — intensity={self._storm_intensity:.2f}, "
                f"duration={duration_minutes:.0f}min"
            )

    def _storm_envelope(self) -> float:
        """Returns current storm multiplier 0-1, with smooth rise/decay."""
        if not self._storm_active:
            return 0.0
        ramp_up = min(self._tick % 20, 10) / 10.0
        return self._storm_intensity * ramp_up

    def _generate_reading(self) -> dict:
        storm = self._storm_envelope()
        diurnal = 1.5 * math.sin(2 * math.pi * self._tick / 2880)  # 24h cycle

        # ─── RTL-SDR RF Noise ─────────────────────────────────────────────────
        # Solar storms increase ionospheric absorption → RF noise floor rises
        rf_storm_delta = storm * random.uniform(12, 28)   # dBm increase
        rf_noise = (
            BASELINE_RF_NOISE
            + random.gauss(0, 1.5)
            + rf_storm_delta
            + diurnal * 0.5
        )

        # ─── Derived indicators ───────────────────────────────────────────────
        kp_proxy = min(9.0, storm * 8.5 + random.uniform(0, 1.5))
        storm_alert = kp_proxy >= 5.0

        reading = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tick": self._tick,
            "source": "mock",  # ← becomes "rtlsdr" when hardware connected

            # RF data
            "rf": {
                "noise_dbm": float(round(rf_noise, 2)),
                "baseline_dbfs": float(BASELINE_RF_NOISE),
                "delta_dbfs": float(round(rf_noise - BASELINE_RF_NOISE, 3)),
                "storm_detected": storm_alert,
                "alert_level": (
                    "SEVERE" if kp_proxy >= 8 else
                    "STRONG" if kp_proxy >= 6 else
                    "MODERATE" if kp_proxy >= 5 else
                    "MINOR" if kp_proxy >= 4 else
                    "NONE"
                ),
            },

            # Per-frequency RF breakdown (NOAA APT frequencies)
            "per_frequency": {
                "137.620MHz": float(round(rf_noise - random.uniform(0.5, 2.0), 2)),
                "137.912MHz": float(round(rf_noise - random.uniform(1.0, 3.0), 2)),
                "138.098MHz": float(round(rf_noise - random.uniform(0.2, 1.5), 2)),
            },

            # Solar storm assessment (kp_proxy here is RF-derived, not from
            # a fake magnetometer — real Kp/IMF now comes from NOAA SWPC)
            "solar": {
                "storm_active": storm_alert,
                "intensity": round(storm * self._storm_intensity if self._storm_active else 0.0, 3),
                "kp_proxy": round(kp_proxy, 2),
                "alert_level": (
                    "SEVERE" if kp_proxy >= 8 else
                    "STRONG" if kp_proxy >= 6 else
                    "MODERATE" if kp_proxy >= 5 else
                    "MINOR" if kp_proxy >= 4 else
                    "NONE"
                ),
            },
        }

        return reading

    def _tick_loop(self):
        while self._running:
            self._inject_storm()
            reading = self._generate_reading()

            if self._storm_active:
                self._storm_remaining_ticks -= 1
                if self._storm_remaining_ticks <= 0:
                    self._storm_active = False
                    self._storm_intensity = 0.0
                    logger.info("[MOCK] Solar storm subsided")

            self._tick += 1

            self.history.append(reading)
            if len(self.history) > self._max_history:
                self.history = self.history[-self._max_history:]

            self.emit(reading)
            time.sleep(30)  # Match RTL-SDR/ESP32 30s reporting interval

    def start(self):
        if self._running:
            return

        logger.info("[MOCK SENSOR] Pre-seeding history with 1h of data...")
        for i in range(120):  # 120 * 30s = 1 hour
            self._tick = i
            self._inject_storm()
            reading = self._generate_reading()
            self.history.append(reading)
            if self._storm_active:
                self._storm_remaining_ticks -= 1
                if self._storm_remaining_ticks <= 0:
                    self._storm_active = False

        self._tick = 120
        logger.info(f"[MOCK SENSOR] History pre-seeded with {len(self.history)} readings")

        self._running = True
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()
        logger.info("[MOCK SENSOR] Started — emitting every 30s")

    def stop(self):
        self._running = False
        logger.info("[MOCK SENSOR] Stopped")

    def get_history(self, hours: int = 24) -> list[dict]:
        ticks = min(hours * 120, len(self.history))
        return self.history[-ticks:]

    def get_latest(self) -> dict | None:
        return self.history[-1] if self.history else None