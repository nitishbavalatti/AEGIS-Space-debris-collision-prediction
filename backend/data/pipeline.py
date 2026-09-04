"""
AEGIS Data Pipeline
Handles TLE → orbital position propagation using SGP4,
altitude binning for the heatmap, and ISS proximity calculation.
"""
import math
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

try:
    from sgp4.api import Satrec, jday
    SGP4_AVAILABLE = True
except ImportError:
    SGP4_AVAILABLE = False
    logger.warning("sgp4 not installed — orbit propagation unavailable")


def tle_to_position(tle_line1: str, tle_line2: str, dt: datetime = None) -> dict | None:
    """
    Propagate a TLE to ECI (Earth-Centered Inertial) position and velocity.
    Returns lat/lon/alt using a simple ECI→geographic conversion.
    """
    if not SGP4_AVAILABLE:
        return None

    if dt is None:
        dt = datetime.now(timezone.utc)

    try:
        satellite = Satrec.twoline2rv(tle_line1, tle_line2)
        yr, mo, day, hr, mn, sc = (
            dt.year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second + dt.microsecond / 1e6
        )
        jd, fr = jday(yr, mo, day, hr, mn, sc)
        e, r, v = satellite.sgp4(jd, fr)

        if e != 0:
            return None

        # ECI → Geodetic (simplified)
        x, y, z = r  # km
        r_km = math.sqrt(x**2 + y**2 + z**2)
        alt_km = r_km - 6371.0  # Earth radius

        # GMST (Greenwich Mean Sidereal Time) for lon correction
        gmst = _gmst(jd + fr)
        lon_rad = math.atan2(y, x) - gmst
        lat_rad = math.asin(z / r_km)

        lat = math.degrees(lat_rad)
        lon = math.degrees(lon_rad)

        # Normalize lon to -180..180
        while lon > 180:   lon -= 360
        while lon < -180:  lon += 360

        speed_km_s = math.sqrt(sum(vi**2 for vi in v))

        return {
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "alt_km": round(alt_km, 2),
            "speed_km_s": round(speed_km_s, 3),
            "x_km": round(x, 2),
            "y_km": round(y, 2),
            "z_km": round(z, 2),
        }
    except Exception as e:
        logger.debug(f"SGP4 propagation failed: {e}")
        return None


def _gmst(jd_full: float) -> float:
    """Compute Greenwich Mean Sidereal Time in radians."""
    T = (jd_full - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd_full - 2451545.0)
        + 0.000387933 * T**2
        - T**3 / 38710000.0
    ) % 360
    return math.radians(gmst_deg)


def compute_iss_proximity(
    iss_pos: dict,
    debris_positions: list[dict],
) -> dict:
    """
    Returns the nearest debris object to ISS and its distance in km.
    Uses 3D Euclidean distance in ECI coordinates.
    """
    if not iss_pos or not debris_positions:
        return {"nearest_km": None, "nearest_id": None}

    iss_x = iss_pos.get("x_km", 0)
    iss_y = iss_pos.get("y_km", 0)
    iss_z = iss_pos.get("z_km", 0)

    min_dist = float("inf")
    nearest_id = None
    nearest_obj = None

    for obj in debris_positions:
        pos = obj.get("position")
        if not pos:
            continue
        dx = pos["x_km"] - iss_x
        dy = pos["y_km"] - iss_y
        dz = pos["z_km"] - iss_z
        dist = math.sqrt(dx**2 + dy**2 + dz**2)
        if dist < min_dist:
            min_dist = dist
            nearest_id = obj.get("norad_id")
            nearest_obj = obj

    return {
        "nearest_km": round(min_dist, 3) if min_dist < float("inf") else None,
        "nearest_id": nearest_id,
        "nearest_object": nearest_obj,
        "alert": (
            "RED" if (min_dist < 5) else
            "AMBER" if (min_dist < 10) else
            "GREEN"
        ) if min_dist < float("inf") else "UNKNOWN",
    }


def build_altitude_heatmap(debris_objects: list[dict]) -> list[dict]:
    """
    Bins debris objects into altitude bands (200km-2000km, 50km buckets).
    Returns sorted list of {alt_band, count, label} for the D3 chart.
    """
    # Define bands: 200-250, 250-300, ..., 1950-2000
    bands = {}
    for lower in range(200, 2000, 50):
        upper = lower + 50
        bands[(lower, upper)] = 0

    for obj in debris_objects:
        try:
            alt = float(obj.get("APOAPSIS", 0) or obj.get("alt_km", 0))
            if 200 <= alt < 2000:
                band_lower = (int(alt) // 50) * 50
                band_key = (band_lower, band_lower + 50)
                if band_key in bands:
                    bands[band_key] += 1
        except (ValueError, TypeError):
            pass

    result = []
    for (lower, upper), count in sorted(bands.items()):
        label_parts = [f"{lower}-{upper}km"]
        if lower <= 400 < upper:
            label_parts.append("ISS")
        if lower <= 550 < upper:
            label_parts.append("Starlink")
        if lower <= 800 < upper:
            label_parts.append("⚠ DENSE")

        result.append({
            "alt_lower": lower,
            "alt_upper": upper,
            "alt_mid": (lower + upper) / 2,
            "count": count,
            "label": " | ".join(label_parts),
        })

    return result
