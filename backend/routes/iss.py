"""ISS routes — live position, proximity, passes."""
from flask import Blueprint, jsonify, current_app
import logging

logger = logging.getLogger(__name__)
iss_bp = Blueprint("iss", __name__, url_prefix="/api/iss")


@iss_bp.route("/position")
def iss_position():
    """GET /api/iss/position — live ISS lat/lon/alt from N2YO or fallback to TLE."""
    from backend.services.n2yo import get_iss_position
    from backend.services.spacetrack import get_iss_tle
    from backend.data.pipeline import tle_to_position

    pos = get_iss_position()
    if pos and pos.get("lat") is not None and pos.get("lon") is not None:
        return jsonify(pos)

    iss_tle = get_iss_tle()
    if iss_tle:
        latlon = tle_to_position(iss_tle.get("TLE_LINE1", ""), iss_tle.get("TLE_LINE2", ""))
        if latlon:
            return jsonify({
                "lat": latlon["lat"],
                "lon": latlon["lon"],
                "alt_km": latlon["alt_km"],
                "name": iss_tle.get("OBJECT_NAME", "ISS"),
                "source": "space-track",
            })

    return jsonify(pos)


@iss_bp.route("/proximity")
def iss_proximity():
    """
    GET /api/iss/proximity
    Returns nearest debris object to ISS and distance in km.
    This powers the ISS Proximity Alert panel on the dashboard.
    """
    state = current_app.config["APP_STATE"]
    proximity = state.get("iss_proximity", {})
    return jsonify(proximity)


@iss_bp.route("/passes")
def iss_passes():
    """
    GET /api/iss/passes
    Returns upcoming ISS visible passes over Bengaluru ground station.
    Useful for timing hardware sensor recordings.
    """
    from backend.services.n2yo import get_iss_passes
    passes = get_iss_passes(days=5, min_elevation=10)
    return jsonify({"passes": passes})
