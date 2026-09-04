"""
AEGIS GNN — Trace high-risk CDM IDs through resolution
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why this exists:
  The MIN_RNG field-name fix was correct (confirmed: no more "wrong key"
  warning), but CDM-confirmed labels are STILL 0/2059. This means the
  set of {IDs with MIN_RNG < 1000} either:
    (a) is empty across the full 100 CDM records (not just the first 5
        shown earlier), or
    (b) is non-empty, but none of those specific IDs successfully
        resolve via get_tle_by_norad_ids — plausible if the close
        conjunctions involve small/recent debris fragments (e.g. "CZ-6A
        DEB") that have a catalog ID but no current public GP/TLE record

This script isolates exactly which case it is, instead of guessing again.

Run: python -m backend.gnn.trace_high_risk_ids
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.services.spacetrack import get_cdm, get_tle_by_norad_ids

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def trace(cdm_limit: int = 100, threshold_m: float = 1000.0):
    cdms = get_cdm(limit=cdm_limit)
    if not cdms:
        logger.error("No CDM records fetched.")
        return

    logger.info(f"Fetched {len(cdms)} CDM records. Threshold: MIN_RNG < {threshold_m}m\n")

    high_risk_pairs = []  # (sat1_id, sat1_name, sat1_type, sat2_id, sat2_name, sat2_type, min_rng)
    high_risk_ids = set()

    for cdm in cdms:
        try:
            min_rng = float(cdm.get("MIN_RNG", 99999))
        except (ValueError, TypeError):
            continue
        if min_rng < threshold_m:
            s1_id, s1_name, s1_type = cdm.get("SAT_1_ID", ""), cdm.get("SAT_1_NAME", ""), cdm.get("SAT1_OBJECT_TYPE", "")
            s2_id, s2_name, s2_type = cdm.get("SAT_2_ID", ""), cdm.get("SAT_2_NAME", ""), cdm.get("SAT2_OBJECT_TYPE", "")
            high_risk_pairs.append((s1_id, s1_name, s1_type, s2_id, s2_name, s2_type, min_rng))
            high_risk_ids.add(str(s1_id))
            high_risk_ids.add(str(s2_id))

    logger.info(f"CDM records with MIN_RNG < {threshold_m}m: {len(high_risk_pairs)} / {len(cdms)}")
    logger.info(f"Unique high-risk NORAD IDs: {len(high_risk_ids)}\n")

    if not high_risk_pairs:
        logger.warning(
            "⚠ CASE (a): zero CDM records anywhere in this batch have "
            f"MIN_RNG < {threshold_m}m. The 98/40 examples seen earlier were "
            "apparently not representative of the full 100 — most conjunctions "
            "in this feed are further apart than the threshold. Consider "
            "raising the threshold (e.g. 5000m) or using MIN_RNG directly as "
            "a continuous risk signal instead of a hard cutoff."
        )
        return

    logger.info("── High-risk pairs found ──")
    for s1_id, s1_name, s1_type, s2_id, s2_name, s2_type, min_rng in high_risk_pairs:
        logger.info(f"  MIN_RNG={min_rng:.0f}m | {s1_name} ({s1_id}, {s1_type}) "
                    f"<-> {s2_name} ({s2_id}, {s2_type})")
    logger.info("")

    # Now check resolution
    id_list = list(high_risk_ids)
    resolved = get_tle_by_norad_ids(id_list)
    resolved_ids = {str(obj.get("NORAD_CAT_ID", "")) for obj in resolved}
    unresolved_ids = high_risk_ids - resolved_ids

    logger.info("─" * 60)
    logger.info("RESOLUTION CHECK")
    logger.info("─" * 60)
    logger.info(f"High-risk IDs requested:  {len(high_risk_ids)}")
    logger.info(f"Successfully resolved:    {len(resolved_ids)}")
    logger.info(f"Unresolved:               {len(unresolved_ids)}")
    if unresolved_ids:
        logger.info(f"Unresolved ID list: {sorted(unresolved_ids)}")
        # Cross-reference names/types for unresolved ones
        for s1_id, s1_name, s1_type, s2_id, s2_name, s2_type, min_rng in high_risk_pairs:
            if str(s1_id) in unresolved_ids:
                logger.info(f"  Unresolved: {s1_name} ({s1_id}) — type {s1_type}")
            if str(s2_id) in unresolved_ids:
                logger.info(f"  Unresolved: {s2_name} ({s2_id}) — type {s2_type}")

    logger.info("")
    if len(unresolved_ids) == len(high_risk_ids):
        logger.warning(
            "⚠ CASE (b) confirmed: ALL high-risk IDs are unresolvable via the "
            "gp query. This is why CDM-confirmed labels are 0 even after the "
            "MIN_RNG field fix — the genuinely close-approach objects don't "
            "have public GP/TLE data (common for small/recent debris "
            "fragments). Training on synthetic/proxy features for these "
            "specific objects isn't possible without a different data source."
        )
    elif unresolved_ids:
        logger.warning(
            f"⚠ Partial resolution: {len(resolved_ids)} of {len(high_risk_ids)} "
            "high-risk IDs resolved. build_labels_from_cdm should now produce "
            f"some nonzero CDM-confirmed count (~{len(resolved_ids)} or fewer, "
            "depending on how many appear in the actual training sample)."
        )
    else:
        logger.info(
            "✓ All high-risk IDs resolved successfully. If train.py still "
            "shows 0 CDM-confirmed, the bug is elsewhere (check that these "
            "resolved objects are actually being merged into the final "
            "debris list passed to build_labels_from_cdm)."
        )


if __name__ == "__main__":
    trace()