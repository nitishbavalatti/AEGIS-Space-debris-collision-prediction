"""
AEGIS Background Tasks
"""
import threading
import time
import logging
from datetime import datetime, timezone
from backend.config import Config
from backend.buzzer_alert import publish_buzzer_state
from backend.services import risk_history

logger = logging.getLogger(__name__)

# GNN altitude features saturate at 2000km (see model.py's alt/2000.0 cap) —
# above this, objects are typically GTO-leftover rocket bodies (low perigee
# for drag, but a very high apogee), a completely different risk regime than
# the 30-day LEO-shell-decay scenario the GNN was trained on. Score these
# with the heuristic instead of letting the GNN extrapolate wildly outside
# its training distribution.
GNN_MAX_APOAPSIS_KM = 2000.0


def _get_apoapsis(obj: dict) -> float:
    try:
        return float(obj.get("APOAPSIS", 0) or 0)
    except (ValueError, TypeError):
        return 0.0
    

def _run_blocking(fn, *args, **kwargs):
    """
    Run CPU-heavy work (conjunction screening, GNN graph build/inference) in a
    REAL OS thread. app.py calls eventlet.monkey_patch(), which turns every
    threading.Thread into a cooperative greenlet: numpy/torch never yield, so a
    heavy call made directly froze the web server, the socket connection (the
    dashboard flipped to OFFLINE) and the sensor loop until it finished — over a
    minute for the D×D screener. tpool.execute runs it in a native thread and
    keeps the event loop serving.
    """
    try:
        from eventlet import tpool
    except ImportError:
        return fn(*args, **kwargs)
    return tpool.execute(fn, *args, **kwargs)


def _get_live_kp() -> float:
    """
    Real geomagnetic Kp from NOAA SWPC (same source as the dashboard's solar
    panel), for the GNN's solar_kp_proxy feature.

    Uses ONLY the Kp feed. This used to call get_solar_summary(), which also
    fetches flares, CMEs and geomag one after another — each with a 15-20 s
    timeout — so on a slow/blocked NOAA connection the debris pipeline sat for
    over a minute before showing anything. It is now called only AFTER the
    debris has already been published, so a slow NOAA can delay the GNN
    refinement but never the debris appearing.

    Falls back to 2.0 (quiet baseline) if NOAA is unreachable.
    """
    try:
        from backend.services.noaa_swpc import get_kp_history, _parse_kp
        events, ok = get_kp_history(days_back=1)
        if ok and events:
            return float(max(_parse_kp(e) for e in events))
    except Exception as e:
        logger.warning(f"[Pipeline] Live Kp fetch failed: {e} — using quiet baseline (2.0)")
    return 2.0


def _emit_debris_updated(app, stage: str, state: dict):
    """Tell connected dashboards to re-fetch debris. Without this the frontend
    only re-fetched every 10 minutes, so it kept showing the startup mock
    objects long after the real data was ready."""
    try:
        app.extensions["socketio"].emit("debris_updated", {
            "stage":      stage,                       # "heuristic" | "final"
            "count":      len(state.get("debris_objects", [])),
            "risk_score": state.get("current_risk_score", 0),
            "is_mock":    bool(state.get("debris_is_mock")),
        })
    except Exception as e:
        logger.debug(f"[Pipeline] debris_updated emit failed: {e}")


def _publish_debris(app, state: dict, debris: list, stage: str):
    """Atomically swap in a new debris list, update the aggregate score and
    notify clients."""
    scores = sorted((float(o.get("risk_score") or 0) for o in debris), reverse=True)
    if scores:
        top10 = scores[:max(1, len(scores) // 10)]
        state["current_risk_score"] = round(sum(top10) / len(top10), 1)
    state["debris_is_mock"] = bool(debris) and all(o.get("_is_mock") for o in debris)
    state["debris_stage"] = stage
    state["debris_objects"] = debris          # single assignment: readers never see a half-built list
    _emit_debris_updated(app, stage, state)


# ── Unusable element sets ─────────────────────────────────────────────────────
# The live catalogue is the highest-BSTAR debris, which is dominated by objects in
# their last days on orbit. Some of those have an apoapsis below 100 km (already
# re-entered — the dashboard showed a "SL-8 DEB" at 89 km at #2 in the threat feed)
# or an element set weeks old. SGP4 returns errors/garbage for them, they score
# "high risk" purely because they are low and draggy, and they were being picked
# FIRST by the conjunction screener (it screens the top 800 by risk) — so the
# screened set was mostly objects that cannot produce a real encounter.
MIN_VALID_APOAPSIS_KM = 160.0     # below this an object re-enters within hours
MAX_ELEMENT_AGE_DAYS  = 30        # older TLEs are off by hundreds of km


def _parse_epoch(s):
    try:
        s = str(s).strip().replace(" ", "T")
        if "." in s:
            head, frac = s.split(".", 1)
            s = head + "." + frac[:6]
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _drop_unusable(debris, now=None):
    """Returns (kept, {"decayed": n, "re_entering": n, "stale_epoch": n}).
    Fields that are missing or unparseable never cause a drop. If the filter
    would remove EVERYTHING (e.g. an unexpected data format) the original list
    is returned untouched, so a bad assumption can never blank the dashboard."""
    now = now or datetime.now(timezone.utc)
    counts = {"decayed": 0, "re_entering": 0, "stale_epoch": 0}
    kept = []
    for o in debris:
        if o.get("DECAY_DATE"):
            counts["decayed"] += 1
            continue
        try:
            apo = float(o.get("APOAPSIS"))
        except (TypeError, ValueError):
            apo = None
        if apo is not None and 0 < apo < MIN_VALID_APOAPSIS_KM:
            counts["re_entering"] += 1
            continue
        ep = _parse_epoch(o.get("EPOCH")) if o.get("EPOCH") else None
        if ep is not None and (now - ep).total_seconds() > MAX_ELEMENT_AGE_DAYS * 86400:
            counts["stale_epoch"] += 1
            continue
        kept.append(o)
    if not kept and debris:
        logger.warning("[Pipeline] Unusable-object filter would remove every object — ignoring it")
        return debris, {k: 0 for k in counts}
    return kept, counts


_PIPELINE_LOCK = threading.Lock()


def run_debris_pipeline(app) -> bool:
    """
    Fetch -> publish -> refine. Returns True on success, False on failure
    (the caller retries).

      Stage 1  fetch TLEs, propagate positions, heuristic-score, PUBLISH.
               Real debris is on screen within seconds of the data arriving.
      Stage 2  GNN scoring (needs NOAA Kp + the graph build), PUBLISH again.
               If the GNN is disabled or fails, stage-1 scores simply stay.
    """
    if not _PIPELINE_LOCK.acquire(blocking=False):
        logger.info("[Pipeline] Already running — skipping duplicate trigger")
        return True
    try:
        with app.app_context():
            state = app.config["APP_STATE"]
            logger.info("[Pipeline] Starting debris data refresh...")
            t0 = time.time()
            try:
                from backend.services.spacetrack import get_debris_tle, last_debris_source
                real_mode = getattr(Config, "DATA_MODE", "REAL") != "MOCK"
                state["debris_data_stale"] = False

                if not real_mode:
                    debris = _generate_mock_debris(5000)
                else:
                    raw = get_debris_tle(limit=5000)      # disk cache / stale fallback inside; raises if nothing
                    if not raw:
                        raise RuntimeError("Space-Track returned no TLE records")
                    # True when Space-Track was unreachable and an OLD disk copy was
                    # used. The loop below then retries in minutes, not 6 hours.
                    state["debris_data_stale"] = (last_debris_source(5000) == "disk-stale")
                    # Copy: the fetch layer caches these dicts, and the pipeline
                    # adds keys to them. Never pad with mock objects — fabricated
                    # objects must not be mixed into a real catalogue.
                    debris = [dict(o) for o in raw]
                    debris, dropped = _drop_unusable(debris)
                    if any(dropped.values()):
                        logger.info(
                            f"[Pipeline] Excluded {sum(dropped.values())} unusable objects of {len(raw)}: "
                            f"{dropped['re_entering']} re-entering (apoapsis < {MIN_VALID_APOAPSIS_KM:.0f} km), "
                            f"{dropped['decayed']} decayed, {dropped['stale_epoch']} with elements older than "
                            f"{MAX_ELEMENT_AGE_DAYS} days"
                        )

                if real_mode:
                    from backend.data.pipeline import tle_to_position
                    now = datetime.now(timezone.utc)
                    for k, obj in enumerate(debris):
                        if k % 250 == 0:
                            time.sleep(0)   # app.py runs under eventlet: yield so the web server isn't starved
                        t1, t2 = obj.get("TLE_LINE1"), obj.get("TLE_LINE2")
                        if t1 and t2:
                            pos = tle_to_position(t1, t2, now)
                            if pos:
                                obj["lat"]    = pos["lat"]
                                obj["lon"]    = pos["lon"]
                                obj["alt_km"] = pos["alt_km"]

                # ── Stage 1: heuristic scores, publish immediately ──────────
                debris = _score_debris(debris)
                for o in debris:
                    o["_scoring_method"] = "heuristic"
                _publish_debris(app, state, debris, "heuristic")
                logger.info(f"[Pipeline] Published {len(debris)} objects (heuristic scores) "
                            f"in {time.time() - t0:.1f}s")

                # ── Stage 2: GNN refinement ─────────────────────────────────
                gnn_model = state.get("gnn_model")
                if gnn_model:
                    try:
                        from backend.gnn.model import predict_risk
                        kp = _get_live_kp()
                        logger.info(f"[Pipeline] Using live NOAA Kp={kp:.2f} for GNN scoring")

                        # Only genuinely LEO-range objects go through the GNN;
                        # high-apogee objects keep the heuristic score.
                        eligible = [o for o in debris if _get_apoapsis(o) <= GNN_MAX_APOAPSIS_KM]
                        excluded = [o for o in debris if _get_apoapsis(o) > GNN_MAX_APOAPSIS_KM]
                        if eligible:
                            eligible = _run_blocking(predict_risk, gnn_model, eligible, kp_index=kp)
                            for o in eligible:
                                o["_scoring_method"] = "gnn"
                        for o in excluded:
                            o["_scoring_method"] = "heuristic_high_apogee"
                        debris = eligible + excluded
                        _publish_debris(app, state, debris, "final")
                        logger.info(
                            f"[Pipeline] GNN scored {len(eligible)} objects (apoapsis <= "
                            f"{GNN_MAX_APOAPSIS_KM:.0f}km), heuristic-scored {len(excluded)} "
                            f"high-apogee objects"
                        )
                    except Exception as e:
                        logger.error(f"[Pipeline] GNN scoring failed — keeping heuristic scores: {e}",
                                     exc_info=True)
                else:
                    logger.info("[Pipeline] GNN not loaded — objects are heuristic-scored")

                debris = state["debris_objects"]
                state["last_tle_fetch"] = time.time()
                score_now = state.get("current_risk_score", 0.0)
                risk_history.record_risk_score(score_now)
                mongo = state.get("mongo_store")
                if mongo and mongo.connected:
                    mongo.store_debris_snapshot(debris, score_now)

                top = max((float(o.get("risk_score") or 0) for o in debris), default=0.0)
                logger.info(f"[Pipeline] Done. {len(debris)} objects in {time.time() - t0:.1f}s. Top: {top:.1f}")
                return True

            except Exception as e:
                logger.error(f"[Pipeline] Error: {e}", exc_info=True)
                # Nothing real to show (first run, no disk cache, Space-Track down):
                # show clearly-flagged demo objects rather than an empty globe.
                if not state.get("debris_objects"):
                    demo = _score_debris(_generate_mock_debris(500))
                    _publish_debris(app, state, demo, "demo")
                    logger.warning("[Pipeline] Showing 500 DEMO objects until real data is available")
                return False
    finally:
        _PIPELINE_LOCK.release()


def run_conjunction_screener(app) -> bool:
    """
    Runs both D×ISS and D×D conjunction screening.
    Combines all results, emits WebSocket alerts for RED/AMBER events.
    Returns True if the run completed with both screeners, False if it failed or
    had to skip D×ISS (the caller then retries sooner than the usual 30 min).
    """
    complete = True
    with app.app_context():
        state = app.config["APP_STATE"]
        logger.info("[Conjunction] Starting screening run (D×ISS + D×D)...")
        # Lets the UI tell "still screening" from "screened, nothing found" —
        # both used to look like "CONJUNCTIONS · 0".
        state["conjunction_screening"] = True

        try:
            from backend.data.conjunction_screener import (
                screen_conjunctions,
                screen_debris_conjunctions,
                generate_mock_conjunctions,
            )

            debris   = state.get("debris_objects", [])
            is_mock  = getattr(Config, "DATA_MODE", "REAL") == "MOCK"
            real_count = sum(1 for o in debris if o.get("TLE_LINE1") and not o.get("_is_mock"))

            if is_mock or real_count == 0:
                # No real TLE data — generate mock events so UI has something
                conjunctions = generate_mock_conjunctions(n=8)
                logger.info(f"[Conjunction] Mock mode — {len(conjunctions)} mock events")
            else:
                # Previously `iss_tle = get_iss_tle()` raised when Space-Track was
                # unreachable, which aborted the WHOLE run: no D×D either, and
                # state["conjunctions"] stayed empty for the next 30 minutes
                # ("CONJUNCTIONS · 0"). The ISS TLE is only needed for D×ISS.
                iss_tle = _cached_iss_tle()
                if iss_tle:
                    dxiss_events = _run_blocking(
                        screen_conjunctions,
                        debris_objects=debris,
                        iss_tle=iss_tle,
                        window_hours=72,
                        sample_interval_s=60,
                        miss_threshold_km=25.0,
                        max_objects=300,
                    )
                else:
                    dxiss_events = []
                    complete = False
                    logger.warning("[Conjunction] No ISS TLE (Space-Track unreachable, nothing cached) — "
                                   "skipping D×ISS this run; D×D screening continues")

                dxd_events = _run_blocking(
                    screen_debris_conjunctions,
                    debris_objects=debris,
                    window_hours=72,
                    sample_interval_s=60,
                    # 25 km / 800 objects: the measured miss-distance error vs
                    # Space-Track is ~18 km (median), so a 10 km cut-off dropped
                    # real events. Alert tiers (RED < 2 km, AMBER < 5 km) are
                    # unchanged, so only the timeline gains the wider YELLOW band.
                    miss_threshold_km=25.0,
                    max_objects=800,
                )

                # Combine and sort by miss distance
                conjunctions = sorted(
                    dxiss_events + dxd_events,
                    key=lambda e: e["miss_distance_km"]
                )

                logger.info(
                    f"[Conjunction] Total: {len(conjunctions)} events "
                    f"({len(dxiss_events)} D×ISS, {len(dxd_events)} D×D)"
                )

            state["conjunctions"]              = conjunctions
            publish_buzzer_state(state["conjunctions"])
            state["last_conjunction_screen"]   = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                app.extensions["socketio"].emit("conjunctions_updated", {
                    "count": sum(1 for c in conjunctions if not c.get("_is_mock")),
                    "last_screened": state["last_conjunction_screen"],
                })
            except Exception as e:
                logger.debug(f"[Conjunction] conjunctions_updated emit failed: {e}")

            # Emit WebSocket alerts for RED and AMBER events
            # Use the socketio instance attached to THIS app. Importing
            # backend.app here re-executes it as a second module when it was
            # launched as __main__, creating a second SocketIO with no
            # connected clients, so alerts were emitted into the void.
            socketio = app.extensions["socketio"]
            critical = [c for c in conjunctions if c.get("alert") in ("RED", "AMBER")
                        and not c.get("_is_mock")]
            if critical:
                socketio.emit("conjunction_alert", {
                    "count":  len(critical),
                    "events": critical[:10],
                })
                # Log breakdown
                dxiss_crit = [c for c in critical if c.get("_type") == "debris_iss"]
                dxd_crit   = [c for c in critical if c.get("_type") == "debris_debris"]
                logger.warning(
                    f"[Conjunction] ⚠ {len(critical)} critical events — "
                    f"{len(dxiss_crit)} D×ISS, {len(dxd_crit)} D×D — "
                    f"nearest: {critical[0]['object_name']} "
                    f"at {critical[0]['miss_distance_km']}km"
                )

        except Exception as e:
            logger.error(f"[Conjunction] Screener error: {e}", exc_info=True)
            complete = False
        finally:
            state["conjunction_screening"] = False
    return complete


_iss_tle_cache = {"t": 0.0, "v": None}


def _cached_iss_tle(ttl_s: int = 1800):
    """ISS TLE changes slowly; don't hit Space-Track every minute."""
    now = time.time()
    if _iss_tle_cache["v"] is None or now - _iss_tle_cache["t"] > ttl_s:
        try:
            from backend.services.spacetrack import get_iss_tle
            _iss_tle_cache["v"] = get_iss_tle()
            _iss_tle_cache["t"] = now
        except Exception as e:
            logger.warning(f"[ISS Updater] ISS TLE fetch failed: {e}")
    return _iss_tle_cache["v"]


def run_iss_updater(app):
    while True:
        try:
            with app.app_context():
                state = app.config["APP_STATE"]
                from backend.services.n2yo import get_iss_position
                from backend.data.pipeline import tle_to_position, compute_iss_proximity
                import math

                iss_pos = get_iss_position()
                state["iss_position"] = iss_pos

                # Persist ISS position independent of debris state. This
                # used to be nested inside `if iss_pos and debris:` below,
                # coupled to proximity computation — which meant that if
                # run_debris_pipeline() ever failed before populating
                # state["debris_objects"] (e.g. a transient Space-Track
                # auth/rate-limit error, swallowed by that function's own
                # blanket except-and-log), `debris` here would stay `[]`
                # forever and mongo.store_iss_position() would silently
                # stop running too — even though get_iss_position() was
                # still succeeding every single minute. One unrelated
                # pipeline failure shouldn't be able to kill ISS
                # persistence as a side effect. Do this write first,
                # unconditionally on iss_pos alone.
                mongo = state.get("mongo_store")
                if iss_pos and mongo and mongo.connected:
                    mongo.store_iss_position(iss_pos)

                debris = state.get("debris_objects", [])
                iss_tle = _cached_iss_tle()
                # ONE timestamp for the ISS and every debris object. Propagating
                # each at its own datetime.now() spread the positions over the
                # ~0.3 s the loop takes — at ~7.7 km/s that is a ~2-3 km error,
                # comparable to the 5/10 km alert thresholds.
                now = datetime.now(timezone.utc)
                iss_eci = None
                if iss_tle:
                    p = tle_to_position(iss_tle.get("TLE_LINE1", ""), iss_tle.get("TLE_LINE2", ""), now)
                    if p:
                        # SAME frame (SGP4 TEME) as the debris positions below.
                        iss_eci = {"x_km": p["x_km"], "y_km": p["y_km"], "z_km": p["z_km"]}
                # Needs only the ISS TLE and the debris — NOT the N2YO position,
                # so a missing/invalid N2YO key no longer disables proximity.
                if debris and iss_eci:
                    propagated = []
                    # Every real object, not just the first 1000 (which, in
                    # BSTAR order, mostly can't be anywhere near the ISS).
                    for k, obj in enumerate(debris):
                        if k % 250 == 0:
                            time.sleep(0)   # yield to the eventlet hub
                        t1, t2 = obj.get("TLE_LINE1"), obj.get("TLE_LINE2")
                        if not (t1 and t2) or obj.get("_is_mock"):
                            continue
                        pos = tle_to_position(t1, t2, now)
                        if pos:
                            propagated.append({
                                "norad_id": obj.get("NORAD_CAT_ID"),
                                "name":     obj.get("OBJECT_NAME", "Unknown"),
                                "position": pos,
                            })
                    proximity = compute_iss_proximity(iss_eci, propagated)
                    proximity["checked"] = len(propagated)
                    proximity["as_of"] = now.isoformat()
                    state["iss_proximity"] = proximity
        except Exception as e:
            logger.error(f"[ISS Updater] Error: {e}")
        time.sleep(60)


def start_background_tasks(app):
    logger.info("[Tasks] Starting background tasks...")

    def debris_loop():
        # One worker: run now, refresh every 6 h, and on FAILURE retry with
        # backoff (1, 2, 4, 8, 15 min). Previously a single failed Space-Track
        # call left the startup mock data on screen for the full 6 hours.
        failures = 0
        while True:
            ok = run_debris_pipeline(app)
            stale = ok and bool(app.config["APP_STATE"].get("debris_data_stale"))
            if ok and not stale:
                failures = 0
                time.sleep(6 * 3600)
            else:
                failures += 1
                wait = min(60 * 2 ** (failures - 1), 900)
                why = "Space-Track unreachable — showing STALE cached data" if stale else "failed"
                logger.warning(f"[Pipeline] {why} (attempt {failures}) — retrying in {wait // 60} min")
                time.sleep(wait)

    threading.Thread(target=debris_loop, daemon=True).start()

    def noaa_warmup():
        # Prefetch NOAA data now, in the background, so the Solar Weather and
        # Correlation panels have it when the page loads instead of waiting on
        # a cold network fetch (which, on a slow DNS, took 15-30 s).
        try:
            from backend.services.noaa_swpc import warm_cache
            warm_cache()
            logger.info("[Tasks] NOAA cache warmed")
        except Exception as e:
            logger.warning(f"[Tasks] NOAA warm-up failed: {e}")

    threading.Thread(target=noaa_warmup, daemon=True).start()

    t2 = threading.Thread(target=run_iss_updater, args=(app,), daemon=True)
    t2.start()

    def conjunction_loop():
        # Wait until the first debris pipeline run has actually finished
        # (a fixed 30 s sleep raced it and produced mock-only first results).
        state = app.config["APP_STATE"]
        waited = 0
        while not state.get("last_tle_fetch") and waited < 600:
            time.sleep(5)
            waited += 5
        ok = run_conjunction_screener(app)
        quick = 0
        while True:
            if ok:
                quick = 0
                time.sleep(30 * 60)               # re-screen every 30 minutes
            else:
                quick += 1
                wait = min(5 * 60 * 2 ** (quick - 1), 30 * 60)   # 5, 10, 20, 30 min
                logger.warning(f"[Conjunction] Incomplete run — retrying in {wait // 60} min")
                time.sleep(wait)
            ok = run_conjunction_screener(app)

    threading.Thread(target=conjunction_loop, daemon=True).start()

    logger.info("[Tasks] All background tasks started")


def _score_debris(debris):
    """
    PLACEHOLDER heuristic (used until a GNN checkpoint is loaded, and for
    high-apogee objects). It is a drag/low-altitude/eccentricity proxy, NOT a
    collision probability, and should be described that way.

    Two changes from the original:
      - The BSTAR term used to be min(bstar*80000, 35), which saturates at
        BSTAR 4.4e-4. The live catalogue is the 5,000 HIGHEST-BSTAR objects, so
        most sat at the cap and the score stopped discriminating (half the catalogue
        landed in HIGH). It is now log-scaled over 1e-4..3e-2.
      - The up-to-10-point pseudo-random "jitter" (seeded from the NORAD id) is
        gone: a risk score must not contain invented noise.
    """
    import math
    for obj in debris:
        if "risk_score" in obj and obj["risk_score"] is not None:
            continue
        try:
            alt   = float(obj.get("APOAPSIS") or obj.get("PERIGEE") or 500) or 500
            bstar = abs(float(obj.get("BSTAR", 0) or 0))
            ecc   = float(obj.get("ECCENTRICITY", 0) or 0)
            alt_score   = max(0, (900 - alt) / 900) * 35
            if bstar > 0:
                frac = (math.log10(bstar) + 4.0) / 2.5          # 1e-4 -> 0, 3e-2 -> ~1
                bstar_score = max(0.0, min(frac, 1.0)) * 35
            else:
                bstar_score = 0.0
            ecc_score   = min(ecc * 200, 20)
            score = min(round(alt_score + bstar_score + ecc_score, 1), 100)
        except (ValueError, TypeError):
            score = 10.0
        obj["risk_score"] = score
        obj["risk_level"] = (
            "CRITICAL" if score >= 80 else
            "HIGH"     if score >= 60 else
            "MEDIUM"   if score >= 40 else "LOW"
        )
    return debris


def _generate_mock_debris(count=500):
    import random, math
    COUNTRIES = ["US", "CIS", "PRC", "ESA", "JPN", "IND", "FRA", "GBR"]
    NAMES = ["COSMOS DEB", "SL-8 DEB", "FENGYUN DEB", "IRIDIUM DEB",
             "ARIANE DEB", "DELTA DEB", "CENTAUR DEB", "TITAN DEB"]
    ALT_SHELLS = [(380,430,.15),(500,600,.25),(750,900,.30),(900,1200,.20),(1200,2000,.10)]
    debris = []
    for i in range(count):
        r = random.random()
        cumulative, alt_min, alt_max = 0, 400, 800
        for lo, hi, w in ALT_SHELLS:
            cumulative += w
            if r <= cumulative:
                alt_min, alt_max = lo, hi
                break
        alt  = random.uniform(alt_min, alt_max)
        incl = random.uniform(28, 98)
        ecc  = abs(random.gauss(0.002, 0.003))
        bstar = abs(random.gauss(0.00005, 0.00008))
        raan = random.uniform(0, 360)
        argp = random.uniform(0, 360)
        mm   = 84.49 / math.sqrt((6371+alt)**3/398600.4418) * (1/(2*math.pi)) * 86400
        launch_year = random.randint(1975, 2023)
        u    = math.radians(argp + random.uniform(0, 360))
        incl_r = math.radians(incl)
        obj_lat = math.degrees(math.asin(math.sin(incl_r) * math.sin(u)))
        obj_lon = ((math.degrees(math.radians(raan) + math.atan2(
            math.cos(incl_r)*math.sin(u), math.cos(u))) + random.uniform(-180,180) + 180) % 360) - 180
        debris.append({
            "NORAD_CAT_ID": str(20000 + i),
            "OBJECT_NAME":  f"{random.choice(NAMES)} {i+1}",
            "OBJECT_TYPE":  "DEBRIS",
            "COUNTRY_CODE": random.choice(COUNTRIES),
            "LAUNCH_YEAR":  str(launch_year),
            "APOAPSIS":     str(round(alt + random.uniform(0, 20), 1)),
            "PERIGEE":      str(round(alt - random.uniform(0, 20), 1)),
            "INCLINATION":  str(round(incl, 4)),
            "ECCENTRICITY": f"{ecc:.7f}",
            "BSTAR":        str(bstar),
            "MEAN_MOTION":  str(round(mm, 8)),
            "RA_OF_ASC_NODE": str(round(raan, 4)),
            "ARG_OF_PERICENTER": str(round(argp, 4)),
            "lat": round(obj_lat, 4), "lon": round(obj_lon, 4),
            "TLE_LINE1": "", "TLE_LINE2": "", "_is_mock": True,
        })
    logger.info(f"[Pipeline] Generated {len(debris)} mock debris objects")
    return debris