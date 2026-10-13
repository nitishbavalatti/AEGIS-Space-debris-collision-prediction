"""
AEGIS — compare our SGP4 closest approach with Space-Track's CDMs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For every public CDM pair we can resolve to TLEs, propagate both objects
with the SAME SGP4 code AEGIS uses, search +-10 min around Space-Track's
TCA at 1 s steps, and compare our closest approach (time + miss distance)
with theirs.

This validates the screener's accuracy using public TLEs. Differences are
expected: Space-Track computes with higher-accuracy states and
covariances, and we use the CURRENT TLEs (which can be days from the
CDM's own epoch). Report the error distribution; don't hide it.

Run (from the project root):
  python -m backend.gnn.compare_to_spacetrack
Output: terminal table + gnn/spacetrack_comparison.json
"""
import os
import sys
import json
import logging
import statistics
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.services.spacetrack import get_cdm, get_tle_by_norad_ids
from backend.data.conjunction_screener import _propagate_object_bulk, _dist, _rel_vel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUT_PATH = "gnn/spacetrack_comparison.json"
HALF_WINDOW_S = 600


def _parse_tca(s: str) -> datetime:
    s = str(s).strip().replace(" ", "T")
    if "." in s:                                   # trim to microseconds
        head, frac = s.split(".", 1)
        s = head + "." + frac[:6]
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _dedupe(cdms: list[dict]) -> list[dict]:
    """cdm_public repeats the same conjunction as the CDM is updated.
    Keep the most recently created record per (pair, TCA hour)."""
    best = {}
    for c in cdms:
        try:
            a, b = sorted([str(c["SAT_1_ID"]), str(c["SAT_2_ID"])])
            key = (a, b, str(c["TCA"])[:13])
        except KeyError:
            continue
        if key not in best or str(c.get("CREATION_DATE", "")) > str(best[key].get("CREATION_DATE", "")):
            best[key] = c
    return list(best.values())


def _closest(tle_a, tle_b, tca):
    times = [tca + timedelta(seconds=s) for s in range(-HALF_WINDOW_S, HALF_WINDOW_S + 1)]
    pa = _propagate_object_bulk(tle_a["TLE_LINE1"], tle_a["TLE_LINE2"], times)
    pb = _propagate_object_bulk(tle_b["TLE_LINE1"], tle_b["TLE_LINE2"], times)
    best = None
    for t, x, y in zip(times, pa, pb):
        if x and y:
            d = _dist(x, y)
            if best is None or d < best[0]:
                best = (d, t, _rel_vel(x, y))
    return best


def _pct(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))] if s else float("nan")


def run(cdm_limit: int = 100):
    raw = get_cdm(limit=cdm_limit)
    cdms = _dedupe(raw)
    ids = {str(c[k]) for c in cdms for k in ("SAT_1_ID", "SAT_2_ID") if c.get(k)}
    tles = {str(t["NORAD_CAT_ID"]): t for t in get_tle_by_norad_ids(sorted(ids))}
    logger.info(f"{len(raw)} CDM records -> {len(cdms)} unique conjunctions; "
                f"{len(tles)}/{len(ids)} objects have a public TLE")

    rows, no_tle, bad = [], 0, 0
    for c in cdms:
        a, b = tles.get(str(c.get("SAT_1_ID"))), tles.get(str(c.get("SAT_2_ID")))
        if not a or not b:
            no_tle += 1
            continue
        try:
            tca = _parse_tca(c["TCA"])
            their_km = float(c["MIN_RNG"]) / 1000.0      # MIN_RNG is in metres
        except (KeyError, ValueError, TypeError):
            bad += 1
            continue
        ours = _closest(a, b, tca)
        if not ours:
            bad += 1
            continue
        rows.append({
            "pair": f'{c.get("SAT_1_NAME", c.get("SAT_1_ID"))} x {c.get("SAT_2_NAME", c.get("SAT_2_ID"))}',
            "their_tca": tca.isoformat(), "their_miss_km": round(their_km, 3),
            "our_tca": ours[1].isoformat(), "our_miss_km": round(ours[0], 3),
            "tca_err_s": round((ours[1] - tca).total_seconds(), 1),
            "miss_err_km": round(ours[0] - their_km, 3),
            "rel_vel_km_s": round(ours[2], 2),
        })

    summary = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "cdm_records": len(raw), "unique_conjunctions": len(cdms),
        "compared": len(rows), "skipped_no_tle": no_tle, "skipped_other": bad,
    }
    if rows:
        te = [abs(r["tca_err_s"]) for r in rows]
        me = [abs(r["miss_err_km"]) for r in rows]
        # search window edge = our minimum sits at +-10 min, i.e. not a real match
        at_edge = sum(1 for r in rows if abs(r["tca_err_s"]) >= HALF_WINDOW_S - 1)
        summary.update({
            "median_abs_tca_err_s": statistics.median(te),
            "p90_abs_tca_err_s": _pct(te, 0.9),
            "median_abs_miss_err_km": statistics.median(me),
            "p90_abs_miss_err_km": _pct(me, 0.9),
            "within_10s_pct": 100 * sum(t <= 10 for t in te) / len(te),
            "miss_within_1km_pct": 100 * sum(m <= 1.0 for m in me) / len(me),
            "no_match_at_window_edge": at_edge,
        })
    json.dump({"summary": summary, "rows": rows}, open(_ensure(OUT_PATH), "w"), indent=2)

    logger.info("═" * 60)
    for k, v in summary.items():
        logger.info(f"{k:28s} {v}")
    logger.info("═" * 60)
    logger.info(f"{'PAIR':44s} {'THEIR km':>9s} {'OUR km':>9s} {'TCA err s':>10s}")
    for r in sorted(rows, key=lambda r: abs(r["tca_err_s"]))[:15]:
        logger.info(f"{r['pair'][:44]:44s} {r['their_miss_km']:9.3f} {r['our_miss_km']:9.3f} {r['tca_err_s']:10.1f}")
    logger.info(f"Saved {OUT_PATH}")
    return summary


def _ensure(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


if __name__ == "__main__":
    run()