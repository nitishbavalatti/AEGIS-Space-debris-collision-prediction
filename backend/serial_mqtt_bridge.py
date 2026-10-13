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
import os
import sys
import time

import paho.mqtt.client as mqtt
import serial

MQTT_BROKER = os.getenv("MQTT_BROKER", "10.40.251.89")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
SENSOR_TOPIC = "aegis/sensors"
ALERT_TOPIC = "aegis/alert"


def stamp(msg: str) -> None:
    """Print with millisecond wall-clock time, for alert-latency measurement."""
    now = time.time()
    print(f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d} {msg}")


def make_client():
    """paho-mqtt 2.x needs CallbackAPIVersion; 1.x doesn't have it."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        return mqtt.Client()


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
        stamp(f"[MQTT] alert state RECEIVED: {alert} ({event_type})")

        try:
            if alert in ("RED", "AMBER"):
                ser.write(b"ALERT\n")
                stamp("[SERIAL] forwarded ALERT to ESP32")
            else:
                ser.write(b"SILENCE\n")
                stamp("[SERIAL] forwarded SILENCE to ESP32")
        except Exception as e:
            print(f"[SERIAL] failed to forward alert: {e}")

    return on_message


def print_ports() -> None:
    """List the serial ports Windows currently has, so a wrong COM number is obvious."""
    from serial.tools import list_ports
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    if not ports:
        print("  (no serial ports found at all)")
        print("  -> the ESP32 is not being detected. Try: a different USB cable (many are charge-only),")
        print("     another USB port, and check Device Manager > 'Ports (COM & LPT)' / 'Other devices'.")
        print("     No port at all usually means a missing driver: CP210x (Silicon Labs) or CH340/CH341 (WCH).")
        return
    for p in ports:
        ids = f"  [VID:PID {p.vid:04X}:{p.pid:04X}]" if p.vid is not None and p.pid is not None else ""
        print(f"  {p.device:8s} {p.description}{ids}")
    # USB-serial chips found on ESP32 boards: Silicon Labs CP210x, WCH CH340/CH9102, FTDI, Espressif native USB
    likely = [p for p in ports if p.vid in (0x10C4, 0x1A86, 0x0403, 0x303A)]
    if likely:
        print("  -> likely your ESP32: " + ", ".join(p.device for p in likely) + f"   (re-run with --port {likely[0].device})")
    else:
        print("  -> none of these look like a USB-serial adapter; if your ESP32 is plugged in and not listed,")
        print("     check the cable and the driver (see above). Otherwise re-run with the right --port.")


def main():
    global MQTT_BROKER
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="e.g. COM4 (run with --list to see what exists)")
    parser.add_argument("--list", action="store_true", help="list available serial ports and exit")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--broker", default=MQTT_BROKER)
    args = parser.parse_args()
    MQTT_BROKER = args.broker

    if args.list:
        print_ports()
        return
    if not args.port:
        parser.error("--port is required (use --list to see the available ports)")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=2)
    except serial.SerialException as e:
        msg = str(e)
        print(f"[SERIAL] cannot open {args.port}: {e}")
        if "Access is denied" in msg:
            # Windows error 5: the port is open in another program.
            print("  The port exists but another program has it open (Arduino IDE / PlatformIO serial monitor, "
                  "another copy of this bridge). Close it and retry.")
        elif "not functioning" in msg or ", 31)" in msg:
            # Windows error 31 (ERROR_GEN_FAILURE): the port is listed, but the USB-serial device
            # fails the moment it is opened. NOT a busy port — a driver / cable / power / stale-handle fault.
            print("  Windows lists this port but the USB-serial device fails when it is opened (error 31).")
            print("  It is not a busy port. In this order:")
            print("   1. Unplug the board, wait 5 s, plug it back in (a different USB port / a short data cable,")
            print("      no hub), then retry. A stale handle from a crashed program causes exactly this.")
            print("   2. Test the port without this script:  python -m serial.tools.miniterm COM4 115200")
            print("      (use your port number). If that fails the same way it is the driver/hardware, not the bridge.")
            print("   3. Device Manager > Ports > your port > Properties: read 'Device status'. Try Disable then")
            print("      Enable device, or reinstall the CP210x driver (clone chips can need the older v6.7 driver).")
            print("   4. Weak USB power can do it (the ESP32 resets when the port opens): unplug the buzzer and any")
            print("      other loads from the board while testing, or use a powered USB hub.")
        elif "PermissionError" in msg:
            print("  Windows refused access to the port. Close anything that might be using it and retry.")
        else:
            print("  That port does not exist right now. Ports Windows currently has:")
            print_ports()
        sys.exit(1)
    time.sleep(2)
    print(f"[SERIAL] connected to {args.port} @ {args.baud} baud")

    client = make_client()
    client.on_connect = on_connect
    client.on_message = make_on_message(ser)
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=30)
    client.loop_start()

    header_skipped = False
    last_sensor_err_log = 0.0

    while True:
        try:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            if line.startswith("bx_uT") and not header_skipped:
                header_skipped = True
                continue
            if line == "SENSOR_ERROR":
                # No magnetometer is attached (the ESP32 is used for the buzzer
                # only), so the firmware reports this on every loop. Log it
                # once a minute instead of flooding the terminal.
                if time.time() - last_sensor_err_log > 60:
                    stamp("[SERIAL] ESP32 reports no magnetometer (expected: buzzer-only setup)")
                    last_sensor_err_log = time.time()
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