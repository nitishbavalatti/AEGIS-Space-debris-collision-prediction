"""Debris routes — TLE data, risk scores, altitude heatmap."""
from flask import Blueprint, jsonify, request, current_app
import logging

logger = logging.getLogger(__name__)
debris_bp = Blueprint("debris", __name__, url_prefix="/api/debris")


@debris_bp.route("/list")
def debris_list():
    """
    GET /api/debris/list?limit=200
    Returns debris objects with GNN risk scores.
    Query params:
      limit   — max objects to return (default 200)
      min_risk — filter by minimum risk score (0-100)
    """
    limit = int(request.args.get("limit", 200))
    start = int(request.args.get("start", 0))
    min_risk = float(request.args.get("min_risk", 0))

    state = current_app.config["APP_STATE"]
    objects = state.get("debris_objects", [])

    if min_risk > 0:
        objects = [o for o in objects if o.get("risk_score", 0) >= min_risk]

    end = start + limit
    sliced = objects[start:end]
    return jsonify({
        "count": len(sliced),
        "total": len(objects),
        "objects": sliced,
    })


@debris_bp.route("/top-threats")
def top_threats():
    """
    GET /api/debris/top-threats?n=10
    Returns top N highest-risk objects for the threat feed panel.
    """
    n = int(request.args.get("n", 10))
    state = current_app.config["APP_STATE"]
    objects = state.get("debris_objects", [])

    sorted_objects = sorted(
        objects, key=lambda o: o.get("risk_score", 0), reverse=True
    )

    return jsonify({
        "threats": sorted_objects[:n],
    })


@debris_bp.route("/heatmap")
def altitude_heatmap():
    """
    GET /api/debris/heatmap
    Returns altitude band data for the D3 heatmap chart.
    """
    from backend.data.pipeline import build_altitude_heatmap
    state = current_app.config["APP_STATE"]
    objects = state.get("debris_objects", [])
    heatmap = build_altitude_heatmap(objects)
    return jsonify({"bands": heatmap})


@debris_bp.route("/object/<int:norad_id>")
def debris_detail(norad_id):
    """
    GET /api/debris/object/12345
    Returns full details for a specific debris object (for identity card popup).
    """
    state = current_app.config["APP_STATE"]
    objects = state.get("debris_objects", [])

    obj = next((o for o in objects if str(o.get("NORAD_CAT_ID")) == str(norad_id)), None)
    if not obj:
        return jsonify({"error": "Object not found"}), 404

    return jsonify(obj)


@debris_bp.route("/refresh", methods=["POST"])
def refresh():
    """
    POST /api/debris/refresh
    Triggers a fresh TLE fetch + GNN inference cycle.
    (Auto-runs every 6 hours via background scheduler)
    """
    from backend.tasks import run_debris_pipeline
    run_debris_pipeline(current_app._get_current_object())
    return jsonify({"status": "refresh triggered"})
