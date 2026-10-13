"""Debris routes — TLE data, risk scores, altitude heatmap."""
from flask import Blueprint, jsonify, request, current_app
import logging

logger = logging.getLogger(__name__)
debris_bp = Blueprint("debris", __name__, url_prefix="/api/debris")


def _score(o) -> float:
    try:
        return float(o.get("risk_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _tier_counts(objects) -> dict:
    """Risk-tier counts over the WHOLE catalogue (same 80/60 cut-offs and
    rounding the dashboard uses), so the header doesn't have to infer them
    from whichever subset the browser happened to load."""
    critical = high = 0
    for o in objects:
        s = round(_score(o))
        if s >= 80:
            critical += 1
        elif s >= 60:
            high += 1
    return {"critical": critical, "high": high}


def _mixed(objects, limit: int):
    """Top objects by risk PLUS an evenly-spaced sample of the rest.

    Returning only the top-N by risk meant the globe showed nothing but the
    highest tier (all one colour) and hid the real spread of the catalogue. This
    keeps the ~300 highest-risk objects (so the dangerous ones are always drawn)
    and fills the remaining slots by sampling the rest at even intervals through
    the risk ranking, so the colours reflect the actual distribution."""
    ranked = sorted(objects, key=_score, reverse=True)
    if len(ranked) <= limit:
        return ranked
    head_n = min(300, limit // 4)
    head, rest = ranked[:head_n], ranked[head_n:]
    need = limit - head_n
    step = len(rest) / need
    return head + [rest[int(i * step)] for i in range(need)]


@debris_bp.route("/list")
def debris_list():
    """
    GET /api/debris/list?limit=2000&sort=risk
    Returns debris objects with risk scores.
    Query params:
      limit    — max objects to return (default 200)
      start    — offset into the (optionally sorted) list
      sort     — "risk": highest-risk first. "mixed": the top ~300 by risk plus
                 an even sample of the rest (what the globe uses, so colours
                 show the real distribution). Without it, a limited page is
                 whichever objects come first in catalogue order
      min_risk — filter by minimum risk score (0-100)
    The response also carries whole-catalogue `tiers` and `is_mock_data`, so the
    UI's TRACKED / CRITICAL / HIGH figures describe the same set.
    """
    limit = int(request.args.get("limit", 200))
    start = int(request.args.get("start", 0))
    min_risk = float(request.args.get("min_risk", 0))
    sort = request.args.get("sort", "")

    state = current_app.config["APP_STATE"]
    all_objects = state.get("debris_objects", [])
    objects = all_objects

    if min_risk > 0:
        objects = [o for o in objects if _score(o) >= min_risk]
    if sort == "risk":
        objects = sorted(objects, key=_score, reverse=True)
    elif sort == "mixed":
        objects = _mixed(objects, limit)

    end = start + limit
    sliced = objects[start:end]
    return jsonify({
        "count": len(sliced),
        "total": len(all_objects),
        "objects": sliced,
        "tiers": _tier_counts(all_objects),
        "is_mock_data": bool(state.get("debris_is_mock")),
        "stage": state.get("debris_stage"),
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

    sorted_objects = sorted(objects, key=_score, reverse=True)

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