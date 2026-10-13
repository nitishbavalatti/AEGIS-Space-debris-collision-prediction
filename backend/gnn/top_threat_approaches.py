"""
AEGIS — what is the nearest approach of each TOP-RISK object?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Threat Feed ranks objects by RISK SCORE (orbit-based: where an object
sits, how fast it decays). The Conjunction list shows PAIRS that actually
come within the screening threshold in the next 72 h. They are different
questions, so a high-score object need not appear in the conjunction list.

This script links the two without touching the dashboard: for each of the
top-N objects by score it finds the CLOSEST predicted approach to ANY other
object in the next 72 h (no threshold), so you can see how near the
highest-scoring objects really come to something.

Run (backend must be running):
  python -m backend.gnn.top_threat_approaches [N]
"""
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

import numpy as np
import requests
from sgp4.api import Satrec, jday

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from backend.data.conjunction_screener import _propagate_all, _alt_range_km, _refine, _risk_rank

logging.basicConfig(level=logging.WARNING)
API = os.getenv("AEGIS_API", "http://localhost:5000/api")


def _states(sats, jd, fr):
    err, r, v = _propagate_all(sats, jd, fr)
    with np.errstate(invalid="ignore"):
        bad = (err != 0) | (np.abs(r).max(axis=2) > 1.0e6)
    r[bad] = np.nan
    v[bad] = np.nan
    return r.astype(np.float32), v.astype(np.float32)


def nearest_approaches(objects, top_n=15, window_hours=72, step_s=60, chunk=500,
                       keep=3, start_time=None):
    real = [o for o in objects if o.get("TLE_LINE1") and o.get("TLE_LINE2") and not o.get("_is_mock")]
    objs, sats = [], []
    for o in _risk_rank(real):
        try:
            sats.append(Satrec.twoline2rv(o["TLE_LINE1"], o["TLE_LINE2"]))
            objs.append(o)
        except Exception:
            continue
    n = len(objs)
    if n < 2:
        return []
    top_n = min(top_n, n)

    now = start_time or datetime.now(timezone.utc)
    n_t = int((window_hours * 3600) // step_s)
    jd0, fr0 = jday(now.year, now.month, now.day, now.hour, now.minute,
                    now.second + now.microsecond / 1e6)
    jd = np.full(n_t, jd0)
    fr = fr0 + np.arange(n_t) * step_s / 86400.0
    half = step_s / 2.0

    rt, vt = _states(sats[:top_n], jd, fr)
    ranges = np.array([_alt_range_km(o) for o in objs])
    lo, hi = ranges[:, 0], ranges[:, 1]
    best = [[] for _ in range(top_n)]          # per target: (estimate_km, j, t_idx, tau)

    with np.errstate(all="ignore"):
        for c0 in range(0, n, chunk):
            idx = np.arange(c0, min(n, c0 + chunk))
            rc, vc = _states([sats[k] for k in idx], jd, fr)
            for a in range(top_n):
                ok = (lo[a] - 50 <= hi[idx]) & (lo[idx] - 50 <= hi[a]) & (idx != a)
                if not ok.any():
                    continue
                R0 = rc[ok] - rt[a]
                Vr = vc[ok] - vt[a]
                vv = (Vr * Vr).sum(axis=2)
                rv = (R0 * Vr).sum(axis=2)
                tau = np.clip(-rv / np.maximum(vv, 1e-9), -half, half)
                M = np.sqrt(((R0 + Vr * tau[..., None]) ** 2).sum(axis=2))
                M = np.where(np.isnan(M), np.inf, M)
                tmin = M.argmin(axis=1)
                mmin = M[np.arange(len(tmin)), tmin]
                js = idx[ok]
                for h in np.argsort(mmin)[:keep]:
                    if np.isfinite(mmin[h]):
                        best[a].append((float(mmin[h]), int(js[h]), int(tmin[h]), float(tau[h, tmin[h]])))
                best[a] = sorted(best[a])[:keep]

    rows = []
    for a in range(top_n):
        refined = None
        for _, j, t_idx, tau_s in best[a]:
            tc = now + timedelta(seconds=t_idx * step_s + tau_s)
            ref = _refine(objs[a], objs[j], tc, step_s)
            if ref and (refined is None or ref[0] < refined[0][0]):
                refined = (ref, j)
        if refined:
            (d, tca, rv_), j = refined
            rows.append({"rank": a + 1, "name": objs[a].get("OBJECT_NAME"), "norad": objs[a].get("NORAD_CAT_ID"),
                         "score": objs[a].get("risk_score"), "alt_km": objs[a].get("APOAPSIS"),
                         "other": objs[j].get("OBJECT_NAME"), "other_norad": objs[j].get("NORAD_CAT_ID"),
                         "miss_km": d, "hours": (tca - now).total_seconds() / 3600, "rel_vel": rv_,
                         "n_checked": int(((lo[a] - 50 <= hi) & (lo - 50 <= hi[a])).sum()) - 1})
    return rows


def main():
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    headers = {"X-API-Key": os.environ["AEGIS_API_KEY"]} if os.getenv("AEGIS_API_KEY") else {}
    r = requests.get(f"{API}/debris/list", params={"limit": 5000}, headers=headers, timeout=60)
    r.raise_for_status()
    objs = r.json()["objects"]
    n_real = sum(1 for o in objs if o.get("TLE_LINE1") and not o.get("_is_mock"))
    print(f"{len(objs)} objects from the backend, {n_real} with real TLEs")
    if n_real < 2:
        print("The backend has no real Space-Track data loaded yet (it starts with 500 mock objects "
              "and swaps in the real ones about 30 s after launch).\n"
              "Wait for '[Pipeline] Done. 5000 objects' in the backend log, then run this again.")
        return
    t = time.time()
    rows = nearest_approaches(objs, top_n=top_n)
    print(f"\nNearest predicted approach of the top {top_n} risk-ranked objects to ANY other tracked object, next 72 h"
          f" ({len(objs)} objects checked, {time.time() - t:.0f} s)\n")
    print(f"{'#':>2} {'OBJECT':22s} {'SCORE':>6} {'ALT km':>7} {'NEAREST OBJECT':24s} {'MISS km':>9} {'IN h':>6} {'REL km/s':>8} {'CHECKED':>8}")
    for x in rows:
        print(f"{x['rank']:>2} {str(x['name'])[:22]:22s} {float(x['score'] or 0):>6.1f} {float(x['alt_km'] or 0):>7.0f} "
              f"{str(x['other'])[:24]:24s} {x['miss_km']:>9.1f} {x['hours']:>6.1f} {x['rel_vel']:>8.2f} {x['n_checked']:>8}")
    close = sum(1 for x in rows if x["miss_km"] <= 25)
    print(f"\n{close} of {len(rows)} top-risk objects come within 25 km of something in the next 72 h.")


if __name__ == "__main__":
    main()