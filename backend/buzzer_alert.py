"""
AEGIS Buzzer Alert Publisher
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Watches the current conjunction list (same data CollisionPopup renders)
and publishes an MQTT message to the ESP32 buzzer node whenever the
highest active alert level changes.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import logging
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

MQTT_BROKER = "10.40.251.89"
MQTT_PORT   = 1883
MQTT_TOPIC  = "aegis/alert"

_client = mqtt.Client(client_id="aegis-buzzer-publisher")
_last_published_alert = None  # tracks last state sent, avoids spamming


def _connect():
    try:
        _client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
        _client.loop_start()
    except Exception as e:
        logger.warning(f"[Buzzer] MQTT connect failed: {e}")


def _highest_alert(conjunctions):
    """RED > AMBER > CLEAR, ignoring mock data — same priority as CollisionPopup."""
    real = [c for c in conjunctions if not c.get("_is_mock")]
    if any(c.get("alert") == "RED" for c in real):
        return "RED", next(c for c in real if c["alert"] == "RED")["_type"]
    if any(c.get("alert") == "AMBER" for c in real):
        return "AMBER", next(c for c in real if c["alert"] == "AMBER")["_type"]
    return "CLEAR", None


def publish_buzzer_state(conjunctions):
    """
    Call this after every screening run (same place you update
    state["conjunctions"] in tasks.py). Only publishes on state change.
    """
    global _last_published_alert

    alert, event_type = _highest_alert(conjunctions)
    if alert == _last_published_alert:
        return  # no change, skip publish

    payload = json.dumps({"alert": alert, "type": event_type or ""})
    try:
        _client.publish(MQTT_TOPIC, payload, qos=1, retain=True)
        logger.info(f"[Buzzer] Published alert state: {alert} ({event_type})")
        _last_published_alert = alert
    except Exception as e:
        logger.warning(f"[Buzzer] Publish failed: {e}")


def init_buzzer():
    """Call once at app startup, e.g. alongside your other hardware init."""
    _connect()