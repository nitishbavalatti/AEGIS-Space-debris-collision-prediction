import os
from dotenv import load_dotenv

load_dotenv(override=True)  # Force override any existing env vars

class Config:
    # Space-Track
    SPACETRACK_USERNAME = os.getenv("SPACETRACK_USERNAME")
    SPACETRACK_PASSWORD = os.getenv("SPACETRACK_PASSWORD")
    SPACETRACK_BASE_URL = "https://www.space-track.org"

    # N2YO
    N2YO_API_KEY = os.getenv("N2YO_API_KEY")
    N2YO_BASE_URL = "https://api.n2yo.com/rest/v1/satellite"

    # NOAA SWPC
    NOAA_SWPC_URL = os.getenv("NOAA_SWPC_URL", "https://services.swpc.noaa.gov")

    # NASA
    NASA_API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")
    NASA_DONKI_URL = "https://api.nasa.gov/DONKI"
    NASA_SDO_URL = "https://sdo.gsfc.nasa.gov/assets/img/latest/latest_1024_0193.jpg"

    # MongoDB
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/aegis")

    # Flask
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")

    # Hardware / Sensor
    USE_MOCK_SENSOR = os.getenv("USE_MOCK_SENSOR", "true").lower() == "true"
    MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
    MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
    MQTT_TOPIC = os.getenv("MQTT_TOPIC", "aegis/sensors")

    # Ground Station (Bengaluru)
    STATION_LAT = float(os.getenv("STATION_LAT", 12.9716))
    STATION_LON = float(os.getenv("STATION_LON", 77.5946))
    STATION_ALT = float(os.getenv("STATION_ALT", 920))

    # ISS NORAD ID
    ISS_NORAD_ID = 25544

    # Data mode: REAL when real data sources are configured, MOCK as fallback
    # Heuristics: REAL if any real API keys are present; otherwise MOCK
    _REAL_DATA_KEYS = (
        SPACETRACK_USERNAME or SPACETRACK_PASSWORD or N2YO_API_KEY or (NASA_API_KEY != "DEMO_KEY")
    )
    DATA_MODE = os.getenv("DATA_MODE", "REAL" if _REAL_DATA_KEYS else "MOCK")
