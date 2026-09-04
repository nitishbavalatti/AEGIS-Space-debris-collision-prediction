"""
Lock-in buffer for real-time geomagnetic (IMF Bt/Bz) readings.

NOAA's rtsw_mag_1m.json feed doesn't just append new minutes over time —
it can also retroactively change which spacecraft (SOLAR1/ACE/IMAP) is
treated as "active"/authoritative for a MINUTE THAT'S ALREADY IN THE
PAST, on a later fetch. Combined with noaa_swpc.get_geomag_realtime()'s
per-fetch source-priority dedupe (see _SOURCE_PRIORITY there), the value
returned for the SAME historical minute can genuinely differ between two
fetches just ~60s apart (the geomag cache's TTL) — not because the
underlying physics changed, but because NOAA's own feed re-flagged which
spacecraft "wins" that minute.

Served directly, that produces a visibly unstable chart: the ENTIRE
historical curve can subtly reshape itself on every ~60s cache refresh.
That's what shows up as "the graph looks different every time I check" /
"renders incorrectly, then corrects a few seconds later" — each
individual render is internally consistent, successive renders just
disagree with each other about history that already happened.

Fix: once we've observed and validated a value for a given minute
(time_tag), lock it in and never let a later fetch overwrite it —
first-write-wins. New minutes (going forward in time) still get added
normally. This trades "always reflects NOAA's current best guess about
history" for "the chart is stable once rendered" — NOAA's current
after-the-fact guess about a past minute isn't more authoritative than
our own first good read of it, and stability is what actually matters
for a live dashboard someone is looking at continuously.

As a side benefit, this also makes the chart resilient to a transient
NOAA outage: if a fetch fails or returns nothing, snapshot() still
serves whatever was already locked in, instead of the chart blanking to
"NOAA SWPC UNREACHABLE" over what might just be one bad poll.
"""
from datetime import datetime, timezone
import threading

_LOCK = threading.Lock()
_LOCKED: dict[str, dict] = {}  # time_tag -> {"bt_nT": float, "bz_nT": float | None}
_MAX_HOURS = 48
_TRIM_CHECK_THRESHOLD = 20000  # only bother scanning for stale entries once it's worth it


def _parse_ts(ts: str):
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None


def ingest(rows: list[dict]) -> None:
    """
    Merge freshly-fetched, already-validated geomag rows (the output of
    noaa_swpc.get_geomag_realtime) into the locked buffer. Existing
    time_tags are left untouched — only genuinely new ones get added.
    Safe to call on every request: cheap dict lookups, and a no-op for
    any row whose time_tag is already locked (the common case, since the
    upstream NOAA cache itself only changes every ~60s).
    """
    if not rows:
        return
    with _LOCK:
        for row in rows:
            ts = row.get("time_tag")
            if not ts or ts in _LOCKED:
                continue
            _LOCKED[ts] = {"bt_nT": row.get("bt_nT"), "bz_nT": row.get("bz_nT")}

        if len(_LOCKED) > _TRIM_CHECK_THRESHOLD:
            cutoff = datetime.now(timezone.utc).timestamp() - _MAX_HOURS * 3600
            stale = []
            for ts in _LOCKED:
                parsed = _parse_ts(ts)
                if parsed is None or parsed.timestamp() < cutoff:
                    stale.append(ts)
            for ts in stale:
                del _LOCKED[ts]


def snapshot(minutes_back: int) -> list[dict]:
    """
    Ascending-by-time_tag list of {"time_tag", "bt_nT", "bz_nT"} for the
    requested window, read from the LOCKED buffer. This is what
    /api/solar/correlation should build its chart series from — NOT
    noaa_swpc.get_geomag_realtime()'s raw per-fetch output — so the
    chart's history stops reshaping itself across polls.
    """
    with _LOCK:
        items = list(_LOCKED.items())

    cutoff_ts = datetime.now(timezone.utc).timestamp() - minutes_back * 60

    out = []
    for ts, vals in items:
        parsed = _parse_ts(ts)
        if parsed is None or parsed.timestamp() < cutoff_ts:
            continue
        out.append({"time_tag": ts, "bt_nT": vals.get("bt_nT"), "bz_nT": vals.get("bz_nT")})

    out.sort(key=lambda r: r["time_tag"])
    return out


def locked_count() -> int:
    with _LOCK:
        return len(_LOCKED)