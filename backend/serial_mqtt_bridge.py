"""
AEGIS - Serial <-> MQTT bridge for the ESP32 + GY-271 magnetometer + buzzer.

ESP32 stays serial-only (USB) since its WiFi keeps resetting. This script:
  1. Reads calibrated bx/by/bz over serial, computes total field +
     declination, and republishes to mosquitto on "aegis/sensors" in the
     schema hardware_sensor.py expects.
  2. Subscribes to "aegis/alert" (published by buzzer_alert.py) and
     forwards RED/AMBER as "ALERT\n" and CLEAR as "SILENCE\n" down the
     same serial line, since the ESP32 has no MQTT connection of its own.

Usage:
    pip install pyserial paho-mqtt
    python serial_mqtt_bridge.py --port COM4
"""

import argparse
import json
import math
import time

import paho.mqtt.client as mqtt
import serial

MQTT_BROKER = "10.40.251.89"
MQTT_PORT = 1883
SENSOR_TOPIC = "aegis/sensors"
ALERT_TOPIC = "aegis/alert"


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] connected to {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(ALERT_TOPIC)
        print(f"[MQTT] subscribed to {ALERT_TOPIC}")
    else:
        print(f"[MQTT] connect failed, rc={rc}")


def make_on_message(ser: serial.Serial):
    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[MQTT] bad alert payload: {e}")
            return

        alert = data.get("alert", "CLEAR")
        event_type = data.get("type", "")
        print(f"[MQTT] alert state: {alert} ({event_type})")

        try:
            if alert in ("RED", "AMBER"):
                ser.write(b"ALERT\n")
                print("[SERIAL] forwarded ALERT to ESP32")
            else:
                ser.write(b"SILENCE\n")
                print("[SERIAL] forwarded SILENCE to ESP32")
        except Exception as e:
            print(f"[SERIAL] failed to forward alert: {e}")

    return on_message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="e.g. COM4")
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=2)
    time.sleep(2)
    print(f"[SERIAL] connected to {args.port} @ {args.baud} baud")

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = make_on_message(ser)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
    client.loop_start()

    header_skipped = False

    while True:
        try:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            if line.startswith("bx_uT") and not header_skipped:
                header_skipped = True
                continue
            if line == "SENSOR_ERROR":
                print("[SERIAL] sensor error reported by ESP32")
                continue

            parts = line.split(",")
            if len(parts) != 3:
                continue

            bx, by, bz = (float(p) for p in parts)
            total = math.sqrt(bx * bx + by * by + bz * bz)
            declination = math.degrees(math.atan2(by, bx))

            payload = {
                "mag": {
                    "bx": round(bx, 3),
                    "by": round(by, 3),
                    "bz": round(bz, 3),
                    "total": round(total, 3),
                    "declination_deg": round(declination, 2),
                }
            }

            client.publish(SENSOR_TOPIC, json.dumps(payload))
            print(f"[MQTT] published: {payload}")

        except KeyboardInterrupt:
            print("\n[BRIDGE] stopping")
            break
        except Exception as e:
            print(f"[BRIDGE] error: {e}")
            time.sleep(1)

    client.loop_stop()
    ser.close()


if __name__ == "__main__":
    main()