"""
AEGIS GNN — Inspect raw CDM records
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why this exists:
  diagnose_training_data.py showed 60 CDM-referenced objects are
  successfully present in the training set, but build_labels_from_cdm
  still produces 0 CDM-confirmed (label=1.0) nodes. That function's only
  filter is `miss_dist = float(cdm.get("MISS_DISTANCE", 9999)); if
  miss_dist < 1000`. Zero matches across 100 records means either:
    (a) the field name isn't actually "MISS_DISTANCE" in the real
        response (so .get() falls back to the 9999 default every time),
    (b) the values ARE present but in a unit/scale where none happen to
        be < 1000, or
    (c) float(...) is raising for every record (unlikely to be silent
        for ALL 100, but worth ruling out)
  This prints the raw records so we can see ground truth instead of
  guessing.

Run: python -m backend.gnn.inspect_cdm_fields
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.services.spacetrack import get_cdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def inspect(limit: int = 5):
    cdms = get_cdm(limit=100)
    if not cdms:
        logger.error("No CDM records fetched.")
        return

    logger.info(f"Fetched {len(cdms)} CDM records. Showing first {limit} in full:\n")

    for i, cdm in enumerate(cdms[:limit]):
        logger.info(f"── Record {i} — all keys/values ──")
        for k, v in cdm.items():
            logger.info(f"  {k!r}: {v!r}  (type: {type(v).__name__})")
        logger.info("")

    # Specifically check the field the code relies on
    logger.info("─" * 60)
    logger.info("MISS_DISTANCE check across all records:")
    logger.info("─" * 60)
    present = 0
    values = []
    for cdm in cdms:
        if "MISS_DISTANCE" in cdm:
            present += 1
            try:
                values.append(float(cdm["MISS_DISTANCE"]))
            except (ValueError, TypeError):
                pass

    logger.info(f"Records containing key 'MISS_DISTANCE': {present} / {len(cdms)}")
    if values:
        logger.info(f"Min: {min(values)}  Max: {max(values)}  "
                    f"Mean: {sum(values)/len(values):.3f}")
        logger.info(f"Sample values: {values[:10]}")
    else:
        logger.info("No numeric MISS_DISTANCE values found — check the key name "
                    "against the raw record dump above.")

    # Also list every key seen across all records, in case the field name
    # differs from what the code expects (e.g. MISS_DISTANCE_KM, MISS_DIST, etc.)
    all_keys = set()
    for cdm in cdms:
        all_keys.update(cdm.keys())
    logger.info("")
    logger.info(f"All distinct keys seen across {len(cdms)} records:")
    for k in sorted(all_keys):
        logger.info(f"  {k}")


if __name__ == "__main__":
    inspect()