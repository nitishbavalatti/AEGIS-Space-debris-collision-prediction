"""
Conjunction routes — upcoming close approach predictions.
Covers both Debris×ISS and Debris×Debris events.
"""
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)
conjunction_bp = Blueprint("conjunction", __name__, url_prefix="/api/conjunction")


def _live_events(conjunctions):
    """Recompute tca_hours from the stored TCA timestamp at request time and
    drop events whose TCA has already passed. tca_hours is frozen at
    screening time, so without this the UI shows stale countdowns and keeps
    expired events until the next 30-minute screening run."""
    now = datetime.now(timezone.utc)
    out = []
    for c in conjunctions:
        try:
            tca = datetime.fromisoformat(c["tca"])
            if tca.tzinfo is None:
                tca = tca.replace(tzinfo=timezone.utc)
            hrs = (tca - now).total_seconds() / 3600
        except (KeyError, ValueError, TypeError):
            out.append(c)
            continue
        if hrs < 0:
            continue
        out.append({**c, "tca_hours": round(hrs, 3)})
    return out


@conjunction_bp.route("/upcoming")
def upcoming_conjunctions():
    """
    GET /api/conjunction/upcoming?hours=72&threshold=25&type=all
    Returns all predicted close approaches sorted by miss distance.

    type param: "all" | "debris_iss" | "debris_debris"
    """
    hours     = int(request.args.get("hours",     72))
    threshold = float(request.args.get("threshold", 25.0))
    event_type = request.args.get("type", "all")

    state        = current_app.config["APP_STATE"]
    conjunctions = _live_events(state.get("conjunctions", []))

    filtered = [
        c for c in conjunctions
        if c.get("miss_distance_km", 999) <= threshold
        and c.get("tca_hours", 999) <= hours
        and (event_type == "all" or c.get("_type") == event_type)
    ]

    # Separate counts by type for the frontend
    dxiss_count = sum(1 for c in filtered if c.get("_type") == "debris_iss")
    dxd_count   = sum(1 for c in filtered if c.get("_type") == "debris_debris")

    return jsonify({
        "count":          len(filtered),
        "debris_iss":     dxiss_count,
        "debris_debris":  dxd_count,
        "window_hours":   hours,
        "threshold_km":   threshold,
        "conjunctions":   filtered,
        "last_screened":  state.get("last_conjunction_screen"),
        "screening":      bool(state.get("conjunction_screening")),
    })


@conjunction_bp.route("/critical")
def critical_conjunctions():
    """
    GET /api/conjunction/critical
    Returns only RED and AMBER events across both D×ISS and D×D.
    Used by CollisionPopup.
    """
    state        = current_app.config["APP_STATE"]
    conjunctions = _live_events(state.get("conjunctions", []))

    critical = [
        c for c in conjunctions
        if c.get("alert") in ("RED", "AMBER") and not c.get("_is_mock")
    ]

    dxiss = [c for c in critical if c.get("_type") == "debris_iss"]
    dxd   = [c for c in critical if c.get("_type") == "debris_debris"]

    return jsonify({
        "count":         len(critical),
        "debris_iss":    len(dxiss),
        "debris_debris": len(dxd),
        "events":        critical,
    })


@conjunction_bp.route("/stats")
def conjunction_stats():
    """
    GET /api/conjunction/stats
    Summary counts broken down by event type and alert level.
    """
    state        = current_app.config["APP_STATE"]
    conjunctions = _live_events(state.get("conjunctions", []))
    real         = [c for c in conjunctions if not c.get("_is_mock")]

    red_iss   = sum(1 for c in real if c.get("alert") == "RED"   and c.get("_type") == "debris_iss")
    red_dxd   = sum(1 for c in real if c.get("alert") == "RED"   and c.get("_type") == "debris_debris")
    amber_iss = sum(1 for c in real if c.get("alert") == "AMBER" and c.get("_type") == "debris_iss")
    amber_dxd = sum(1 for c in real if c.get("alert") == "AMBER" and c.get("_type") == "debris_debris")

    nearest = real[0] if real else None

    # Flag if any D×D event has kessler_risk = True
    kessler_threat = any(c.get("kessler_risk") for c in real)

    return jsonify({
        "total":           len(real),
        "red_iss":         red_iss,
        "red_debris":      red_dxd,
        "amber_iss":       amber_iss,
        "amber_debris":    amber_dxd,
        "kessler_threat":  kessler_threat,
        "nearest_km":      nearest["miss_distance_km"] if nearest else None,
        "nearest_tca_h":   nearest["tca_hours"]        if nearest else None,
        "nearest_type":    nearest["_type"]             if nearest else None,
        "last_screened":   state.get("last_conjunction_screen"),
    })


@conjunction_bp.route("/refresh", methods=["POST"])
def refresh_conjunctions():
    """POST /api/conjunction/refresh — trigger a fresh screening run."""
    import threading
    from backend.tasks import run_conjunction_screener
    app = current_app._get_current_object()
    threading.Thread(target=run_conjunction_screener, args=(app,), daemon=True).start()
    return jsonify({"status": "screening started — both D×ISS and D×D"})