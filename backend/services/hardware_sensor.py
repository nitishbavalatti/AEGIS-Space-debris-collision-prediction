"""
AEGIS Hardware Sensor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces mock_sensor.py when real hardware is connected.

Listens to two MQTT topics:
  aegis/sensors  ← ESP32 publishes HMC5883L magnetometer data
  aegis/rtlsdr   ← rtlsdr_monitor.py publishes RF noise data

Merges both streams into the same reading format that
mock_sensor.py produces, so the rest of the backend
never knows the difference.

MAGNETOMETER FALLBACK (temporary, until ESP32/HMC5883L is fixed):
  If no ESP32 MQTT message has arrived recently, this module polls
  the USGS Geomagnetism Web Service (real ground-observatory data,
  no API key required) and emits readings in the exact same
  mag={bx, by, bz, total, declination_deg} schema. The moment real
  ESP32 messages resume, the fallback stops overwriting the feed.

To activate real hardware:
  Set USE_MOCK_SENSOR=false in .env
  Make sure mosquitto is running
  Make sure ESP32 is flashed and online
  Make sure rtlsdr_monitor.py is running (for RF data)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import logging
import math
import os
import re
import threading
import time
import numpy as np
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _parse_kp_value(raw) -> float:
    """
    NOAA's planetary_k_index_1m.json sometimes returns kp as a string with
    a trailing data-quality flag letter (e.g. '0P', '3Z') instead of a plain
    number — a bare float() cast throws on these. Strip to the leading
    numeric portion instead of silently dropping to 0.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.match(r"^-?\d+(\.\d+)?", str(raw).strip())
    return float(match.group(0)) if match else 0.0


# ─── USGS Geomagnetism fallback config ───────────────────────────────────────
# No API key needed. Observatory codes: BOU, CMO, FRN, HON, SJG, etc.
# (No India station is available on this network; this is real ground-truth
# geomagnetic data, just not local to Bengaluru — fine for a demo/fallback.)
USGS_OBSERVATORY = os.getenv("USGS_OBSERVATORY", "BOU")
USGS_URL = "https://geomag.usgs.gov/ws/data/"
USGS_POLL_SECONDS = 60          # USGS updates ~once/minute
ESP32_STALE_AFTER_SECONDS = 90  # if no ESP32 msg in this long, use fallback


MAG_TOTAL_PLAUSIBLE_MIN = 15.0   # µT — real Earth field is ~20-65µT anywhere on the planet
MAG_TOTAL_PLAUSIBLE_MAX = 100.0  # µT — generous upper bound


class HardwareSensor:
    """
    MQTT subscriber that merges ESP32 + RTL-SDR streams.
    Exposes the same interface as MockSensor so the Flask
    app doesn't need any changes when switching modes.
    """

    def __init__(self, emit_callback, broker: str = "localhost", port: int = 1883):
        self.emit = emit_callback
        self.broker = broker
        self.port = port
        self._running = False

        # Latest readings from each source
        self._latest_mag   = None   # From ESP32
        self._latest_rf    = None   # From RTL-SDR

        self.history: list[dict] = []
        self._max_history = 2880

        # Status
        self.esp32_connected = False
        self.rtlsdr_connected = False
        self._last_esp32_msg = 0
        self._last_rf_msg = 0
        self._mag_backfilled = False

        # Initialize RTL-SDR (optional — gracefully degrade if unavailable)
        self.sdr = None
        try:
            from rtlsdr import RtlSdr
            logger.info("[HW Sensor] Scanning for RTL-SDR devices...")

            num_devices = RtlSdr.get_num_gain_values(0) if hasattr(RtlSdr, 'get_num_gain_values') else None
            self.sdr = RtlSdr()
            self.sdr.sample_rate = 2.4e6
            self.sdr.center_freq = 137.5e6  # NOAA band
            self.sdr.gain = 30  # manual gain (important)
            logger.info("[HW Sensor] ✅ RTL-SDR initialized successfully - device is connected")
            self.rtlsdr_connected = True
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning(f"[HW Sensor] RTL-SDR Python package unavailable: {e}")
            logger.info("[HW Sensor] → Install rtlsdr and ensure librtlsdr is available on the OS")
            self.sdr = None
            self.rtlsdr_connected = False
        except FileNotFoundError as e:
            logger.critical(f"[HW Sensor] ❌ RTL-SDR device NOT FOUND: {e}")
            logger.info("[HW Sensor] → Ensure RTL-SDR dongle is plugged into USB and drivers are installed")
            self.sdr = None
        except PermissionError as e:
            logger.critical(f"[HW Sensor] ❌ RTL-SDR permission denied: {e}")
            logger.info("[HW Sensor] → On Windows, ensure no other app (SDR#, GQRX) is using the device")
            self.sdr = None
        except Exception as e:
            logger.critical(f"[HW Sensor] ❌ RTL-SDR initialization failed: {e}")
            logger.info(f"[HW Sensor] → Error type: {type(e).__name__}")
            self.sdr = None

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info(f"[HW Sensor] MQTT connected to {self.broker}:{self.port}")
            client.subscribe("aegis/sensors")
            client.subscribe("aegis/rtlsdr")
        else:
            logger.error(f"[HW Sensor] MQTT connection failed rc={rc}")

    def _is_plausible_mag(self, mag: dict) -> bool:
        """
        Rejects obviously-glitched ESP32/HMC5883L readings (e.g. the known
        I2C dropout producing total≈0) so a single bad packet can't briefly
        override good USGS fallback data on the chart.
        """
        try:
            total = float(mag.get("total", 0))
        except (TypeError, ValueError):
            return False
        return MAG_TOTAL_PLAUSIBLE_MIN <= total <= MAG_TOTAL_PLAUSIBLE_MAX

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            topic = msg.topic

            if topic == "aegis/sensors":
                # ESP32 message — has mag data. Validate before trusting it:
                # a known I2C dropout occasionally produces a single garbage
                # packet (e.g. total≈0), which would otherwise briefly
                # override good USGS fallback data on the chart.
                mag = data.get("mag", {})
                if not self._is_plausible_mag(mag):
                    logger.warning(
                        f"[HW Sensor] Rejected implausible ESP32 mag reading "
                        f"(total={mag.get('total')}) — likely I2C glitch, ignoring"
                    )
                    return

                self._latest_mag = data
                self._last_esp32_msg = time.time()
                self.esp32_connected = True
                # Merge with latest RF if available, then emit
                merged = self._merge_reading(data)
                self._store_and_emit(merged)

            elif topic == "aegis/rtlsdr":
                # RTL-SDR message — has RF data
                self._latest_rf = data
                self._last_rf_msg = time.time()
                self.rtlsdr_connected = True
                # Update latest merged reading's RF section
                if self.history:
                    self.history[-1]["rf"].update(data.get("rf", {}))

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"[HW Sensor] Message parse error: {e}")

    def _merge_reading(self, esp32_data: dict) -> dict:
        """
        Merges ESP32 magnetometer data with latest RTL-SDR RF data.
        Falls back to placeholder RF values if RTL-SDR not connected.
        """
        rf_data = {}
        if self._latest_rf:
            rf_data = self._latest_rf.get("rf", {})
        else:
            # RTL-SDR not running — use placeholder
            rf_data = {
                "noise_dbm": -92.0,
                "baseline_dbm": -92.0,
                "delta_dbm": 0.0,
                "noise_dbfs": -45.0,
                "baseline_dbfs": -45.0,
                "delta_dbfs": 0.0,
                "storm_detected": False,
                "alert_level": "NONE",
                "note": "rtlsdr_monitor.py not running",
            }

        # Build merged reading in same schema as MockSensor
        merged = {
            "timestamp": esp32_data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "tick": esp32_data.get("tick", 0),
            "source": "esp32",  # Real hardware
            "mag": esp32_data.get("mag", {}),
            "rf": rf_data,
            "solar": self._get_real_kp(),
            "hardware_status": {
                "esp32": self.esp32_connected,
                "rtlsdr": self.rtlsdr_connected,
            },
        }
        return merged

    def _store_and_emit(self, reading: dict):
        self.history.append(reading)
        if len(self.history) > self._max_history:
            self.history = self.history[-self._max_history:]
        self.emit(reading)

    def _watchdog(self):
        """Log warnings if hardware goes silent."""
        while self._running:
            now = time.time()
            if self.esp32_connected and self._last_esp32_msg and (now - self._last_esp32_msg) > 120:
                logger.warning("[HW Sensor] ESP32 silent for >2 min — check connection")
                self.esp32_connected = False
            if self.rtlsdr_connected and self._last_rf_msg and (now - self._last_rf_msg) > 120:
                logger.warning("[HW Sensor] RTL-SDR silent for >2 min — check rtlsdr_monitor.py")
                self.rtlsdr_connected = False
            time.sleep(30)

    # 🔥 RTL-SDR NOISE FUNCTION
    def _disable_rtlsdr(self, reason: str):
        if self.sdr:
            try:
                self.sdr.close()
            except Exception:
                pass
        self.sdr = None
        self.rtlsdr_connected = False
        logger.warning(f"[HW Sensor] RTL-SDR disabled after error: {reason}")

    def _get_rf_noise(self):
        if not self.sdr:
            # Device not available - this is expected if RTL-SDR is not connected
            return -92.0

        try:
            samples = self.sdr.read_samples(64 * 1024)

            power = np.abs(samples) ** 2
            mean_power = np.mean(power)

            db = 10 * np.log10(mean_power + 1e-12)
            return float(db)

        except Exception as e:
            logger.error(f"[RTL-SDR] read_samples error: {e}")
            self._disable_rtlsdr(str(e))
            return -92.0

    # 🔥 RTL-SDR PROCESSOR
    def _read_rtl(self):
        BASELINE_DBFS = -45.0

        noise_dbfs = self._get_rf_noise()
        delta_dbfs = noise_dbfs - BASELINE_DBFS

        if delta_dbfs > 24:
            alert = "SEVERE"
        elif delta_dbfs > 18:
            alert = "STRONG"
        elif delta_dbfs > 12:
            alert = "MODERATE"
        elif delta_dbfs > 8:
            alert = "MINOR"
        else:
            alert = "NONE"

        storm_detected = bool(delta_dbfs > 8)
        data_source = "REAL" if self.sdr else "PLACEHOLDER (device not connected)"

        rf_data = {
            "noise_dbm": float(noise_dbfs),
            "noise_dbfs": float(noise_dbfs),
            "baseline_dbfs": float(BASELINE_DBFS),
            "delta_dbfs": float(delta_dbfs),
            "storm_detected": storm_detected,
            "alert_level": alert,
            "data_source": data_source,
        }

        # Only log when not using real data (to avoid spam)
        if not self.sdr:
            logger.debug(f"[RTL-SDR] Using placeholder - noise_dbfs={noise_dbfs:.1f}dBm (device not connected)")

        return rf_data

    # 🌍 USGS MAGNETOMETER FALLBACK (temporary, until ESP32/HMC5883L is fixed)
    def _get_external_mag_field(self) -> dict | None:
        """
        Polls USGS's real-time geomagnetic observatory data and returns
        a reading in the exact same schema mock_sensor.py / ESP32 use:
        {bx, by, bz, total, declination_deg} in microtesla.
        Returns None on any failure so the caller can skip this tick.
        """
        try:
            params = {
                "id": USGS_OBSERVATORY,
                "elements": "X,Y,Z,F",
                "format": "json",
            }
            resp = requests.get(USGS_URL, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            # Real schema: data["values"] is a list of series, each with
            # metadata.element (e.g. "X") and a parallel "values" array
            # aligned to the top-level "times" array. Recent samples can be
            # null (data gap), so walk backward to find the latest real one.
            channels = {}
            for series in data.get("values", []):
                element = series.get("metadata", {}).get("element")
                samples = series.get("values", [])
                last_valid = next((v for v in reversed(samples) if v is not None), None)
                if element and last_valid is not None:
                    channels[element] = last_valid

            if not all(k in channels for k in ("X", "Y", "Z", "F")):
                logger.warning(f"[USGS Fallback] incomplete response, got channels: {list(channels.keys())}")
                return None

            bx_nt, by_nt, bz_nt, total_nt = (
                channels["X"], channels["Y"], channels["Z"], channels["F"]
            )

            # USGS reports in nanotesla; mock/ESP32 schema is microtesla
            bx, by, bz, total = (v / 1000.0 for v in (bx_nt, by_nt, bz_nt, total_nt))
            declination = math.degrees(math.atan2(by, bx))

            return {
                "bx": round(bx, 3),
                "by": round(by, 3),
                "bz": round(bz, 3),
                "total": round(total, 3),
                "declination_deg": round(declination, 2),
            }
        except Exception as e:
            logger.error(f"[USGS Fallback] fetch failed: {e}")
            return None

    def _get_real_kp(self) -> dict:
        """
        Real Kp index from NOAA SWPC (already used by routes/solar.py),
        reshaped into the same solar={storm_active, intensity, kp_proxy,
        alert_level} schema mock_sensor.py produces. Falls back to a
        quiet/NONE reading on any failure — never blocks the mag tick.
        get_kp_history() is already cached (30 min TTL) so polling this
        every 60s costs nothing extra.
        """
        try:
            from backend.services.noaa_swpc import get_kp_history, _kp_to_level

            events, ok = get_kp_history(days_back=1)
            if not ok or not events:
                return {"storm_active": False, "intensity": 0.0, "kp_proxy": 0.0, "alert_level": "NONE"}

            latest = events[0]  # get_kp_history() sorts most-recent-first
            kp = _parse_kp_value(latest.get("kp_index") or latest.get("kp") or latest.get("kpIndex"))
            level = _kp_to_level(kp)

            return {
                "storm_active": level != "NONE",
                "intensity": round(min(kp / 9.0, 1.0), 3),
                "kp_proxy": round(kp, 2),
                "alert_level": level,
            }
        except Exception as e:
            logger.error(f"[USGS Fallback] NOAA Kp fetch failed: {e}")
            return {"storm_active": False, "intensity": 0.0, "kp_proxy": 0.0, "alert_level": "NONE"}

    def _backfill_external_mag(self, mag: dict, rf_data: dict, solar_data: dict,
                                count: int = 60, interval_s: int = 60):
        """
        One-time seed of `count` historical points stepping backward from now,
        using the current real reading. Real ground-station field barely
        changes over an hour, so this is a faithful backfill (not synthetic
        noise) — it just stops the chart from drawing a straight ramp between
        a stale '0' point and the first live reading, the same problem
        MockSensor avoids by pre-seeding history on start().
        """
        now = datetime.now(timezone.utc)
        backfilled = []
        for i in range(count, 0, -1):
            ts = now - timedelta(seconds=i * interval_s)
            backfilled.append({
                "timestamp": ts.isoformat(),
                "tick": int(ts.timestamp()),
                "source": f"usgs_{USGS_OBSERVATORY}_backfill",
                "mag": mag,
                "rf": rf_data,
                "solar": solar_data,
                "hardware_status": {"esp32": False, "rtlsdr": self.rtlsdr_connected},
            })
        # Prepend — don't emit these over the socket, just seed history so
        # /api/solar/correlation has a sane window immediately.
        self.history = backfilled + self.history
        if len(self.history) > self._max_history:
            self.history = self.history[-self._max_history:]
        logger.info(f"[USGS Fallback] Backfilled {count} historical points ({USGS_OBSERVATORY})")

    def _external_mag_loop(self):
        """
        Runs continuously. Only actually emits a reading when the ESP32
        feed has gone stale — so it never fights with real hardware data.
        """
        while self._running:
            stale = (
                not self.esp32_connected
                or not self._last_esp32_msg
                or (time.time() - self._last_esp32_msg) > ESP32_STALE_AFTER_SECONDS
            )

            if stale:
                mag = self._get_external_mag_field()
                if mag:
                    rf_data = (self._latest_rf.get("rf", {}) if self._latest_rf else {
                        "noise_dbm": -92.0,
                        "baseline_dbfs": -92.0,
                        "delta_dbfs": 0.0,
                        "storm_detected": False,
                        "alert_level": "NONE",
                        "note": "rtlsdr_monitor.py not running",
                    })
                    solar_data = self._get_real_kp()

                    if not self._mag_backfilled and len(self.history) < 5:
                        self._backfill_external_mag(mag, rf_data, solar_data)
                        self._mag_backfilled = True

                    reading = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "tick": int(time.time()),
                        "source": f"usgs_{USGS_OBSERVATORY}",  # ← flags this as fallback data
                        "mag": mag,
                        "rf": rf_data,
                        "solar": solar_data,
                        "hardware_status": {
                            "esp32": False,
                            "rtlsdr": self.rtlsdr_connected,
                        },
                    }
                    self._store_and_emit(reading)
                    logger.info(
                        f"[USGS Fallback] mag_total={mag['total']:.2f}µT "
                        f"kp={reading['solar']['kp_proxy']} "
                        f"(observatory={USGS_OBSERVATORY}, ESP32 unavailable)"
                    )

            time.sleep(USGS_POLL_SECONDS)

    def start(self):
        mqtt_ok = False
        try:
            import paho.mqtt.client as mqtt_client

            client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id="aegis-backend")
            client.on_connect = self._on_connect
            client.on_message = self._on_message

            client.connect(self.broker, self.port, 60)
            mqtt_ok = True
        except ImportError:
            logger.error("paho-mqtt not installed — ESP32/MQTT input disabled, continuing without it")
        except Exception as e:
            logger.warning(
                f"[HW Sensor] MQTT broker unreachable ({e}) — no ESP32 connected, "
                f"continuing with USGS fallback + RTL-SDR only"
            )

        self._running = True

        if mqtt_ok:
            # MQTT thread — only if a broker was actually reachable
            t1 = threading.Thread(target=client.loop_forever, daemon=True)
            t1.start()
        else:
            logger.info("[HW Sensor] Running without MQTT/ESP32 — USGS fallback will be the sole mag source")

        # Watchdog thread
        t2 = threading.Thread(target=self._watchdog, daemon=True)
        t2.start()

        # USGS magnetometer fallback thread — only emits while ESP32 is stale
        t4 = threading.Thread(target=self._external_mag_loop, daemon=True)
        t4.start()
        logger.info(
            f"[HW Sensor] USGS magnetometer fallback armed (observatory={USGS_OBSERVATORY}, "
            f"activates if ESP32 silent > {ESP32_STALE_AFTER_SECONDS}s)"
        )

        # RTL-SDR loop (only if device is available)
        if self.sdr:
            def rtl_loop():
                while self._running:
                    rf_data = self._read_rtl()

                    if not self.sdr:
                        logger.warning("[HW Sensor] RTL-SDR thread stopping because device is disabled")
                        break

                    self._latest_rf = {"rf": rf_data}
                    self._last_rf_msg = time.time()
                    self.rtlsdr_connected = True

                    if self.history:
                        self.history[-1]["rf"] = rf_data
                        self.emit(self.history[-1])
                    else:
                        # No mag reading yet (ESP32 not connected, USGS
                        # fallback hasn't ticked yet) — don't fabricate a
                        # reading with empty mag data, that bakes a fake
                        # zero point into the chart at whatever moment this
                        # thread happens to race the USGS backfill on
                        # startup. Just cache the RF value; _merge_reading()
                        # and _external_mag_loop() already pick it up via
                        # self._latest_rf on the next real mag tick.
                        logger.debug(
                            "[HW Sensor] RTL-SDR reading cached — waiting on first "
                            "mag reading (ESP32 or USGS fallback) before emitting"
                        )

                    logger.info(
                        f"[RTL-SDR] noise={rf_data['noise_dbm']:.2f} "
                        f"delta={rf_data['delta_dbfs']:.2f} alert={rf_data['alert_level']}"
                    )

                    time.sleep(1)

            t3 = threading.Thread(target=rtl_loop, daemon=True)
            t3.start()
            logger.info("[HW Sensor] ✅ RTL-SDR thread started - reading RF data from device")
        else:
            # Device not available - can still use ESP32 magnetometer via MQTT
            logger.warning("[HW Sensor] ⚠️  RTL-SDR DISABLED - will use MQTT/ESP32 data only")
            logger.warning("[HW Sensor] To enable RTL-SDR:")
            logger.warning("[HW Sensor]   1. Plug RTL-SDR dongle into USB port")
            logger.warning("[HW Sensor]   2. Install LibUSB drivers: https://zadig.akeo.ie/")
            logger.warning("[HW Sensor]   3. Ensure no other app (SDR#, GQRX) is using the device")
            logger.warning("[HW Sensor]   4. Restart the app")

    def stop(self):
        self._running = False

    def get_history(self, hours: int = 24) -> list[dict]:
        ticks = min(hours * 120, len(self.history))
        return self.history[-ticks:]

    def get_latest(self) -> dict | None:
        return self.history[-1] if self.history else None