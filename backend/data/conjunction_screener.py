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

logger = logging.getLogger(__name__)

try:
    from sgp4.api import Satrec, jday
    SGP4_AVAILABLE = True
except ImportError:
    SGP4_AVAILABLE = False

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
                  ][:max_objects]
    if not candidates:
        return []

    def screen_one(obj):
        positions = _propagate_object_bulk(obj["TLE_LINE1"], obj["TLE_LINE2"], times)
        min_d, tca_idx = float("inf"), None
        for i, (ip, op) in enumerate(zip(iss_pos, positions)):
            if ip and op:
                d = _dist(ip, op)
                if d < min_d:
                    min_d, tca_idx = d, i
        if min_d > miss_threshold_km or tca_idx is None:
            return None
        rv  = _rel_vel(iss_pos[tca_idx], positions[tca_idx])
        tca = times[tca_idx]
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
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed({ex.submit(screen_one, o): o for o in candidates}):
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

def screen_debris_conjunctions(debris_objects,
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
                  ][:max_objects]

    if len(candidates) < 2:
        return []

    now   = datetime.now(timezone.utc)
    times = [now + timedelta(seconds=i * sample_interval_s)
             for i in range((window_hours * 3600) // sample_interval_s)]

    # Pre-filter pairs by orbital proximity
    def _could_meet(a, b):
        try:
            return (abs(float(a.get("APOAPSIS", 500) or 500) -
                        float(b.get("APOAPSIS", 500) or 500)) < 50 and
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

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed({ex.submit(propagate, idx): idx for idx in needed}):
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
        min_d, tca_idx = float("inf"), None

        for t_idx, (pi, pj) in enumerate(zip(pos_i, pos_j)):
            if pi and pj:
                d = _dist(pi, pj)
                if d < min_d:
                    min_d, tca_idx = d, t_idx

        if min_d > miss_threshold_km or tca_idx is None:
            continue

        obj_a, obj_b = candidates[i], candidates[j]
        rv  = _rel_vel(pos_i[tca_idx], pos_j[tca_idx])
        tca = times[tca_idx]
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