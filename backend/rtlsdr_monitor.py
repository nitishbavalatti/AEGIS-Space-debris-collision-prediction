"""
AEGIS RTL-SDR Monitor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs separately on your PC alongside the ESP32.
Captures RF noise power from the RTL-SDR dongle at NOAA
weather satellite frequencies (137 MHz) and publishes
measurements to MQTT, which the backend merges with the
HMC5883L magnetometer data.

INSTALL:
  pip install pyrtlsdr paho-mqtt numpy

HARDWARE:
  RTL-SDR dongle + antenna plugged into your PC USB.
  The ESP32 handles the magnetometer.
  Both streams meet at the MQTT broker.

RUN:
  python rtlsdr_monitor.py

FREQUENCIES MONITORED:
  137.100 MHz — NOAA-15 primary APT downlink
  137.620 MHz — NOAA-19 primary APT downlink
  137.912 MHz — NOAA-18 primary APT downlink
  Background noise floor rises during solar storms
  due to increased ionospheric absorption/emission.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import time
import json
import logging
import threading
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ─── Config ───────────────────────────────────────────────────────────────────
MQTT_BROKER   = "localhost"
MQTT_PORT     = 1883
MQTT_TOPIC_RF = "aegis/rtlsdr"    # Published separately; backend merges this

SAMPLE_RATE   = 2.4e6   # 2.4 MHz bandwidth
FREQUENCIES   = [137.1e6, 137.62e6, 137.912e6]  # NOAA APT frequencies
GAIN          = 30.0    # dB — adjust for your antenna (0 = auto)
READ_SECONDS  = 5       # Integrate noise over 5s per reading
REPORT_INTERVAL = 30    # seconds between MQTT publishes

# Baseline noise floor (calibrated in quiet conditions, adjust if needed)
BASELINE_DBFS = -45.0   # dBFS typical quiet SDR reading

# ─── SDR Power Measurement ────────────────────────────────────────────────────

def measure_noise_power(sdr, freq_hz: float, duration_s: float = 2.0) -> float:
    """
    Tune to freq_hz and measure average noise power in dBFS.
    Returns float dBFS value.
    """
    sdr.center_freq = freq_hz
    num_samples = int(SAMPLE_RATE * duration_s)
    # Read in chunks to avoid buffer overflow
    chunk_size = 256 * 1024
    total_power = 0.0
    chunks_read = 0

    samples_remaining = num_samples
    while samples_remaining > 0:
        read_size = min(chunk_size, samples_remaining)
        samples = sdr.read_samples(read_size)
        # Compute power from raw IQ samples (complex values)
        # Power = mean(|I|^2 + |Q|^2) for complex samples
        power = np.mean(np.abs(samples) ** 2)
        total_power += power
        chunks_read += 1
        samples_remaining -= read_size

    avg_power = total_power / chunks_read if chunks_read > 0 else 1e-10
    dbfs = 10 * np.log10(avg_power + 1e-12)
    return round(dbfs, 3)


def run_rtlsdr_monitor():
    """Main monitoring loop."""
    try:
        from rtlsdr import RtlSdr
    except ImportError:
        logger.error("pyrtlsdr not installed: pip install pyrtlsdr")
        return

    try:
        import paho.mqtt.client as mqtt_client
    except ImportError:
        logger.error("paho-mqtt not installed: pip install paho-mqtt")
        return

    # Connect MQTT
    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id="aegis-rtlsdr")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        logger.info(f"[MQTT] Connected to {MQTT_BROKER}:{MQTT_PORT}")
    except Exception as e:
        logger.error(f"[MQTT] Connection failed: {e}")
        return

    # Open SDR
    try:
        sdr = RtlSdr()
        sdr.sample_rate = SAMPLE_RATE
        sdr.gain = GAIN
        logger.info(f"[RTL-SDR] Device open. Gain={GAIN}dB, Rate={SAMPLE_RATE/1e6:.1f}MHz")
    except Exception as e:
        logger.error(f"[RTL-SDR] Cannot open device: {e}")
        logger.error("[RTL-SDR] Is an RTL-SDR dongle plugged in?")
        logger.error("[RTL-SDR] Running in MOCK mode — publishing simulated RF data")
        _run_rtlsdr_mock(client)
        return

    logger.info(f"[RTL-SDR] Monitoring {len(FREQUENCIES)} NOAA frequencies")

    # Establish baseline
    baselines = {}
    logger.info("[RTL-SDR] Establishing noise baselines (takes ~30s)...")
    for freq in FREQUENCIES:
        readings = [measure_noise_power(sdr, freq, 2.0) for _ in range(3)]
        baselines[freq] = float(np.mean(readings))  # ← FIX: Convert to float
        logger.info(f"  {freq/1e6:.3f} MHz baseline: {baselines[freq]:.1f} dBFS")

    tick = 0
    while True:
        try:
            freq_readings = {}
            for freq in FREQUENCIES:
                power = measure_noise_power(sdr, freq, READ_SECONDS / len(FREQUENCIES))
                freq_readings[freq] = float(power)  # ← FIX: Convert numpy to Python float

            # Aggregate: mean across all frequencies
            mean_power = float(np.mean(list(freq_readings.values())))  # ensure Python float
            baseline_mean = float(np.mean(list(baselines.values())))    # ensure Python float
            delta_dbfs = float(mean_power - baseline_mean)              # ensure Python float

            # Convert to approximate dBm
            noise_dbm = -92.0 + delta_dbfs * 0.5

            # Convert numpy bool to Python bool for JSON serialization
            storm_detected = bool(delta_dbfs > 6.0)
            
            rf_alert = (
                "SEVERE"   if delta_dbfs > 20 else
                "STRONG"   if delta_dbfs > 14 else
                "MODERATE" if delta_dbfs > 8  else
                "MINOR"    if delta_dbfs > 4  else
                "NONE"
            )

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tick": int(tick),  # ← FIX: Ensure int type
                "source": "rtlsdr",
                "rf": {
                    "noise_dbm": float(round(noise_dbm, 2)),           # ensure Python float
                    "noise_dbfs": float(round(mean_power, 3)),         # Python float
                    "baseline_dbfs": float(round(baseline_mean, 3)),   # Python float
                    "delta_dbfs": float(round(delta_dbfs, 3)),         # Python float
                    "storm_detected": storm_detected,  # Python bool
                    "alert_level": rf_alert,
                },
                "per_frequency": {
                    f"{f/1e6:.3f}MHz": float(round(v, 2))
                    for f, v in freq_readings.items()
                },
            }

            # Double-check JSON safety
            try:
                safe_json = json.dumps(payload)
            except TypeError as je:
                logger.error(f"JSON encoding failed: {je}")
                logger.error(f"Payload: {payload}")
                continue

            client.publish(MQTT_TOPIC_RF, safe_json)
            logger.info(
                f"[RTL-SDR] tick={tick} noise={noise_dbm:.1f}dBm "
                f"delta={delta_dbfs:+.1f}dBFS alert={rf_alert}"
            )
            tick += 1

        except Exception as e:
            logger.error(f"[RTL-SDR] Measurement error: {e}", exc_info=True)
            time.sleep(5)  # Wait before retry

        time.sleep(REPORT_INTERVAL)


def _run_rtlsdr_mock(client):
    """
    Publish simulated RF noise data when no RTL-SDR dongle is available.
    Uses realistic baseline with occasional noise spikes.
    """
    import random
    tick = 0
    baseline_dbm = -92.0
    logger.info("[RTL-SDR MOCK] Publishing simulated RF data every 30s")

    while True:
        try:
            # Realistic noise floor with small random walk
            noise = baseline_dbm + random.gauss(0, 1.2)
            # Occasional spike (simulating solar activity)
            if random.random() < 0.05:
                noise += random.uniform(3, 10)

            delta = noise - baseline_dbm
            rf_alert = (
                "SEVERE"   if delta > 10 else
                "STRONG"   if delta > 7  else
                "MODERATE" if delta > 4  else
                "MINOR"    if delta > 2  else
                "NONE"
            )

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tick": tick,
                "source": "rtlsdr_mock",
                "rf": {
                    "noise_dbm": round(noise, 2),
                    "baseline_dbm": round(baseline_dbm, 2),
                    "delta_dbm": round(delta, 2),
                    "storm_detected": delta > 4,
                    "alert_level": rf_alert,
                    "note": "No RTL-SDR dongle — simulated data",
                },
            }

            client.publish(MQTT_TOPIC_RF, json.dumps(payload))
            logger.info(f"[RTL-SDR MOCK] tick={tick} noise={noise:.1f}dBm alert={rf_alert}")
            tick += 1
        except Exception as e:
            logger.error(f"[RTL-SDR MOCK] Error: {e}")

        time.sleep(REPORT_INTERVAL)


if __name__ == "__main__":
    logger.info("━" * 60)
    logger.info("  AEGIS RTL-SDR Monitor")
    logger.info("  Monitoring NOAA APT frequencies for solar RF anomalies")
    logger.info("━" * 60)
    run_rtlsdr_monitor()
