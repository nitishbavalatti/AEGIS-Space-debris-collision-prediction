"""
AEGIS Conjunction Screener
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Screens for TWO types of conjunction events:

  1. Debris × ISS    — debris approaching ISS within threshold
  2. Debris × Debris — any two debris objects approaching each other

Debris×Debris is the more dangerous scenario for Kessler cascade.
A single D×D collision at 800km creates thousands of new fragments
that threaten everything else in that shell.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import math
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


def _real_futures():
    """concurrent.futures backed by REAL OS threads even when the app calls
    eventlet.monkey_patch() (app.py does). With the patched module the workers are
    cooperative greenlets: numpy never yields, so they ran one after another and
    froze the web server / sockets for the whole screening run. Real threads let
    numpy overlap (it releases the GIL) and keep the event loop alive."""
    try:
        from eventlet import patcher
        return patcher.original("concurrent.futures")
    except Exception:
        import concurrent.futures as _f
        return _f

logger = logging.getLogger(__name__)

try:
    from sgp4.api import Satrec, jday
    SGP4_AVAILABLE = True
except ImportError:
    SGP4_AVAILABLE = False

try:
    import numpy as np
    NUMPY_OK = True
except ImportError:           # falls back to the slower per-pair python loop
    NUMPY_OK = False

try:
    from sgp4.api import SatrecArray     # missing in older sgp4 releases
except ImportError:
    SatrecArray = None


def _propagate_all(sats, jd, fr):
    """(err, r, v) for every satellite at every time, shapes (n,T), (n,T,3), (n,T,3).
    Uses SatrecArray when the installed sgp4 has it, else per-satellite
    sgp4_array, else a plain loop — so the all-pairs screener works on any
    sgp4 version instead of silently falling back to the old filter."""
    if SatrecArray is not None:
        # Chunked: one SatrecArray.sgp4() call over all objects is a single long C
        # call that holds the GIL (the web server stalled for its whole duration).
        # Between chunks other threads get to run.
        if len(sats) <= 100:
            return SatrecArray(sats).sgp4(jd, fr)
        parts = [SatrecArray(sats[k:k + 100]).sgp4(jd, fr) for k in range(0, len(sats), 100)]
        return (np.concatenate([p[0] for p in parts], axis=0),
                np.concatenate([p[1] for p in parts], axis=0),
                np.concatenate([p[2] for p in parts], axis=0))
    n, T = len(sats), len(jd)
    err = np.zeros((n, T), dtype=int)
    r = np.zeros((n, T, 3))
    v = np.zeros((n, T, 3))
    for k, sat in enumerate(sats):
        if hasattr(sat, "sgp4_array"):
            e, rr, vv = sat.sgp4_array(jd, fr)
            err[k], r[k], v[k] = e, rr, vv
        else:
            for t in range(T):
                e, rr, vv = sat.sgp4(float(jd[t]), float(fr[t]))
                err[k, t] = e
                r[k, t] = rr if e == 0 else np.nan
                v[k, t] = vv if e == 0 else np.nan
    return err, r, v

DEFAULT_SIGMA_R = 0.2
DEFAULT_SIGMA_T = 0.5
DEFAULT_SIGMA_N = 0.2
COMBINED_HBR    = 0.055   # km ISS + debris
DXD_HBR         = 0.010   # km debris + debris


def _propagate_object_bulk(tle_line1, tle_line2, times):
    if not SGP4_AVAILABLE:
        return [None] * len(times)
    try:
        sat = Satrec.twoline2rv(tle_line1, tle_line2)
    except Exception:
        return [None] * len(times)
    results = []
    for dt in times:
        try:
            jd, fr = jday(dt.year, dt.month, dt.day,
                          dt.hour, dt.minute,
                          dt.second + dt.microsecond / 1e6)
            e, r, v = sat.sgp4(jd, fr)
            if e != 0:
                results.append(None)
            else:
                results.append({"x_km": r[0], "y_km": r[1], "z_km": r[2],
                                 "vx": v[0],   "vy": v[1],   "vz": v[2]})
        except Exception:
            results.append(None)
    return results


def _alt_range_km(o, default=500.0):
    """(perigee_alt, apogee_alt) in km. Derived from MEAN_MOTION + ECCENTRICITY,
    because the Space-Track gp field is PERIAPSIS (not PERIGEE): looking up
    "PERIGEE" silently returned the apogee, so every orbit was treated as
    circular and eccentric objects crossing a shell were never compared."""
    try:
        mm  = float(o.get("MEAN_MOTION"))
        ecc = float(o.get("ECCENTRICITY"))
        n = mm * 2.0 * math.pi / 86400.0                     # rad/s
        a = (398600.4418 / (n * n)) ** (1.0 / 3.0)           # km
        return a * (1.0 - ecc) - 6378.137, a * (1.0 + ecc) - 6378.137
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    try:
        apo = float(o.get("APOAPSIS", default) or default)
        peri = float(o.get("PERIAPSIS", o.get("PERIGEE", apo)) or apo)
    except (TypeError, ValueError):
        apo = peri = default
    return min(apo, peri), max(apo, peri)


def _risk_rank(objs):
    """Highest GNN/heuristic risk first, so truncating to max_objects keeps
    the objects that matter instead of whatever happens to be first."""
    def key(o):
        try:
            return float(o.get("risk_score") or 0)
        except (TypeError, ValueError):
            return 0.0
    return sorted(objs, key=key, reverse=True)


def _refine(a, b, t_center, step_s):
    """1-second re-propagation around a coarse minimum -> true TCA / miss."""
    span = int(step_s)
    times = [t_center + timedelta(seconds=s) for s in range(-span, span + 1)]
    pa = _propagate_object_bulk(a["TLE_LINE1"], a["TLE_LINE2"], times)
    pb = _propagate_object_bulk(b["TLE_LINE1"], b["TLE_LINE2"], times)
    best = None
    for t, x, y in zip(times, pa, pb):
        if x and y:
            d = _dist(x, y)
            if best is None or d < best[0]:
                best = (d, t, _rel_vel(x, y))
    return best


def _closest_approach(a, b, pos_a, pos_b, times, thr_km, step_s):
    """
    Two-stage closest approach. Objects close at 7-14 km/s, so with a 60 s
    coarse step the nearest SAMPLE can be 200-400+ km from the true closest
    approach: thresholding the coarse minimum at 25 km misses ~90% of real
    sub-25 km conjunctions. Instead: find coarse local minima, keep those
    that COULD hide a <thr_km approach (bound uses the relative speed), and
    refine each at 1 s. Returns (miss_km, tca_datetime, rel_vel) or None.
    """
    dists = [(_dist(p, q) if p and q else None) for p, q in zip(pos_a, pos_b)]
    n, best = len(dists), None
    for i, d in enumerate(dists):
        if d is None:
            continue
        prev = dists[i - 1] if i > 0 else None
        nxt  = dists[i + 1] if i < n - 1 else None
        if (prev is not None and prev < d) or (nxt is not None and nxt < d):
            continue                      # not a local minimum
        rv = _rel_vel(pos_a[i], pos_b[i])
        if d > math.hypot(thr_km, rv * step_s * 0.6):
            continue                      # can't hide a <thr_km approach
        ref = _refine(a, b, times[i], step_s)
        if ref and (best is None or ref[0] < best[0]):
            best = ref
    return best


def _iss_shell_rank(objs, iss_alt_km, margin_km=50.0):
    """Objects whose perigee..apogee range reaches the ISS altitude come
    first (only they can approach it), then by risk. Ranking by risk alone
    mostly picked objects at 800-1800 km that can never meet a 420 km ISS."""
    def key(o):
        try:
            risk = float(o.get("risk_score") or 0)
        except (TypeError, ValueError):
            return (1, 0.0)
        lo, hi = _alt_range_km(o)
        return (0 if lo - margin_km <= iss_alt_km <= hi + margin_km else 1, -risk)
    return sorted(objs, key=key)


def _dist(a, b):
    return math.sqrt((a["x_km"]-b["x_km"])**2 +
                     (a["y_km"]-b["y_km"])**2 +
                     (a["z_km"]-b["z_km"])**2)


def _rel_vel(a, b):
    return math.sqrt((a["vx"]-b["vx"])**2 +
                     (a["vy"]-b["vy"])**2 +
                     (a["vz"]-b["vz"])**2)


def _pc(miss_dist_km, rel_vel_km_s, hbr=COMBINED_HBR):
    if rel_vel_km_s < 0.001:
        return 0.0
    sigma2 = 2 * (DEFAULT_SIGMA_R**2 + DEFAULT_SIGMA_T**2 + DEFAULT_SIGMA_N**2)
    if sigma2 < 1e-10:
        return 0.0
    return min((hbr**2 / sigma2) * math.exp(-(miss_dist_km**2) / sigma2), 1.0)


# ── 1. Debris × ISS ──────────────────────────────────────────────────────────

def screen_conjunctions(debris_objects, iss_tle,
                         window_hours=72, sample_interval_s=60,
                         miss_threshold_km=25.0, max_objects=300):
    """Screen all debris objects against ISS."""
    if not SGP4_AVAILABLE or not iss_tle or not iss_tle.get("TLE_LINE1"):
        return []

    now    = datetime.now(timezone.utc)
    times  = [now + timedelta(seconds=i * sample_interval_s)
              for i in range((window_hours * 3600) // sample_interval_s)]
    iss_pos = _propagate_object_bulk(iss_tle["TLE_LINE1"], iss_tle["TLE_LINE2"], times)

    candidates = [o for o in debris_objects
                  if o.get("TLE_LINE1") and o.get("TLE_LINE2") and not o.get("_is_mock")
                  ]
    try:
        iss_alt = float(iss_tle.get("APOAPSIS") or 420.0)
    except (TypeError, ValueError):
        iss_alt = 420.0
    candidates = _iss_shell_rank(candidates, iss_alt)[:max_objects]
    if not candidates:
        return []

    def screen_one(obj):
        positions = _propagate_object_bulk(obj["TLE_LINE1"], obj["TLE_LINE2"], times)
        ca = _closest_approach(iss_tle, obj, iss_pos, positions, times,
                               miss_threshold_km, sample_interval_s)
        if ca is None or ca[0] > miss_threshold_km:
            return None
        min_d, tca, rv = ca
        pc  = _pc(min_d, rv, COMBINED_HBR)
        return {
            "norad_id":          obj.get("NORAD_CAT_ID", "?"),
            "object_name":       obj.get("OBJECT_NAME", "UNKNOWN"),
            "miss_distance_km":  round(min_d, 3),
            "tca":               tca.isoformat(),
            "tca_hours":         round((tca - now).total_seconds() / 3600, 2),
            "relative_vel_km_s": round(rv, 3),
            "probability_of_collision": round(pc, 8),
            "pc_percent":        round(pc * 100, 6),
            "alert":  "RED" if min_d < 5 else "AMBER" if min_d < 10 else "YELLOW",
            "country_code": obj.get("COUNTRY_CODE", "—"),
            "launch_year":  obj.get("LAUNCH_YEAR",  "—"),
            "apoapsis":     obj.get("APOAPSIS",     "—"),
            "inclination":  obj.get("INCLINATION",  "—"),
            "risk_score":   obj.get("risk_score",   0),
            "_type":        "debris_iss",
        }

    events = []
    _f = _real_futures()
    with _f.ThreadPoolExecutor(max_workers=8) as ex:
        for fut in _f.as_completed({ex.submit(screen_one, o): o for o in candidates}):
            try:
                r = fut.result()
                if r:
                    events.append(r)
            except Exception as e:
                logger.debug(f"[D×ISS] {e}")

    events.sort(key=lambda e: e["miss_distance_km"])
    logger.info(f"[Conjunction] D×ISS: {len(events)} events within {miss_threshold_km}km")
    return events


# ── 2. Debris × Debris ────────────────────────────────────────────────────────

def _screen_debris_conjunctions_legacy(debris_objects,
                                window_hours=72, sample_interval_s=60,
                                miss_threshold_km=10.0, max_objects=200):
    """
    Screen debris pairs against each other.

    Pre-filters by altitude shell (±50km) and inclination (±5°) before
    running SGP4 to avoid O(n²) full propagation of all pairs.
    Each object is propagated once and reused across all its pairs.
    """
    if not SGP4_AVAILABLE:
        return []

    candidates = [o for o in debris_objects
                  if o.get("TLE_LINE1") and o.get("TLE_LINE2") and not o.get("_is_mock")
                  ]
    candidates = _risk_rank(candidates)[:max_objects]

    if len(candidates) < 2:
        return []

    now   = datetime.now(timezone.utc)
    times = [now + timedelta(seconds=i * sample_interval_s)
             for i in range((window_hours * 3600) // sample_interval_s)]

    # Pre-filter pairs by orbital proximity
    def _alt_range(o):
        return _alt_range_km(o)

    def _could_meet(a, b):
        # Compare the full perigee..apogee altitude RANGES (not apoapsis
        # alone), so eccentric orbits that cross another shell aren't missed.
        try:
            lo_a, hi_a = _alt_range(a)
            lo_b, hi_b = _alt_range(b)
            return (lo_a - 50 <= hi_b and lo_b - 50 <= hi_a and
                    abs(float(a.get("INCLINATION", 45) or 45) -
                        float(b.get("INCLINATION", 45) or 45)) < 5)
        except (ValueError, TypeError):
            return False

    pairs = [(i, j) for i in range(len(candidates))
             for j in range(i + 1, len(candidates))
             if _could_meet(candidates[i], candidates[j])]

    logger.info(f"[Conjunction] D×D: {len(candidates)} objects → {len(pairs)} candidate pairs")
    if not pairs:
        return []

    # Propagate each needed object exactly once
    needed = set(idx for pair in pairs for idx in pair)
    obj_positions = {}

    def propagate(idx):
        obj = candidates[idx]
        return idx, _propagate_object_bulk(obj["TLE_LINE1"], obj["TLE_LINE2"], times)

    _f = _real_futures()
    with _f.ThreadPoolExecutor(max_workers=8) as ex:
        for fut in _f.as_completed({ex.submit(propagate, idx): idx for idx in needed}):
            try:
                idx, pos = fut.result()
                obj_positions[idx] = pos
            except Exception as e:
                logger.debug(f"[D×D propagate] {e}")

    # Screen each pair
    events = []
    for i, j in pairs:
        if i not in obj_positions or j not in obj_positions:
            continue

        pos_i, pos_j = obj_positions[i], obj_positions[j]
        obj_a, obj_b = candidates[i], candidates[j]
        ca = _closest_approach(obj_a, obj_b, pos_i, pos_j, times,
                               miss_threshold_km, sample_interval_s)
        if ca is None or ca[0] > miss_threshold_km:
            continue
        min_d, tca, rv = ca
        pc  = _pc(min_d, rv, DXD_HBR)

        events.append({
            "norad_id":          obj_a.get("NORAD_CAT_ID", "?"),
            "object_name":       obj_a.get("OBJECT_NAME", "UNKNOWN"),
            "norad_id_b":        obj_b.get("NORAD_CAT_ID", "?"),
            "object_name_b":     obj_b.get("OBJECT_NAME", "UNKNOWN"),
            "miss_distance_km":  round(min_d, 3),
            "tca":               tca.isoformat(),
            "tca_hours":         round((tca - now).total_seconds() / 3600, 2),
            "relative_vel_km_s": round(rv, 3),
            "probability_of_collision": round(pc, 8),
            "pc_percent":        round(pc * 100, 6),
            "kessler_risk":      rv > 7.0,  # hypervelocity = Kessler trigger
            "alert": "RED" if min_d < 2 else "AMBER" if min_d < 5 else "YELLOW",
            "country_code":  obj_a.get("COUNTRY_CODE", "—"),
            "country_code_b": obj_b.get("COUNTRY_CODE", "—"),
            "launch_year":   obj_a.get("LAUNCH_YEAR",  "—"),
            "apoapsis":      obj_a.get("APOAPSIS",     "—"),
            "inclination":   obj_a.get("INCLINATION",  "—"),
            "risk_score":    obj_a.get("risk_score",   0),
            "_type":         "debris_debris",
        })

    events.sort(key=lambda e: e["miss_distance_km"])
    logger.info(f"[Conjunction] D×D: {len(events)} events within {miss_threshold_km}km")
    return events


def _dxd_event(obj_a, obj_b, min_d, tca, rv, now):
    pc = _pc(min_d, rv, DXD_HBR)
    return {
        "norad_id":          obj_a.get("NORAD_CAT_ID", "?"),
        "object_name":       obj_a.get("OBJECT_NAME", "UNKNOWN"),
        "norad_id_b":        obj_b.get("NORAD_CAT_ID", "?"),
        "object_name_b":     obj_b.get("OBJECT_NAME", "UNKNOWN"),
        "miss_distance_km":  round(min_d, 3),
        "tca":               tca.isoformat(),
        "tca_hours":         round((tca - now).total_seconds() / 3600, 2),
        "relative_vel_km_s": round(rv, 3),
        "probability_of_collision": round(pc, 8),
        "pc_percent":        round(pc * 100, 6),
        "kessler_risk":      rv > 7.0,  # hypervelocity = Kessler trigger
        "alert": "RED" if min_d < 2 else "AMBER" if min_d < 5 else "YELLOW",
        "country_code":  obj_a.get("COUNTRY_CODE", "—"),
        "country_code_b": obj_b.get("COUNTRY_CODE", "—"),
        "launch_year":   obj_a.get("LAUNCH_YEAR",  "—"),
        "apoapsis":      obj_a.get("APOAPSIS",     "—"),
        "inclination":   obj_a.get("INCLINATION",  "—"),
        "risk_score":    obj_a.get("risk_score",   0),
        "_type":         "debris_debris",
    }


def screen_debris_conjunctions(debris_objects,
                               window_hours=72, sample_interval_s=60,
                               miss_threshold_km=10.0, max_objects=200,
                               max_workers=4, start_time=None, max_events=300):
    """
    Screen debris pairs against each other (all pairs whose altitude ranges
    overlap — NO inclination filter).

    The old version only compared objects within +-5 deg inclination, which
    keeps co-orbital fragments (slow ~1 km/s passes) but drops every
    CROSSING pair (different inclinations, 7-14 km/s) — exactly the fast,
    dangerous encounters. Now:
      1. every object is propagated once, in bulk (sgp4 SatrecArray);
      2. for each pair and each 60 s sample, the closest approach inside that
         sample's +-30 s is estimated from the relative position and
         velocity (vectorised numpy) — cheap enough for all pairs;
      3. only pairs whose estimate is within threshold + 10 km are refined
         with real SGP4 at 1 s steps, and kept if truly within threshold.
    """
    if not SGP4_AVAILABLE:
        return []
    if not NUMPY_OK:
        logger.warning("[Conjunction] D×D mode: LEGACY (numpy not installed) — only objects within "
                       "5 deg inclination are compared, crossing orbits are skipped. pip install numpy")
        return _screen_debris_conjunctions_legacy(
            debris_objects, window_hours, sample_interval_s, miss_threshold_km, max_objects)

    candidates = [o for o in debris_objects
                  if o.get("TLE_LINE1") and o.get("TLE_LINE2") and not o.get("_is_mock")]
    candidates = _risk_rank(candidates)[:max_objects]

    sats, objs = [], []
    for o in candidates:
        try:
            sats.append(Satrec.twoline2rv(o["TLE_LINE1"], o["TLE_LINE2"]))
            objs.append(o)
        except Exception:
            continue
    n = len(sats)
    if n < 2:
        return []
    logger.info("[Conjunction] D×D mode: all-pairs (vectorised, no inclination filter)")

    now = start_time or datetime.now(timezone.utc)
    n_t = int((window_hours * 3600) // sample_interval_s)
    jd0, fr0 = jday(now.year, now.month, now.day, now.hour, now.minute,
                    now.second + now.microsecond / 1e6)
    jd = np.full(n_t, jd0)
    fr = fr0 + np.arange(n_t) * sample_interval_s / 86400.0
    err, r, v = _propagate_all(sats, jd, fr)
    r = r.astype(np.float32)
    v = v.astype(np.float32)
    # SGP4 can return err == 0 with absurd values for decayed/bad element sets;
    # squaring those overflowed float32 ("overflow encountered in square"). Treat
    # anything outside a physically possible LEO state as invalid, like err != 0.
    with np.errstate(invalid="ignore"):
        bad = (err != 0) | (np.abs(r).max(axis=2) > 1.0e5) | (np.abs(v).max(axis=2) > 20.0)
    r[bad] = np.nan
    v[bad] = np.nan

    ranges = np.array([_alt_range_km(o) for o in objs])
    lo, hi = ranges[:, 0], ranges[:, 1]

    # Diagnostics: a run that reports "0 events" must say WHETHER it screened
    # anything meaningful. Objects whose SGP4 state is invalid for the whole
    # window (decayed / stale element sets) can never produce an encounter.
    usable = int((~bad).any(axis=1).sum())
    logger.info(
        f"[Conjunction] D×D input: {n} objects, altitude {float(np.nanmin(lo)):.0f}-{float(np.nanmax(hi)):.0f} km "
        f"(median apoapsis {float(np.nanmedian(hi)):.0f} km); {usable}/{n} have a valid SGP4 state, "
        f"{100.0 * float(bad.mean()):.1f}% of all propagated samples are invalid"
    )
    if usable < 2:
        logger.warning("[Conjunction] D×D: fewer than 2 objects have a valid orbit state — nothing to screen "
                       "(stale/decayed element sets?)")
        return []

    # count candidate pairs (altitude-range overlap only) for the log line
    n_pairs = 0
    for i in range(n - 1):
        j = np.arange(i + 1, n)
        n_pairs += int(((lo[i] - 50 <= hi[j]) & (lo[j] - 50 <= hi[i])).sum())
    logger.info(f"[Conjunction] D×D: {n} objects → {n_pairs} candidate pairs")
    if n_pairs == 0:
        return []

    half = sample_interval_s / 2.0
    bound = miss_threshold_km + 10.0          # margin for the linear-motion estimate
    CHUNK = 100

    def screen_i(i):
        j_all = np.arange(i + 1, n)
        j_all = j_all[(lo[i] - 50 <= hi[j_all]) & (lo[j_all] - 50 <= hi[i])]
        found = []
        best = (float("inf"), i, i)           # closest coarse estimate, even if outside the threshold
        for c0 in range(0, len(j_all), CHUNK):
            js = j_all[c0:c0 + CHUNK]
            R0 = r[js] - r[i]
            Vr = v[js] - v[i]
            vv = (Vr * Vr).sum(axis=2)
            rv = (R0 * Vr).sum(axis=2)
            tau = np.clip(-rv / np.maximum(vv, 1e-9), -half, half)
            M = np.sqrt(((R0 + Vr * tau[..., None]) ** 2).sum(axis=2))
            M = np.where(np.isnan(M), np.inf, M)
            tmin = M.argmin(axis=1)
            mmin = M[np.arange(len(js)), tmin]
            if len(mmin):
                k = int(mmin.argmin())
                if mmin[k] < best[0]:
                    best = (float(mmin[k]), i, int(js[k]))
            for h in np.nonzero(mmin <= bound)[0]:
                t_idx = int(tmin[h])
                tc = now + timedelta(seconds=float(t_idx * sample_interval_s + tau[h, t_idx]))
                ref = _refine(objs[i], objs[int(js[h])], tc, sample_interval_s)
                if ref and ref[0] <= miss_threshold_km:
                    found.append((i, int(js[h]), ref))
        return found, best

    events = []
    errors, first_err = 0, None
    closest = (float("inf"), 0, 0)
    _f = _real_futures()
    with np.errstate(all="ignore"), _f.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in _f.as_completed([ex.submit(screen_i, i) for i in range(n - 1)]):
            try:
                found, best = fut.result()
                if best[0] < closest[0]:
                    closest = best
                for i, j, (min_d, tca, rv_) in found:
                    events.append(_dxd_event(objs[i], objs[j], min_d, tca, rv_, now))
            except Exception as e:
                # Was logger.debug: if every worker failed the run silently reported
                # "0 events" and nothing in the log said why.
                errors += 1
                if first_err is None:
                    import traceback
                    first_err = traceback.format_exc()
    if errors:
        logger.warning(f"[Conjunction] D×D: {errors}/{n - 1} screening workers FAILED — results are incomplete. "
                       f"First error:\n{first_err}")
    if closest[0] < float("inf"):
        a, b = objs[closest[1]], objs[closest[2]]
        logger.info(f"[Conjunction] D×D closest coarse approach in the window: {closest[0]:.1f} km "
                    f"({a.get('OBJECT_NAME', a.get('NORAD_CAT_ID'))} × {b.get('OBJECT_NAME', b.get('NORAD_CAT_ID'))})")
    else:
        logger.warning("[Conjunction] D×D: no pair produced a finite distance at all")

    events.sort(key=lambda e: e["miss_distance_km"])
    total = len(events)
    events = events[:max_events]          # keep the UI / alert list manageable (closest first)
    logger.info(f"[Conjunction] D×D: {total} events within {miss_threshold_km}km"
                + (f" (keeping the closest {max_events})" if total > max_events else ""))
    return events


def generate_mock_conjunctions(n=8):
    """Mock events for when no real TLE data is available."""
    import random
    MOCK = [
        ("38778", "FENGYUN 1C DEB",   8.2,  2.1, "PRC", "2007", "847", "98.8"),
        ("22675", "IRIDIUM 33 DEB",   14.1, 1.8, "US",  "1997", "776", "86.4"),
        ("36036", "SL-16 R/B DEB",    6.7,  3.2, "CIS", "1993", "320", "71.0"),
        ("41332", "COSMOS 2504 DEB",  19.3, 2.9, "CIS", "2015", "385", "64.9"),
        ("28903", "MICROSAT-R DEB",   11.5, 2.4, "IND", "2019", "447", "97.4"),
        ("19046", "COSMOS 1408 DEB",   4.2, 3.8, "CIS", "1982", "495", "82.5"),
        ("43641", "BREEZE-M DEB",     22.4, 1.5, "CIS", "2018", "290", "48.9"),
        ("31595", "H-2A R/B DEB",    17.8, 2.2, "JPN", "2007", "602", "98.1"),
    ]
    now    = datetime.now(timezone.utc)
    events = []
    for norad, name, miss, rv, country, launch, apo, incl in MOCK[:n]:
        hours = random.uniform(1, 70)
        tca   = now + timedelta(hours=hours)
        pc    = _pc(miss, rv)
        events.append({
            "norad_id": norad, "object_name": name,
            "miss_distance_km": round(miss + random.uniform(-0.5, 0.5), 3),
            "tca": tca.isoformat(), "tca_hours": round(hours, 2),
            "relative_vel_km_s": round(rv, 3),
            "probability_of_collision": round(pc, 8),
            "pc_percent": round(pc * 100, 6),
            "alert": "RED" if miss < 5 else "AMBER" if miss < 10 else "YELLOW",
            "country_code": country, "launch_year": launch,
            "apoapsis": apo, "inclination": incl,
            "risk_score": round(random.uniform(40, 95), 1),
            "_type": "debris_iss", "_is_mock": True,
        })
    events.sort(key=lambda e: e["miss_distance_km"])
    return events