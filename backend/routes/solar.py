"""Solar weather routes — NOAA SWPC status, sensor correlation data."""
from flask import Blueprint, jsonify, request, current_app
import logging

from backend.services import risk_history
from backend.services import geomag_history

logger = logging.getLogger(__name__)
solar_bp = Blueprint("solar", __name__, url_prefix="/api/solar")


def _load_risk_history(state, hours: int) -> list[tuple[str, float]]:
    """
    Combined risk-score history for the correlation chart: Mongo (real,
    persisted, survives restarts) as the base, merged with anything the
    in-memory buffer has that Mongo doesn't yet.

    Why merge rather than just pick one:
      - Mongo only gets a new entry when run_debris_pipeline() completes
        a full run (~6h cadence) — see tasks.py's
        mongo.store_debris_snapshot() call.
      - The in-memory buffer (risk_history.py) also gets opportunistic
        fallback samples from THIS route and /summary between pipeline
        runs, so it can have points Mongo doesn't.
      - If Mongo is down/unconfigured, this still works — falls through
        to whatever the in-memory buffer has, same as before.

    De-duped and sorted ascending by timestamp string. Safe to call this
    on every request — Mongo query is capped to the requested window and
    projects out the heavy `top_10` field (see get_risk_history), and the
    in-memory side is a plain deque copy.
    """
    combined: list[tuple[str, float]] = []

    mongo = state.get("mongo_store")
    if mongo and getattr(mongo, "connected", False):
        try:
            combined.extend(mongo.get_risk_history(hours=hours))
        except Exception as e:
            logger.warning(f"[Solar] Mongo risk history query failed: {e}")

    combined.extend(risk_history.snapshot())

    seen = set()
    merged = []
    for ts, val in sorted(combined, key=lambda x: x[0]):
        if ts in seen:
            continue
        seen.add(ts)
        merged.append((ts, val))
    return merged


@solar_bp.route("/summary")
def solar_summary():
    """GET /api/solar/summary — current solar weather from NOAA SWPC."""
    try:
        from backend.services.noaa_swpc import get_solar_summary
        summary = get_solar_summary(days_back=7)

        # Fallback sampling only — the authoritative sample point is in
        # tasks.py's run_debris_pipeline(), right after a real GNN
        # rescoring pass. This just keeps the buffer from sitting
        # completely empty between pipeline runs (every ~6h) or right
        # after a fresh deploy before the first pipeline run finishes.
        # record_risk_score() de-dupes, so polling this route frequently
        # doesn't spam the buffer with repeats of the same value.
        state = current_app.config.get("APP_STATE", {})
        risk_history.record_risk_score(state.get("current_risk_score"))

        return jsonify(summary)
    except Exception as e:
        logger.error(f"[Solar] summary error: {e}", exc_info=True)

    # Hard fallback — should never reach here
    return jsonify({
        "storm_level": "NONE",
        "max_kp_index": 0.0,
        "flare_count": 0,
        "x_class_flares": 0,
        "m_class_flares": 0,
        "cme_count": 0,
        "latest_flares": [],
        "latest_cmes": [],
        "geomag_bt_nT": None,
        "geomag_bz_nT": None,
        "geomag_reachable": False,
        "noaa_reachable": False,
        "donki_reachable": False,
        "note": "Solar summary error",
    })


@solar_bp.route("/test-noaa")
@solar_bp.route("/test-donki")
def test_noaa():
    """
    GET /api/solar/test-noaa
    Diagnostic endpoint — clears cache then hits NOAA SWPC directly.
    Check the server logs for the detailed error if reachable=false.
    """
    from backend.services.noaa_swpc import (
        clear_noaa_cache, get_kp_history, get_geomag_realtime
    )
    from backend.config import Config

    clear_noaa_cache()

    history, ok = get_kp_history(7)
    geomag = get_geomag_realtime(15)

    return jsonify({
        "noaa_url":      Config.NOAA_SWPC_URL,
        "noaa_ok":       ok,
        "kp_count":      len(history),
        "geomag_ok":     bool(geomag),
        "geomag_count":  len(geomag),
        "geomag_latest": geomag[-1] if geomag else None,
        "message": (
            "NOAA SWPC reachable — check logs for counts"
            if ok
            else "NOAA SWPC UNREACHABLE — check server logs for the specific error (SSL/DNS/timeout/HTTP)"
        ),
    })


@solar_bp.route("/flares")
def solar_flares():
    """GET /api/solar/flares?days=7 — recent solar flare events."""
    from backend.services.noaa_swpc import get_solar_summary
    days = int(request.args.get("days", 7))
    summary = get_solar_summary(days_back=days)
    return jsonify({
        "flares": summary.get("latest_flares", []),
        "count": len(summary.get("latest_flares", [])),
        "note": "NOAA SWPC summary only; flare events are not available in this feed.",
    })


@solar_bp.route("/correlation")
def sensor_correlation():
    """
    GET /api/solar/correlation?hours=24
    Real NOAA geomagnetic field (L1 IMF Bt/Bz) vs GNN risk score history.
    """
    from backend.services.noaa_swpc import get_geomag_realtime, get_kp_history, _parse_kp

    hours = int(request.args.get("hours", 24))
    state = current_app.config["APP_STATE"]
    current_risk_score = state.get("current_risk_score", 0)

    # Fallback sample (see solar_summary() above for why this isn't the
    # primary source), then load real history — Mongo-persisted samples
    # merged with anything newer still only in the in-memory buffer. See
    # _load_risk_history() docstring above for why both are combined.
    #
    # Query a WIDER window (min 48h) than what's actually being charted
    # (`hours`, default 24). nearest_at_or_before() just needs the most
    # recent risk sample at-or-before each geomag point's timestamp — if
    # the risk buffer only samples every ~6h (pipeline cadence) and we
    # only fetched exactly `hours` back, the very first few hours of the
    # chart could have no qualifying "before" sample even though a
    # perfectly good one exists just outside that narrower window, and
    # they'd render null (a gap) instead of correctly holding the last
    # known value forward.
    risk_history.record_risk_score(current_risk_score)
    history_snapshot = _load_risk_history(state, hours=max(hours, 48))

    # Fetch NOAA's current view (to discover any genuinely NEW minutes),
    # lock in whatever's new, then build the actual served series from
    # our own locked buffer rather than NOAA's raw per-fetch output — see
    # geomag_history.py for why. This also means a transient NOAA fetch
    # failure no longer blanks the whole chart: raw_geomag being empty
    # just means "nothing new to lock in this poll", and snapshot() below
    # still serves whatever history was already locked from earlier polls.
    raw_geomag = get_geomag_realtime(minutes_back=hours * 60)
    if raw_geomag:
        geomag_history.ingest(raw_geomag)

    geomag = geomag_history.snapshot(minutes_back=hours * 60)
    if not geomag:
        return jsonify({
            "series": [],
            "count": 0,
            "geomag_source": "unreachable",
        })

    # Pull enough Kp history to cover the window, then attach the most
    # recent Kp reading available at-or-before each geomag timestamp.
    kp_events, _ = get_kp_history(days_back=max(1, hours // 24 + 1))
    kp_sorted = sorted(kp_events, key=lambda e: e.get("time_tag", ""))

    def _nearest_kp(ts):
        # Returns None when no Kp event at-or-before `ts` exists yet, so
        # the frontend can distinguish "no data available" from a genuine
        # Kp=0 reading. Kp=0 is a real, valid quiet-geomagnetic value —
        # treating it as missing punches gaps in the correlation line
        # every time conditions are actually calm, which is wrong.
        best = None
        for k in kp_sorted:
            if k.get("time_tag", "") <= ts:
                best = _parse_kp(k)
            else:
                break
        return best

    series = []
    for p in geomag:
        kp = _nearest_kp(p["time_tag"])
        # Real historical risk_score if a sample exists at-or-before this
        # timestamp yet, else None (NOT current_risk_score) — a None here
        # is what lets the frontend's `.defined()` on the risk line leave
        # a genuine gap instead of drawing a fake flat line back through
        # time the buffer hasn't reached yet. With tasks.py now recording
        # on every real pipeline run (~6h cadence), expect the risk line
        # to look like a stepped/sparse series rather than a smooth one —
        # that's honest given how often the underlying score actually
        # changes, not a bug.
        risk = risk_history.nearest_at_or_before(p["time_tag"], history_snapshot)
        series.append({
            "timestamp":    p["time_tag"],
            "mag_total":    p["bt_nT"],       # IMF Bt, nT
            "mag_bz":       p["bz_nT"],
            "rf_noise":     None,             # RF stays wired to hardware/mock separately
            "kp_proxy":     kp,               # null when no Kp data yet, NOT 0
            "storm_active": (kp is not None and kp >= 5.0),
            "risk_score":   risk,             # null until real history exists, NOT current value
        })

    return jsonify({
        "series":        series,
        "count":         len(series),
        "geomag_source": "NOAA SWPC (real-time L1, locked history)",
        "risk_history_samples": len(history_snapshot),
        "geomag_locked_samples": geomag_history.locked_count(),
    })


@solar_bp.route("/latest-sensor")
def latest_sensor():
    """GET /api/solar/latest-sensor — most recent sensor reading (RF only now)."""
    state = current_app.config["APP_STATE"]
    mock_sensor = state.get("mock_sensor")
    if not mock_sensor:
        return jsonify({}), 503
    latest = mock_sensor.get_latest()
    return jsonify(latest or {})