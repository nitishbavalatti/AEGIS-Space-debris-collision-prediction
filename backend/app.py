"""
AEGIS — Flask Backend
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Main entry point. Run with:
  python app.py

API base:   http://localhost:5000/api
WebSocket:  ws://localhost:5000/socket.io
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import eventlet
eventlet.monkey_patch()
import logging
import os
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from backend.config import Config   # loads .env first, so buzzer_alert sees MQTT_BROKER
from backend.routes.conjunction import conjunction_bp
from backend.buzzer_alert import init_buzzer
init_buzzer()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = Config.SECRET_KEY
CORS(app, origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173", "http://127.0.0.1:3000"])

# ─── Rate limiting ────────────────────────────────────────────────────────────
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["120 per minute"],
    storage_uri="memory://",
)

# ─── Optional API key auth (set AEGIS_API_KEY in .env to enable) ──────────────
API_KEY = os.getenv("AEGIS_API_KEY")

@app.before_request
def require_api_key():
    """Reject requests without valid API key if AEGIS_API_KEY is set."""
    if not API_KEY:
        return  # Auth disabled in dev
    if request.path == "/api/health":
        return  # Health check always open
    provided = request.headers.get("X-API-Key")
    if provided != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet"
)

# ─── Shared application state (in-memory, with optional MongoDB persistence) ──
app.config["APP_STATE"] = {
    "debris_objects": [],
    "iss_position": {},
    "iss_proximity": {},
    "conjunctions": [],                  
    "last_conjunction_screen": None, 
    "current_risk_score": 0.0,
    "last_tle_fetch": 0,
    "mock_sensor": None,
    "gnn_model": None,
    "mongo_store": None,
}

# ─── Register blueprints ──────────────────────────────────────────────────────
from backend.routes.debris import debris_bp
from backend.routes.iss import iss_bp
from backend.routes.solar import solar_bp

app.register_blueprint(debris_bp)
app.register_blueprint(conjunction_bp)
app.register_blueprint(iss_bp)
app.register_blueprint(solar_bp)


# ─── Health check ─────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    state = app.config["APP_STATE"]
    return jsonify({
        "status": "ok",
        "debris_count": len(state.get("debris_objects", [])),
        "sensor_source": "mock" if Config.USE_MOCK_SENSOR else "hardware",
        "gnn_loaded": state.get("gnn_model") is not None,
    })


def _sensor_detail(state) -> str:
    """Which sensors are actually connected, so the top bar doesn't say HARDWARE
    when only the RTL-SDR dongle is attached (no ESP32 / magnetometer)."""
    sensor = state.get("mock_sensor")
    esp = bool(getattr(sensor, "esp32_connected", False))
    rtl = bool(getattr(sensor, "rtlsdr_connected", False))
    if esp and rtl: return "esp32+rtlsdr"
    if esp:         return "esp32"
    if rtl:         return "rtlsdr"
    return "mock" if Config.USE_MOCK_SENSOR else "none"


# ─── WebSocket events ─────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    logger.info(f"Client connected: {request.sid}")
    # Send initial state snapshot on connect
    state = app.config["APP_STATE"]
    emit("initial_state", {
        "debris_count": len(state.get("debris_objects", [])),
        "risk_score": state.get("current_risk_score", 0),
        "sensor_source": "mock" if Config.USE_MOCK_SENSOR else "hardware",
        "debris_stage": state.get("debris_stage"),
        "is_mock_data": bool(state.get("debris_is_mock")),
        "sensor_detail": _sensor_detail(state),
    })


@socketio.on("disconnect")
def on_disconnect():
    logger.info(f"Client disconnected")


@socketio.on("request_sensor_data")
def on_request_sensor():
    state = app.config["APP_STATE"]
    mock_sensor = state.get("mock_sensor")
    if mock_sensor:
        latest = mock_sensor.get_latest()
        emit("sensor_reading", latest)


# ─── Sensor emit callback (called by MockSensor or real MQTT listener) ────────
def broadcast_sensor_reading(data: dict):
    """
    Called every 30s by mock_sensor.py or MQTT listener.
    Broadcasts to all connected WebSocket clients.
    
    Detects solar storm events from both:
    - RTL-SDR RF noise anomalies
    - Magnetometer geomagnetic spikes
    """
    socketio.emit("sensor_reading", data)

    # ─── NEW: Broadcast solar storm alerts ──────────────────────────────
    try:
        rf_data = data.get("rf", {})
        solar_data = data.get("solar", {})
        
        # RTL-SDR detected RF spike = ionospheric storm signature
        if rf_data.get("storm_detected") and rf_data.get("alert_level") != "NONE":
            logger.info(f"[App] RTL-SDR STORM ALERT: {rf_data.get('alert_level')}")
            socketio.emit("solar_storm_alert", {
                "source": "rtlsdr",
                "rf_noise": float(rf_data.get("noise_dbm", -92)),
                "alert_level": rf_data.get("alert_level", "NONE"),
                "level": rf_data.get("alert_level", "NONE"),
                "timestamp": data.get("timestamp"),
                "delta_dbfs": float(rf_data.get("delta_dbfs", 0)),
                "kp": float(data.get("solar", {}).get("kp_proxy", 0)),
            })
        
        # Mock sensor detected magnetic spike = geomagnetic storm
        if solar_data.get("storm_active") and solar_data.get("alert_level") != "NONE":
            logger.info(f"[App] MAGNETOMETER STORM ALERT: {solar_data.get('alert_level')}")
            socketio.emit("solar_storm_alert", {
                "source": "magnetometer",
                "kp": float(solar_data.get("kp_proxy", 0)),
                "alert_level": solar_data.get("alert_level", "NONE"),
                "level": solar_data.get("alert_level", "NONE"),
                "timestamp": data.get("timestamp"),
            })
    except Exception as e:
        logger.debug(f"[App] Storm alert emission failed: {e}")

    # Persist to MongoDB (existing code)
    state = app.config["APP_STATE"]
    mongo = state.get("mongo_store")
    if mongo and mongo.connected:
        mongo.store_sensor_reading(data)

    # Update KP index for GNN re-scoring (existing code)
    kp = data.get("solar", {}).get("kp_proxy", 2.0)
    if kp >= 5.0:
        logger.info(f"[App] Solar storm detected! Kp={kp:.1f}")
        # Could trigger lightweight re-scoring here


# ─── Startup ──────────────────────────────────────────────────────────────────
def init_app():
    state = app.config["APP_STATE"]

    # 1. Initialize MongoDB persistence
    try:
        from backend.services.mongo_store import MongoStore
        mongo = MongoStore(uri=Config.MONGO_URI)
        state["mongo_store"] = mongo
    except Exception as e:
        logger.warning(f"[Init] MongoDB unavailable: {e} — in-memory mode")

    # 2. Load GNN model
    try:
        from backend.gnn.model import load_model
        model = load_model(checkpoint_path="gnn/aegis_gnn.pt")
        state["gnn_model"] = model
        if model is not None:
            logger.info("[Init] GNN model loaded")
        else:
            logger.warning("[Init] GNN NOT loaded (missing/incompatible checkpoint) — "
                           "objects will use heuristic scoring. Retrain: python -m backend.gnn.train")
    except Exception as e:
        logger.warning(f"[Init] GNN not loaded: {e} — using fallback scoring")

    # 2. Start sensor (mock or hardware)
    if Config.USE_MOCK_SENSOR:
        from backend.mock_sensor import MockSensor
        sensor = MockSensor(emit_callback=broadcast_sensor_reading)
        sensor.start()
        state["mock_sensor"] = sensor
        logger.info("[Init] Mock sensor started (USE_MOCK_SENSOR=true)")
    else:
        try:
            from backend.services.hardware_sensor import HardwareSensor
            sensor = HardwareSensor(
                emit_callback=broadcast_sensor_reading,
                broker=Config.MQTT_BROKER,
                port=Config.MQTT_PORT,
            )
            sensor.start()
            state["mock_sensor"] = sensor  # same key — backend never knows the difference
            logger.info("[Init] Hardware sensor listener started (USE_MOCK_SENSOR=false)")
        except Exception as e:
            logger.warning(f"[Init] Hardware sensor listener failed: {e} — falling back to mock sensor")
            from backend.mock_sensor import MockSensor
            sensor = MockSensor(emit_callback=broadcast_sensor_reading)
            sensor.start()
            state["mock_sensor"] = sensor
            logger.info("[Init] Mock sensor started (fallback)")

    # 3. Initial debris
    # REAL mode: start EMPTY. The pipeline publishes real objects within seconds
    # (immediately if the on-disk TLE cache is fresh) and the dashboard refetches
    # on the "debris_updated" event. The old code pre-loaded 500 *fake* objects
    # with realistic names, which looked real and was what stayed on screen.
    # MOCK mode: pre-load demo objects as before.
    if getattr(Config, "DATA_MODE", "REAL") == "MOCK":
        try:
            from backend.tasks import _generate_mock_debris, _score_debris
            logger.info("[Init] MOCK data mode — pre-loading 500 demo debris objects...")
            debris = _score_debris(_generate_mock_debris(500))
            state["debris_objects"] = debris
            state["debris_is_mock"] = True
            scores = sorted([o.get("risk_score", 0) for o in debris], reverse=True)
            top10 = scores[:max(1, len(scores)//10)]
            state["current_risk_score"] = round(sum(top10) / len(top10), 1)
        except Exception as e:
            logger.warning(f"[Init] Debris pre-load failed: {e}")
    else:
        logger.info("[Init] REAL data mode — waiting for the debris pipeline (no placeholder objects)")

    # 4. Start background tasks (TLE fetch + ISS updater)
    from backend.tasks import start_background_tasks
    start_background_tasks(app)


if __name__ == "__main__":
    # Need to import request for websocket handler
    init_app()
    logger.info("━" * 60)
    logger.info("  AEGIS Backend running on http://localhost:5000")
    logger.info(f"  Sensor mode: {'MOCK (simulated)' if Config.USE_MOCK_SENSOR else 'HARDWARE (ESP32)'}")
    logger.info("━" * 60)
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)