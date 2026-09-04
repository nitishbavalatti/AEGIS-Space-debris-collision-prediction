"""
N2YO API service.
Provides ISS live position, pass predictions, and satellite above queries.

API Docs: https://www.n2yo.com/api/
"""
import requests
import logging
from backend.config import Config
from backend.services.cache import cache

logger = logging.getLogger(__name__)

BASE = Config.N2YO_BASE_URL
KEY = Config.N2YO_API_KEY
LAT = Config.STATION_LAT
LON = Config.STATION_LON
ALT = Config.STATION_ALT
ISS_ID = Config.ISS_NORAD_ID


def get_iss_position() -> dict:
    """
    Returns live ISS position: lat, lon, altitude (km), azimuth, velocity.
    Endpoint: /positions/{id}/{lat}/{lon}/{alt}/{seconds}
    """
    cached = cache.get("n2yo:iss_position")
    if cached is not None:
        return cached

    url = f"{BASE}/positions/{ISS_ID}/{LAT}/{LON}/{ALT}/1/&apiKey={KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        positions = data.get("positions", [])
        if positions:
            pos = positions[0]
            result = {
                "lat": pos.get("satlatitude"),
                "lon": pos.get("satlongitude"),
                "alt_km": pos.get("sataltitude"),
                "azimuth": pos.get("azimuth"),
                "elevation": pos.get("elevation"),
                "ra": pos.get("ra"),
                "dec": pos.get("dec"),
                "timestamp": pos.get("timestamp"),
                "name": data.get("info", {}).get("satname", "ISS"),
            }
            cache.set("n2yo:iss_position", result, ttl=30)
            return result
    except Exception as e:
        logger.error(f"N2YO ISS position error: {e}")
    return {}


def get_iss_passes(days: int = 5, min_elevation: int = 10) -> list[dict]:
    """
    Returns upcoming ISS visible passes over Bengaluru ground station.
    Useful for: timing real antenna pointing when hardware is connected.
    """
    url = f"{BASE}/visualpasses/{ISS_ID}/{LAT}/{LON}/{ALT}/{days}/{min_elevation}/&apiKey={KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        passes = data.get("passes", [])
        logger.info(f"Fetched {len(passes)} ISS passes")
        return passes
    except Exception as e:
        logger.error(f"N2YO ISS passes error: {e}")
        return []


def get_satellites_above(category: int = 6, radius: int = 70) -> dict:
    """
    Returns all objects (debris = category 6) above Bengaluru within radius degrees.
    category 6 = rocket debris | category 0 = all objects
    """
    url = f"{BASE}/above/{LAT}/{LON}/{ALT}/{radius}/{category}/&apiKey={KEY}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        above = data.get("above", [])
        logger.info(f"{len(above)} objects above Bengaluru")
        return {
            "count": data.get("info", {}).get("satcount", 0),
            "objects": above[:20],  # top 20 for display
        }
    except Exception as e:
        logger.error(f"N2YO above error: {e}")
        return {"count": 0, "objects": []}
