"""
AEGIS — log predicted conjunctions now, check them after they happen
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1 (now, with the backend running):
    python -m backend.gnn.verify_prediction snapshot
  Reads the current events from the AEGIS API, looks up Space-Track's own
  CDM for each pair (if one exists) and saves everything to
  gnn/predictions_log.json. Run it again closer to the event for a
  better-lead-time record; the latest snapshot before the event is used.

Step 2 (after the closest-approach time has passed):
    python -m backend.gnn.verify_prediction verify
  For each logged event whose TCA has passed, fetches the CURRENT TLEs
  (their epoch is now near or after the event, so more accurate than the
  ones used for the prediction), recomputes the closest approach at 1 s
  steps, and compares it with what AEGIS predicted (and with Space-Track's
  CDM if there was one). Output: terminal table + gnn/prediction_verification.json

Honest framing: for debris there is no ground-truth observation of the
miss distance. The recomputed value is a BETTER ESTIMATE, not truth, and
shows how much the prediction moved once fresher orbital data arrived.
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API = os.getenv("AEGIS_API", "http://localhost:5000/api")
LOG_PATH = "gnn/predictions_log.json"
OUT_PATH = "gnn/prediction_verification.json"
ISS_ID = "25544"


def _now():
    return datetime.now(timezone.utc)


def _key(id_a, id_b, tca_iso):
    a, b = sorted([str(id_a), str(id_b)])
    return f"{a}|{b}|{str(tca_iso)[:13]}"          # pair + TCA hour


def _load_log():
    return json.load(open(LOG_PATH)) if os.path.exists(LOG_PATH) else []


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=2)


def _find_cdm(id_a, id_b):
    """Space-Track's own latest CDM for this pair, or None."""
    try:
        from backend.services.spacetrack import _client
        from backend.config import Config
        _client._ensure_auth()
        best = None
        for s1, s2 in ((id_a, id_b), (id_b, id_a)):
            url = (f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/cdm_public/"
                   f"SAT_1_ID/{s1}/SAT_2_ID/{s2}/orderby/CREATION_DATE desc/limit/3/format/json")
            r = _client.session.get(url, timeout=20)
            r.raise_for_status()
            for rec in r.json():
                if not isinstance(rec, dict):
                    continue
                if best is None or str(rec.get("CREATION_DATE", "")) > str(best.get("CREATION_DATE", "")):
                    best = rec
        if best:
            raw = best.get("MIN_RNG")
            if raw is None:
                # A record WAS returned for this pair, just without the field we
                # expect. Say so (and show the keys) instead of claiming "no CDM".
                logger.warning(f"CDM record found for {id_a}/{id_b} but it has no MIN_RNG; "
                               f"keys: {sorted(best.keys())}")
                return {"st_tca": best.get("TCA"), "st_miss_km": None,
                        "st_created": best.get("CREATION_DATE"), "note": "record found, no MIN_RNG"}
            return {"st_tca": best.get("TCA"), "st_miss_km": float(raw) / 1000.0,
                    "st_created": best.get("CREATION_DATE")}
    except Exception as e:
        logger.warning(f"CDM lookup failed for {id_a}/{id_b}: {e}")
    return None


def snapshot():
    import requests
    headers = {"X-API-Key": os.environ["AEGIS_API_KEY"]} if os.getenv("AEGIS_API_KEY") else {}
    r = requests.get(f"{API}/conjunction/upcoming",
                     params={"hours": 72, "threshold": 25, "type": "all"},
                     headers=headers, timeout=20)
    r.raise_for_status()
    events = [e for e in r.json().get("conjunctions", []) if not e.get("_is_mock")]
    if not events:
        logger.info("No real events right now; nothing logged.")
        return
    log = _load_log()
    for e in events:
        id_a = str(e.get("norad_id"))
        id_b = str(e.get("norad_id_b") or ISS_ID)       # D x ISS events: second object is the ISS
        cdm = _find_cdm(id_a, id_b)
        entry = {
            "key": _key(id_a, id_b, e["tca"]), "logged_at": _now().isoformat(),
            "type": e.get("_type"), "alert": e.get("alert"),
            "id_a": id_a, "id_b": id_b,
            "name_a": e.get("object_name"), "name_b": e.get("object_name_b") or "ISS",
            "pred_tca": e["tca"], "pred_miss_km": e["miss_distance_km"],
            "pred_rel_vel_km_s": e.get("relative_vel_km_s"),
            "lead_time_h": e.get("tca_hours"),
            "spacetrack_cdm": cdm,
        }
        log.append(entry)
        logger.info(f"logged {entry['name_a']} x {entry['name_b']} | miss {entry['pred_miss_km']} km | "
                    f"TCA {entry['pred_tca']} | Space-Track CDM: "
                    f"{'lookup failed or none found' if not cdm else ('%.3f km' % cdm['st_miss_km'] if cdm['st_miss_km'] is not None else 'record found, no miss distance')}")
    _save(LOG_PATH, log)
    logger.info(f"Saved {LOG_PATH} ({len(log)} entries)")


def verify():
    from backend.services.spacetrack import get_tle_by_norad_ids
    from backend.gnn.compare_to_spacetrack import _closest, _parse_tca

    log = _load_log()
    if not log:
        logger.error("No log yet. Run: python -m backend.gnn.verify_prediction snapshot")
        return
    # keep the latest snapshot per event (closest to the event = best prediction)
    latest = {}
    for e in log:
        if e["key"] not in latest or e["logged_at"] > latest[e["key"]]["logged_at"]:
            latest[e["key"]] = e

    now = _now()
    due, pending = [], []
    for e in latest.values():
        tca = _parse_tca(e["pred_tca"])
        (due if (now - tca).total_seconds() > 300 else pending).append(e)
    for e in pending:
        h = (_parse_tca(e["pred_tca"]) - now).total_seconds() / 3600
        logger.info(f"pending: {e['name_a']} x {e['name_b']} — TCA in {h:.1f} h")
    if not due:
        logger.info("Nothing has passed its closest-approach time yet.")
        return

    ids = sorted({i for e in due for i in (e["id_a"], e["id_b"])})
    tles = {str(t["NORAD_CAT_ID"]): t for t in get_tle_by_norad_ids(ids)}

    rows = []
    for e in due:
        a, b = tles.get(e["id_a"]), tles.get(e["id_b"])
        if not a or not b:
            logger.warning(f"no current TLE for {e['id_a']}/{e['id_b']} (decayed or unavailable)")
            continue
        pred_tca = _parse_tca(e["pred_tca"])
        res = _closest(a, b, pred_tca)
        if not res:
            continue
        epochs = [_parse_tca(t["EPOCH"]) for t in (a, b) if t.get("EPOCH")]
        newest_epoch = min(epochs) if epochs else None          # the OLDER of the two TLEs
        rows.append({
            "pair": f"{e['name_a']} x {e['name_b']}", "ids": [e["id_a"], e["id_b"]],
            "lead_time_h_at_log": e.get("lead_time_h"),
            "pred_miss_km": e["pred_miss_km"], "pred_tca": e["pred_tca"],
            "recomputed_miss_km": round(res[0], 3), "recomputed_tca": res[1].isoformat(),
            "miss_change_km": round(res[0] - e["pred_miss_km"], 3),
            "tca_change_s": round((res[1] - pred_tca).total_seconds(), 1),
            "tle_epoch_older_of_pair": newest_epoch.isoformat() if newest_epoch else None,
            "tle_epoch_after_event": bool(newest_epoch and newest_epoch > pred_tca),
            "spacetrack_cdm": e.get("spacetrack_cdm"),
        })
    _save(OUT_PATH, rows)

    logger.info("═" * 72)
    for r in rows:
        logger.info(r["pair"])
        logger.info(f"  predicted : {r['pred_miss_km']:.3f} km at {r['pred_tca']}")
        logger.info(f"  recomputed: {r['recomputed_miss_km']:.3f} km at {r['recomputed_tca']}  "
                    f"(change {r['miss_change_km']:+.3f} km, {r['tca_change_s']:+.1f} s)")
        logger.info(f"  TLE epoch after the event: {r['tle_epoch_after_event']} "
                    f"(if False, the TLEs have not been refreshed since — rerun later)")
        c = r["spacetrack_cdm"]
        logger.info("  Space-Track CDM: " + (
            f"{c['st_miss_km']:.3f} km at {c['st_tca']}" if c and c.get("st_miss_km") is not None
            else "record found but no miss distance" if c else "none recorded (or lookup failed)"))
    logger.info("═" * 72)
    logger.info(f"Saved {OUT_PATH}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "snapshot":
        snapshot()
    elif mode == "verify":
        verify()
    else:
        print("usage: python -m backend.gnn.verify_prediction [snapshot|verify]")