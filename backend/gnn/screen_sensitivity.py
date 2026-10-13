"""
AEGIS — is "only a few events" a real answer or a screening artefact?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Re-runs the debris x debris screener on the objects the RUNNING backend
holds, at several miss-distance thresholds and object counts.

How to read it: for a given set of objects, the number of encounters
within d km grows roughly with d^2 (a bigger "target disc"). If the counts
rise about 4x when the threshold doubles, the screener is behaving
consistently and a small count simply reflects how empty orbital space is.
If the counts stay flat or odd, something is wrong.

Run (backend must be running):
  python -m backend.gnn.screen_sensitivity
"""
import os
import sys
import time
import logging

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from backend.data.conjunction_screener import screen_debris_conjunctions

logging.basicConfig(level=logging.WARNING)

API = os.getenv("AEGIS_API", "http://localhost:5000/api")


def main():
    headers = {"X-API-Key": os.environ["AEGIS_API_KEY"]} if os.getenv("AEGIS_API_KEY") else {}
    r = requests.get(f"{API}/debris/list", params={"limit": 5000}, headers=headers, timeout=60)
    r.raise_for_status()
    objs = r.json()["objects"]
    real = [o for o in objs if o.get("TLE_LINE1") and not o.get("_is_mock")]
    print(f"{len(objs)} objects from the backend, {len(real)} with TLEs\n")
    if len(real) < 2:
        print("The backend has no real Space-Track data loaded yet (it starts with 500 mock objects).\n"
              "Wait for '[Pipeline] Done. 5000 objects' in the backend log, then run this again.")
        return

    runs = [(800, 25), (800, 50), (800, 100), (2000, 25)]
    base = None
    print(f"{'objects':>8} {'thr km':>7} {'events':>8} {'x vs 25km':>10} {'>3 km/s':>8} {'seconds':>8}")
    for n_obj, thr in runs:
        t = time.time()
        ev = screen_debris_conjunctions(real, window_hours=72, sample_interval_s=60,
                                        miss_threshold_km=thr, max_objects=n_obj, max_events=10**6)
        dt = time.time() - t
        fast = sum(1 for e in ev if e["relative_vel_km_s"] > 3)
        if (n_obj, thr) == (800, 25):
            base = len(ev)
        ratio = f"{len(ev) / base:.1f}x" if base else "-"
        print(f"{n_obj:>8} {thr:>7} {len(ev):>8} {ratio:>10} {fast:>8} {dt:>8.1f}")
    print("\nExpect the 800-object rows to grow roughly 4x at 50 km and 16x at 100 km (area scaling).")


if __name__ == "__main__":
    main()