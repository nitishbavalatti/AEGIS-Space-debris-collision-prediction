"""
AEGIS Background Tasks
"""
import threading
import time
import logging
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
    

def _get_live_kp() -> float:
    """
    Real geomagnetic Kp index from NOAA SWPC — the same source that feeds
    the Solar Storm correlation panel on the dashboard.

    Previously this pipeline pulled Kp from `mock_sensor.get_latest()`
    instead, which is a completely different (simulated) signal from
    what the frontend chart displays as "live" Kp. With
    USE_MOCK_SENSOR=false and no hardware attached, that path never
    populates at all and silently fell back to a hardcoded 2.0 on every
    single pipeline run — meaning the GNN's solar_kp_proxy feature (#7)
    was effectively never responding to real solar weather, regardless
    of what the dashboard showed. Pulling from get_solar_summary()
    (same call the /api/solar/summary route uses, and it's cached with
    a 30-min TTL — see noaa_swpc.py) makes the number the GNN scores
    against match the number the user is looking at.

    Falls back to 2.0 (quiet baseline) only if NOAA is genuinely
    unreachable, so a transient outage can't crash the pipeline.
    """
    try:
        from backend.services.noaa_swpc import get_solar_summary
        solar = get_solar_summary(days_back=1)
        kp = solar.get("max_kp_index")
        if kp is not None:
            return float(kp)
    except Exception as e:
        logger.warning(f"[Pipeline] Live Kp fetch failed: {e} — using quiet baseline (2.0)")
    return 2.0


def run_debris_pipeline(app):
    with app.app_context():
        state = app.config["APP_STATE"]
        logger.info("[Pipeline] Starting debris data refresh...")
        try:
            from backend.services.spacetrack import get_debris_tle
            if getattr(Config, "DATA_MODE", "REAL") == "MOCK":
                debris = _generate_mock_debris(5000)
            else:
                debris = get_debris_tle(limit=5000)
                if not debris:
                    logger.warning("[Pipeline] No TLE data — using mock debris")
                    debris = _generate_mock_debris(5000)
                else:
                    if len(debris) < 5000:
                        debris.extend(_generate_mock_debris(5000 - len(debris)))

            # Real NOAA Kp — same source the dashboard's solar panel reads,
            # so the GNN's risk scoring and the on-screen storm status can
            # never silently disagree with each other.
            kp = _get_live_kp()
            logger.info(f"[Pipeline] Using live NOAA Kp={kp:.2f} for GNN scoring")

            if getattr(Config, "DATA_MODE", "REAL") == "REAL":
                from backend.data.pipeline import tle_to_position
                for obj in debris:
                    t1, t2 = obj.get("TLE_LINE1"), obj.get("TLE_LINE2")
                    if t1 and t2:
                        pos = tle_to_position(t1, t2)
                        if pos:
                            obj["lat"]    = pos["lat"]
                            obj["lon"]    = pos["lon"]
                            obj["alt_km"] = pos["alt_km"]

            gnn_model = state.get("gnn_model")
            if gnn_model:
                from backend.gnn.model import predict_risk

                # Split by apoapsis — only genuinely LEO-range objects go
                # through the GNN; high-apogee objects get the heuristic.
                gnn_eligible = [o for o in debris if _get_apoapsis(o) <= GNN_MAX_APOAPSIS_KM]
                gnn_excluded = [o for o in debris if _get_apoapsis(o) > GNN_MAX_APOAPSIS_KM]

                if gnn_eligible:
                    gnn_eligible = predict_risk(gnn_model, gnn_eligible, kp_index=kp)
                    for o in gnn_eligible:
                        o["_scoring_method"] = "gnn"
                if gnn_excluded:
                    gnn_excluded = _score_debris(gnn_excluded)
                    for o in gnn_excluded:
                        o["_scoring_method"] = "heuristic_high_apogee"

                debris = gnn_eligible + gnn_excluded
                logger.info(
                    f"[Pipeline] GNN scored {len(gnn_eligible)} objects (apoapsis <= {GNN_MAX_APOAPSIS_KM:.0f}km), "
                    f"heuristic-scored {len(gnn_excluded)} high-apogee objects (> {GNN_MAX_APOAPSIS_KM:.0f}km)"
                )
            else:
                debris = _score_debris(debris)

            state["debris_objects"] = debris
            state["last_tle_fetch"] = time.time()

            # Compute the aggregate score FIRST, before anything downstream
            # reads it. Previously the Mongo snapshot below was stored
            # using the OLD state["current_risk_score"] (from the
            # *previous* pipeline run, 6h stale) because it read that key
            # before this run's score had been computed — and on the very
            # first pipeline run after a fresh deploy, the key doesn't
            # exist yet at all, which raised KeyError and got swallowed by
            # the outer `except Exception` as a silent "[Pipeline] Error"
            # with no debris ever getting scored that run.
            scores = sorted([o.get("risk_score", 0) for o in debris], reverse=True)
            top10  = scores[:max(1, len(scores) // 10)]
            state["current_risk_score"] = round(sum(top10) / len(top10), 1)

            # Record this REAL, freshly-computed score into the rolling
            # history buffer that powers the Solar Correlation panel's red
            # risk line (see backend/services/risk_history.py). This is
            # the authoritative sample point — one real point per pipeline
            # run (~6h cadence, or on-demand if triggered manually), as
            # opposed to the /api/solar/* routes' fallback sampling which
            # just repeats whatever this value currently is between runs.
            risk_history.record_risk_score(state["current_risk_score"])

            mongo = state.get("mongo_store")
            if mongo and mongo.connected:
                mongo.store_debris_snapshot(debris, state["current_risk_score"])

            logger.info(f"[Pipeline] Done. {len(debris)} objects. Top: {scores[0]:.1f}")
        except Exception as e:
            logger.error(f"[Pipeline] Error: {e}", exc_info=True)


def run_conjunction_screener(app):
    """
    Runs both D×ISS and D×D conjunction screening.
    Combines all results, emits WebSocket alerts for RED/AMBER events.
    """
    with app.app_context():
        state = app.config["APP_STATE"]
        logger.info("[Conjunction] Starting screening run (D×ISS + D×D)...")

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
                from backend.services.spacetrack import get_iss_tle

                iss_tle = get_iss_tle()

                # Run both screeners
                dxiss_events = screen_conjunctions(
                    debris_objects=debris,
                    iss_tle=iss_tle,
                    window_hours=72,
                    sample_interval_s=60,
                    miss_threshold_km=25.0,
                    max_objects=300,
                )

                dxd_events = screen_debris_conjunctions(
                    debris_objects=debris,
                    window_hours=72,
                    sample_interval_s=60,
                    miss_threshold_km=10.0,
                    max_objects=200,
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

            # Emit WebSocket alerts for RED and AMBER events
            from backend.app import socketio
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
                if iss_pos and debris:
                    iss_lat = math.radians(iss_pos.get("lat", 0) or 0)
                    iss_lon = math.radians(iss_pos.get("lon", 0) or 0)
                    iss_alt = (iss_pos.get("alt_km", 408) or 408) + 6371.0
                    iss_eci = {
                        "x_km": iss_alt * math.cos(iss_lat) * math.cos(iss_lon),
                        "y_km": iss_alt * math.cos(iss_lat) * math.sin(iss_lon),
                        "z_km": iss_alt * math.sin(iss_lat),
                    }
                    propagated = []
                    for obj in debris[:200]:
                        t1, t2 = obj.get("TLE_LINE1"), obj.get("TLE_LINE2")
                        if t1 and t2:
                            pos = tle_to_position(t1, t2)
                            if pos:
                                propagated.append({
                                    "norad_id": obj.get("NORAD_CAT_ID"),
                                    "name":     obj.get("OBJECT_NAME", "Unknown"),
                                    "position": pos,
                                })
                    proximity = compute_iss_proximity(iss_eci, propagated)
                    state["iss_proximity"] = proximity
        except Exception as e:
            logger.error(f"[ISS Updater] Error: {e}")
        time.sleep(60)


def start_background_tasks(app):
    logger.info("[Tasks] Starting background tasks...")

    t1 = threading.Thread(target=run_debris_pipeline, args=(app,), daemon=True)
    t1.start()

    t2 = threading.Thread(target=run_iss_updater, args=(app,), daemon=True)
    t2.start()

    def periodic_debris():
        while True:
            time.sleep(6 * 3600)
            run_debris_pipeline(app)

    threading.Thread(target=periodic_debris, daemon=True).start()

    def conjunction_loop():
        time.sleep(30)             # wait for TLE data to load first
        run_conjunction_screener(app)
        while True:
            time.sleep(30 * 60)   # re-screen every 30 minutes
            run_conjunction_screener(app)

    threading.Thread(target=conjunction_loop, daemon=True).start()

    logger.info("[Tasks] All background tasks started")


def _score_debris(debris):
    import random
    for obj in debris:
        if "risk_score" in obj and obj["risk_score"] is not None:
            continue
        try:
            alt   = float(obj.get("APOAPSIS") or obj.get("PERIGEE") or 500) or 500
            bstar = abs(float(obj.get("BSTAR", 0) or 0))
            ecc   = float(obj.get("ECCENTRICITY", 0) or 0)
            alt_score   = max(0, (900 - alt) / 900) * 35
            bstar_score = min(bstar * 80000, 35)
            ecc_score   = min(ecc * 200, 20)
            seed_val    = obj.get("NORAD_CAT_ID", 0) or 0
            random.seed(int(seed_val) * 123)
            jitter = random.uniform(0, 10)
            random.seed()
            score = min(round(alt_score + bstar_score + ecc_score + jitter, 1), 100)
        except (ValueError, TypeError):
            score = round(random.uniform(5, 40), 1)
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