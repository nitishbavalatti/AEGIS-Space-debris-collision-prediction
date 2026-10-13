"""
NOAA SWPC (Space Weather Prediction Center) service.
"""

import re
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests

from backend.config import Config
from backend.services.cache import cache

logger = logging.getLogger(__name__)

NOAA_KP_URL     = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
NOAA_FLARES_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-7-day.json"
# CMEs come from NASA's DONKI service, NOT NOAA — services.swpc.noaa.gov/json/DONKI/CME.json
# is a 404, so the CME count could never populate.
# Primary: CCMC's own DONKI web service (no API key). Fallback: the api.nasa.gov gateway,
# which needs Config.NASA_API_KEY ("DEMO_KEY" is rate-limited) and was observed redirecting
# to a CCMC notice page instead of returning JSON.
def _cme_urls(days_back: int) -> list[str]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(1, days_back))
    gateway = getattr(Config, "NASA_DONKI_URL", "https://api.nasa.gov/DONKI")
    return [
        f"https://kauai.ccmc.gsfc.nasa.gov/DONKI/WS/get/CME?startDate={start}&endDate={end}",
        f"{gateway}/CME?startDate={start}&endDate={end}&api_key={Config.NASA_API_KEY}",
    ]

# Real-time interplanetary magnetic field (L1 spacecraft), no API key required.
#
# NOAA retired the legacy "/products/solar-wind/mag-1-day.json" endpoint
# (returns 404 as of Aug 2026) as part of the DSCOVR -> SOLAR-1 transition.
# See Service Change Notice 26-21. The replacement lives under /json/rtsw/
# and has a different shape: instead of one row per minute for whichever
# spacecraft was "active", it now returns one row PER SPACECRAFT per
# minute (SOLAR1 / ACE / IMAP), each carrying its own "active" and
# "source" flags. Values are numeric floats now (previously quoted
# strings). Rows come back newest-first, not oldest-first.
NOAA_MAG_URL = "https://services.swpc.noaa.gov/json/rtsw/rtsw_mag_1m.json"

# Trust order when more than one spacecraft is flagged "active" for the
# same minute (rare, but happens during handover). Lower = more trusted.
_SOURCE_PRIORITY = {"SOLAR1": 0, "ACE": 1, "IMAP": 2}

# Physically implausible for L1 solar wind Bt. Even a severe G5 storm
# rarely sustains much above ~100 nT; values above this are far more
# likely a sensor glitch / calibration fault than real conditions, and a
# single such row is enough to blow out the chart's entire y-axis scale
# (all real ~1-10nT data gets squashed flat while the axis stretches to
# fit the outlier).
MAX_PLAUSIBLE_BT_NT = 150.0

# A spacecraft's "active" flag being True does NOT guarantee its data is
# current. Observed in the wild: SOLAR1 returning hundreds of rows all
# flagged active=True but timestamped 20+ hours behind the freshest data
# in the SAME response (from ACE/IMAP). Those rows pass the normal
# time-sanity check (cutoff/future_guard) just fine because they fall
# well inside a 24h correlation window — they're not "bad" in the
# malformed/future sense _parse_time_str catches, they're just STALE
# while being labeled authoritative. If SOLAR1 also wins source priority
# (it does — priority 0), its stale rows silently displace genuinely
# current ACE/IMAP readings for those same minutes, producing a
# discontinuity in the plotted series: a flat/lagging stretch of
# yesterday's values followed by a sudden jump once fresh data starts
# winning again (e.g. once SOLAR1 catches up or its priority stops
# mattering because no other source reports that exact minute).
#
# Fix: before doing the per-minute dedupe, check how fresh each source's
# OWN latest active row is relative to the freshest row seen anywhere in
# this fetch (any source, active or not — that tells us how current the
# feed is capable of being right now). If a source's latest active
# reading is stale by more than this threshold, we don't trust that
# source's "active" flag for this fetch at all and exclude its rows
# entirely, rather than letting it win priority-based dedupe with old data.
STALE_SOURCE_THRESHOLD_MINUTES = 10


def _date_range(days_back: int = 7):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    return start, end


def _parse_time_str(ts: str) -> datetime:
    """
    Sentinel-based parser used everywhere EXCEPT the geomag row loop below.
    Returns datetime.min (a sentinel meaning "unparseable / missing") on
    failure rather than None or raising, because most call sites use this
    purely for SORTING (e.g. `sorted(events, key=_parse_time)`), where a
    sentinel that sorts first is more convenient than having to filter out
    Nones everywhere. This is why it's intentionally lossy about WHY
    something failed to parse — callers that need to distinguish "genuinely
    malformed" from "valid but out of range" (like the geomag loop) use
    _parse_time_str_strict below instead.
    """
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _parse_time_str_strict(ts: str) -> datetime | None:
    """
    Same parsing as _parse_time_str, but returns None on failure instead of
    a sentinel, so callers can tell "this string genuinely doesn't parse"
    apart from "this string parses fine but the resulting datetime happens
    to be old/future/out of range". The geomag ingestion path needs that
    distinction to avoid mislabeling ordinary out-of-window rows as "bad".
    """
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _parse_time(item: dict) -> datetime:
    ts = (
        item.get("time_tag")
        or item.get("peak_time")
        or item.get("begin_time")
        or item.get("startTime")
        or item.get("date")
        or item.get("timestamp")
        or ""
    )
    return _parse_time_str(ts)


def _parse_kp(item: dict) -> float:
    kp = item.get("kp_index") or item.get("kp") or item.get("kpIndex")
    if kp is None:
        return 0.0
    try:
        return float(kp)
    except Exception:
        return 0.0


def _kp_to_level(kp: float) -> str:
    # NOAA G-scale: G1 (minor storm) starts at Kp 5, G2 (moderate) Kp 6,
    # G3 (strong) Kp 7, G4-G5 (severe/extreme) Kp 8-9. The old mapping was
    # shifted down a step and called Kp 3 a "MINOR storm", so the dashboard
    # showed SOLAR STORM DETECTED on ordinary unsettled conditions.
    if kp >= 8.0:
        return "SEVERE"
    if kp >= 7.0:
        return "STRONG"
    if kp >= 6.0:
        return "MODERATE"
    if kp >= 5.0:
        return "MINOR"
    return "NONE"


def _fetch_json(url: str, timeout: int = 20) -> list | None:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        safe_url = re.sub(r"api_key=[^&]+", "api_key=***", url)       # never log the key
        safe_err = re.sub(r"api_key=[^&\s)]+", "api_key=***", str(e))
        print(f"[NOAA] Fetch failed {safe_url}: {safe_err}")
        return None


# ── Shared, single-flight, stale-while-revalidate download cache ──────────────
# Before: every caller (solar summary, correlation chart, GNN Kp lookup, ...) made
# its OWN request — the same geomag/Kp/flare file was downloaded 2-4 times at once
# on a cold start, a failing URL was retried by every caller (each waiting out the
# full timeout), and any cache miss made the dashboard wait for the network.
#   - one download per URL, shared by all callers (single-flight)
#   - fresh within `ttl`           -> returned immediately
#   - older but < max_stale        -> returned immediately, refreshed in background
#   - a failed fetch is remembered for `neg_ttl` s, so a down/slow network costs
#     one timeout per window instead of one per caller
_RAW: dict[str, dict] = {}
_RAW_LOCK = threading.Lock()
_URL_LOCKS: dict[str, threading.Lock] = {}
_REFRESHING: set[str] = set()


def _raw_store(url: str, data) -> None:
    with _RAW_LOCK:
        ent = _RAW.setdefault(url, {"data": None, "t": 0.0, "fail_t": 0.0})
        if data is None:
            ent["fail_t"] = time.time()          # keep any older good data
        else:
            ent["data"], ent["t"], ent["fail_t"] = data, time.time(), 0.0


def _refresh_async(url: str, timeout: int) -> None:
    with _RAW_LOCK:
        if url in _REFRESHING:
            return
        _REFRESHING.add(url)

    def work():
        try:
            _raw_store(url, _fetch_json(url, timeout))
        finally:
            with _RAW_LOCK:
                _REFRESHING.discard(url)

    threading.Thread(target=work, daemon=True).start()


def _fetch_cached(url: str, ttl: float, timeout: int = 20, neg_ttl: float = 45.0):
    """Cached download of a JSON list. Returns the list, or None if there is
    nothing usable (never fetched successfully, or too stale, and the fetch failed)."""
    max_stale = max(3600.0, 4 * ttl)
    with _RAW_LOCK:
        ent = _RAW.get(url)
        lock = _URL_LOCKS.setdefault(url, threading.Lock())
    now = time.time()

    if ent and ent["data"] is not None:
        age = now - ent["t"]
        if age < ttl:
            return ent["data"]
        if age < max_stale:
            if now - ent["fail_t"] >= neg_ttl:            # don't hammer a failing source
                _refresh_async(url, timeout)
            return ent["data"]                            # serve stale, never make the UI wait
    if ent and now - ent["fail_t"] < neg_ttl:
        return None                                       # failed recently: fail fast

    with lock:                                            # single-flight
        with _RAW_LOCK:
            ent = _RAW.get(url)
        now = time.time()
        if ent and ent["data"] is not None and now - ent["t"] < ttl:
            return ent["data"]                            # someone else just fetched it
        if ent and now - ent["fail_t"] < neg_ttl:
            return None
        data = _fetch_json(url, timeout)
        _raw_store(url, data)
        return data


def warm_cache() -> None:
    """Prefetch everything the dashboard asks for on first load, so the Solar
    Weather / Correlation panels don't wait on a cold start. Safe to call from a
    background thread; every call is cached and failure-tolerant."""
    for fn in (lambda: get_kp_history(1), lambda: get_kp_history(2),
               lambda: get_solar_summary(7), lambda: get_geomag_realtime(1440)):
        try:
            fn()
        except Exception as e:
            print(f"[NOAA] warm-up step failed: {e}")


def _filter_by_days(items: list[dict], days_back: int) -> list[dict]:
    start, _ = _date_range(days_back)
    result = []
    for item in items:
        if _parse_time(item) >= start:
            result.append(item)
    return result


def _get_flare_class(f: dict) -> str:
    for key in ("goes_class", "max_class", "current_class", "class_letter", "xray_class", "class", "flare_class"):
        val = f.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip().upper()
    for val in f.values():
        if isinstance(val, str):
            m = re.match(r'^[ABCMX]\d', val.strip(), re.IGNORECASE)
            if m:
                return val.strip().upper()
    return ""


def get_kp_history(days_back: int = 7) -> tuple[list[dict], bool]:
    cache_key = f"noaa:kp:{days_back}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit, True
    data = _fetch_cached(NOAA_KP_URL, ttl=300)
    if data is None:
        return [], False
    recent = _filter_by_days(data, days_back)
    result = sorted(recent, key=_parse_time, reverse=True)
    cache.set(cache_key, result, ttl=1800)
    return result, True


def get_flare_summary(days_back: int = 7) -> dict:
    cache_key = f"noaa:flares:{days_back}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit
    raw = _fetch_cached(NOAA_FLARES_URL, ttl=600)
    if raw is None:
        return {"ok": False, "flares": [], "total": 0, "x_class": 0, "m_class": 0}
    recent = _filter_by_days(raw, days_back)
    x_class = sum(1 for f in recent if _get_flare_class(f).startswith("X"))
    m_class = sum(1 for f in recent if _get_flare_class(f).startswith("M"))
    formatted = sorted(
        [{"classType": _get_flare_class(f) or "—", "peakTime": f.get("peak_time") or f.get("max_time") or f.get("begin_time") or ""} for f in recent],
        key=lambda f: f["peakTime"], reverse=True,
    )
    result = {"ok": True, "flares": formatted, "total": len(recent), "x_class": x_class, "m_class": m_class}
    cache.set(cache_key, result, ttl=1800)
    return result


def get_cme_summary(days_back: int = 7) -> dict:
    cache_key = f"noaa:cme:{days_back}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit
    raw = None
    for url in _cme_urls(days_back):
        raw = _fetch_cached(url, ttl=1800, timeout=15, neg_ttl=300)
        if raw is not None:
            break
    if raw is None:
        return {"ok": False, "cmes": [], "total": 0}
    recent = _filter_by_days(raw, days_back)
    formatted = sorted(
        [{"startTime": c.get("startTime") or c.get("begin_time") or c.get("time_tag") or "",
          "type": c.get("type") or c.get("cmeType") or "—",
          "speed": c.get("speed") or c.get("cmeSpeed") or "—"} for c in recent],
        key=lambda c: c["startTime"], reverse=True,
    )
    result = {"ok": True, "cmes": formatted, "total": len(recent)}
    cache.set(cache_key, result, ttl=1800)
    return result


def get_geomag_realtime(minutes_back: int = 180) -> list[dict]:
    """
    Real-time interplanetary magnetic field from NOAA SWPC (L1 spacecraft:
    SOLAR-1, with ACE/IMAP as backups), no API key required. Source
    refreshes roughly once a minute.

    As of the DSCOVR retirement (Service Change Notice 26-21), the feed
    returns one row PER SPACECRAFT per minute, each with an "active" flag
    indicating which one SWPC forecasters were treating as authoritative
    at that timestamp. We keep only the active rows.

    Validation applied to every row before it's accepted:
      1. Malformed check — the time_tag string genuinely fails to parse
         (missing, garbled, wrong format). This is the ONLY case logged
         as a per-row WARNING, because it's rare and actually indicates a
         data-quality problem worth looking at.
      2. Window check — the time_tag parses fine but falls outside
         [cutoff, now + small skew tolerance] for the requested
         `minutes_back`. This is NORMAL and expected — the upstream feed
         returns many hours of history per spacecraft regardless of what
         window was asked for, so most rows will legitimately fail this
         check on every call, especially for short windows (e.g. the 15
         -minute window /api/solar/summary uses). These are counted and
         reported as a SINGLE summary line, not logged per row, to avoid
         drowning real problems in noise. A row with a bad/future
         time_tag would previously blow out the chart's x-domain
         (min/max based) if it slipped through; this check still exists,
         it's just not spammy anymore.
      3. Value sanity — reject |bt| outside a physically plausible range.
         A single implausible spike blows out the y-axis scale the same
         way, squashing real ~1-10nT data flat near zero. Logged per row
         (rare in practice).
      4. Source freshness — reject an entire source's active rows for
         this fetch if that source's own latest active reading is stale
         (more than STALE_SOURCE_THRESHOLD_MINUTES behind the freshest
         timestamp seen anywhere in the response). An "active" flag does
         not guarantee current data — SOLAR1 has been observed reporting
         active=True on rows many hours old while ACE/IMAP report
         genuinely current ones in the same fetch. Logged once per
         excluded source per fetch (not per row).
      5. Source de-dupe — if more than one (freshness-passing) spacecraft
         is flagged active for the same minute, keep the most-trusted one
         (SOLAR1 > ACE > IMAP) instead of whichever happened to appear
         first in the JSON.

    Values are in nanotesla (nT), NOT microtesla — quiet is ~1-10 nT,
    storm spikes are ~20-50+ nT.

    Returns ascending list of:
      {"time_tag": iso_str, "bt_nT": float, "bz_nT": float | None}
    bt  = total field magnitude
    bz  = north-south component, GSM frame (negative Bz is what actually
          couples energy into Earth's magnetosphere and drives storms)
    """
    cache_key = f"noaa:geomag:{minutes_back}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    rows = _fetch_cached(NOAA_MAG_URL, ttl=55, timeout=15)
    if rows is None:
        return []

    if not rows:
        return []

    if not isinstance(rows, list) or not isinstance(rows[0], dict):
        print(f"[NOAA] geomag unexpected schema: expected list[dict], got {type(rows)}")
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes_back)
    future_guard = now + timedelta(minutes=5)  # small clock-skew tolerance

    # ── Pass 1: find the freshest timestamp anywhere in this fetch ─────────
    # This tells us how current the feed IS capable of being right now,
    # regardless of which source produced it or whether it's flagged
    # active. Used below to detect a source whose "active" rows are
    # stale relative to what's actually available. Uses the strict
    # parser so a malformed time_tag can't silently masquerade as "the
    # freshest timestamp" via the datetime.min sentinel.
    freshest_ts = None
    for row in rows:
        ts = row.get("time_tag")
        parsed = _parse_time_str_strict(ts)
        if parsed is not None and parsed <= future_guard and (freshest_ts is None or parsed > freshest_ts):
            freshest_ts = parsed

    # ── Pass 2: per-source freshness — is each source's own latest ACTIVE
    # row recent enough to be trusted for this fetch? ──────────────────────
    latest_active_by_source: dict[str, datetime] = {}
    if freshest_ts is not None:
        for row in rows:
            if not row.get("active"):
                continue
            source = row.get("source", "")
            parsed = _parse_time_str_strict(row.get("time_tag"))
            if parsed is None or parsed < cutoff or parsed > future_guard:
                continue
            if source not in latest_active_by_source or parsed > latest_active_by_source[source]:
                latest_active_by_source[source] = parsed

    stale_sources: set[str] = set()
    if freshest_ts is not None:
        stale_threshold = timedelta(minutes=STALE_SOURCE_THRESHOLD_MINUTES)
        for source, latest in latest_active_by_source.items():
            if freshest_ts - latest > stale_threshold:
                stale_sources.add(source)
                logger.warning(
                    f"[NOAA] geomag source {source!r} excluded — its latest "
                    f"active row ({latest.isoformat()}) is stale by "
                    f"{(freshest_ts - latest)} vs freshest available data "
                    f"({freshest_ts.isoformat()}); its active flag is not "
                    f"trusted for this fetch"
                )

    # time_tag -> (priority, row_dict). Lower priority wins on collision.
    best_by_ts: dict[str, tuple[int, dict]] = {}

    # Counters for the single end-of-function summary log, instead of a
    # per-row WARNING for every ordinary out-of-window row (see docstring
    # point 2 — this is the fix for the log-spam issue: a 15-minute
    # window checked against a feed that returns many hours of history
    # per spacecraft will *routinely* have hundreds of legitimate,
    # correctly-parsed rows fall outside the window on every single call).
    n_kept = 0
    n_out_of_window = 0
    n_malformed = 0
    n_implausible = 0
    n_stale_source = 0
    n_inactive = 0

    for row in rows:
        try:
            if not row.get("active"):
                n_inactive += 1
                continue

            source = row.get("source", "")
            if source in stale_sources:
                n_stale_source += 1
                continue

            ts = row.get("time_tag")
            parsed = _parse_time_str_strict(ts)

            if parsed is None:
                # Genuinely unparseable/missing — this IS worth a WARNING,
                # it's rare and indicates a real data-quality problem
                # upstream (not just "outside the requested window").
                n_malformed += 1
                logger.warning(
                    f"[NOAA] geomag row rejected — unparseable time_tag "
                    f"{ts!r} (source={source})"
                )
                continue

            if parsed < cutoff or parsed > future_guard:
                # Valid timestamp, just outside the window this particular
                # call asked for. Normal and common — no per-row log.
                n_out_of_window += 1
                continue

            bt = row.get("bt")
            bz = row.get("bz_gsm")
            if bt is None:
                continue

            try:
                bt_f = float(bt)
            except (TypeError, ValueError):
                continue

            if bt_f < 0 or bt_f > MAX_PLAUSIBLE_BT_NT:
                n_implausible += 1
                logger.warning(
                    f"[NOAA] geomag row rejected — implausible bt={bt_f} "
                    f"at {ts} (source={source})"
                )
                continue

            bz_f = None
            if bz is not None:
                try:
                    bz_f = round(float(bz), 2)
                except (TypeError, ValueError):
                    bz_f = None

            priority = _SOURCE_PRIORITY.get(source, 99)

            existing = best_by_ts.get(ts)
            if existing is None or priority < existing[0]:
                best_by_ts[ts] = (priority, {
                    "time_tag": ts,
                    "bt_nT": round(bt_f, 2),
                    "bz_nT": bz_f,
                })
                if existing is None:
                    n_kept += 1
        except (ValueError, TypeError, KeyError):
            continue

    if n_out_of_window or n_malformed or n_stale_source:
        logger.info(
            f"[NOAA] geomag fetch (minutes_back={minutes_back}): "
            f"kept={n_kept} out_of_window={n_out_of_window} "
            f"malformed={n_malformed} implausible={n_implausible} "
            f"stale_source_excluded={n_stale_source} inactive={n_inactive} "
            f"(total rows={len(rows)})"
        )

    out = sorted((v[1] for v in best_by_ts.values()), key=lambda r: r["time_tag"])
    cache.set(cache_key, out, ttl=60)  # refresh roughly as often as NOAA does
    return out


def get_solar_summary(days_back: int = 7) -> dict:
    try:
        kp_events, kp_ok = get_kp_history(days_back)
        max_kp = max((_parse_kp(e) for e in kp_events), default=0.0)
    except Exception as e:
        print(f"[NOAA] Kp error: {e}")
        kp_ok = False
        max_kp = 0.0

    try:
        flare_data = get_flare_summary(days_back)
    except Exception as e:
        print(f"[NOAA] Flare error: {e}")
        flare_data = {"ok": False, "flares": [], "total": 0, "x_class": 0, "m_class": 0}

    try:
        cme_data = get_cme_summary(days_back)
    except Exception as e:
        print(f"[NOAA] CME error: {e}")
        cme_data = {"ok": False, "cmes": [], "total": 0}

    try:
        geomag = get_geomag_realtime(minutes_back=15)
        latest_bt = geomag[-1]["bt_nT"] if geomag else None
        latest_bz = geomag[-1]["bz_nT"] if geomag else None
        geomag_ok = bool(geomag)
    except Exception as e:
        print(f"[NOAA] geomag latest error: {e}")
        latest_bt = latest_bz = None
        geomag_ok = False

    ok = kp_ok and flare_data["ok"]

    return {
        "storm_level":     _kp_to_level(max_kp),
        "max_kp_index":    max_kp,
        "flare_count":     flare_data["total"],
        "x_class_flares":  flare_data["x_class"],
        "m_class_flares":  flare_data["m_class"],
        "cme_count":       cme_data["total"],
        "latest_flares":   flare_data["flares"][:5],
        "latest_cmes":     cme_data["cmes"][:5],
        "geomag_bt_nT":    latest_bt,
        "geomag_bz_nT":    latest_bz,
        "geomag_reachable": geomag_ok,
        "noaa_reachable":  ok,
        "donki_reachable": cme_data["ok"],
        "note":            "Live NOAA SWPC data" if ok else "NOAA SWPC unavailable",
    }


def clear_noaa_cache() -> None:
    for key in ["noaa:kp:7", "noaa:kp:30", "noaa:kp:1",
                "noaa:flares:7", "noaa:flares:30", "noaa:flares:1",
                "noaa:cme:7", "noaa:cme:30", "noaa:cme:1",
                "noaa:geomag:15", "noaa:geomag:60", "noaa:geomag:180",
                "noaa:geomag:1440"]:
        cache.delete(key)
    with _RAW_LOCK:
        _RAW.clear()
    print("[NOAA] Cache cleared")