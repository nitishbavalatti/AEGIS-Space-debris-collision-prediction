"""
Rolling history buffer for the GNN's aggregate risk score.

Split out into its own module (rather than living inside the solar
routes file) specifically so backend/tasks.py can write a sample here
every time run_debris_pipeline() computes a REAL new current_risk_score,
without tasks.py having to import from the routes layer (routes
importing from tasks, or vice-versa, is the kind of thing that turns
into a circular import once either side grows). Routes only ever READ
from this module; tasks.py is the only writer that matters for data
quality — see the module docstring note below.
"""
from collections import deque
from datetime import datetime, timezone
import threading

_LOCK = threading.Lock()
_HISTORY: deque[tuple[str, float]] = deque(maxlen=20000)  # generous cap; trimmed by age below
_MAX_HOURS = 48  # keep more than the widest UI window (24h) asks for


def record_risk_score(risk_score, min_interval_seconds: float = 5.0) -> None:
    """
    Append a (timestamp, risk_score) sample to the rolling history buffer.

    `min_interval_seconds` de-dupes back-to-back identical-value calls —
    relevant because this now gets called from two different places:
      1. run_debris_pipeline() in tasks.py, right after a REAL GNN
         rescoring pass — the authoritative source, runs every ~6h.
      2. The /api/solar/summary and /api/solar/correlation routes, as a
         fallback so the buffer still gets *some* samples on a fresh
         deploy before the first pipeline run completes, or if polling
         cadence is much faster than the 6h pipeline interval and finer
         granularity is wanted between real recomputes.
    Both call sites pass the same underlying state["current_risk_score"]
    value, so without de-duping, polling from (2) between runs of (1)
    would just repeat the same number — harmless, but wasteful. This
    still lets (1)'s reordering fix in tasks.py record no matter how
    recently (2) last sampled, since a *value change* always records
    regardless of timing.
    """
    if risk_score is None:
        return
    try:
        risk_f = float(risk_score)
    except (TypeError, ValueError):
        return

    now = datetime.now(timezone.utc)
    ts = now.isoformat()

    with _LOCK:
        if _HISTORY:
            last_ts_str, last_val = _HISTORY[-1]
            last_ts = datetime.fromisoformat(last_ts_str)
            same_value = last_val == risk_f
            too_soon = (now - last_ts).total_seconds() < min_interval_seconds
            if same_value and too_soon:
                return

        _HISTORY.append((ts, risk_f))

        cutoff = now.timestamp() - _MAX_HOURS * 3600
        while _HISTORY and datetime.fromisoformat(_HISTORY[0][0]).timestamp() < cutoff:
            _HISTORY.popleft()


def snapshot() -> list[tuple[str, float]]:
    """Ascending-by-timestamp copy of the current buffer contents."""
    with _LOCK:
        return list(_HISTORY)


def nearest_at_or_before(ts: str, history: list[tuple[str, float]] | None = None):
    """
    Most recently recorded risk_score at-or-before `ts`, or None if
    history doesn't reach back that far yet. Pass a pre-fetched
    `history` (from snapshot()) when looking up many timestamps in a
    loop, to avoid re-acquiring the lock and re-copying the deque once
    per lookup.
    """
    if history is None:
        history = snapshot()
    best = None
    for h_ts, val in history:
        if h_ts <= ts:
            best = val
        else:
            break
    return best