"""
AEGIS GNN — Training Data Diagnostics
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Answers: "is 500 objects actually enough?" — not by guessing, but by
measuring the two things that actually determine whether a GNN can
learn from this data:

  1. GRAPH CONNECTIVITY
     A GNN only helps on nodes that have neighbors. If most nodes are
     isolated (no edges), those nodes get zero benefit from message
     passing and the GNN is functionally an MLP for them. More raw
     objects with the same sparse edge rule won't fix a connectivity
     problem — the ALT_THRESH/INCL_THRESH rule needs to be loosened,
     or edges need a different construction, before "more data" helps.

  2. LABEL COVERAGE
     Component 1 (CDM, weight 1.0) is your only ground-truth signal.
     Components 2/3 are heuristic proxies. If CDM count is the real
     bottleneck (e.g. only 100 CDMs → maybe 10-20 unique high-confidence
     positives after dedup), adding more TLE objects doesn't add more
     ground truth — it just adds more heuristic-labeled nodes, which
     doesn't fix the underlying label quality problem.

Run: python -m backend.gnn.diagnose_training_data
(same import path as train.py — copy into backend/gnn/ alongside it)
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.gnn.model import build_graph_from_tle_data
from backend.gnn.train import build_labels_from_cdm, DENSE_BANDS
from backend.services.spacetrack import get_debris_tle_sample_with_cdm_positives, get_cdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def diagnose(sample_size: int = 500, cdm_limit: int = 100):
    logger.info(f"Fetching {sample_size} objects + {cdm_limit} CDMs from Space-Track...")
    debris = get_debris_tle_sample_with_cdm_positives(limit=sample_size, cdm_limit=cdm_limit)
    cdms = get_cdm(limit=cdm_limit)

    if not debris:
        logger.error("No debris data fetched — check Space-Track credentials/connectivity.")
        return

    n = len(debris)
    logger.info(f"Fetched {n} objects, {len(cdms)} CDM records.\n")

    # ── 1. Graph connectivity ──────────────────────────────────────────────
    graph = build_graph_from_tle_data(debris, kp_index=2.0)
    edge_index = graph.edge_index

    num_directed_edges = edge_index.shape[1]
    num_undirected_edges = num_directed_edges // 2  # edges are added both directions

    degree = {}
    src_list = edge_index[0].tolist()
    for s in src_list:
        degree[s] = degree.get(s, 0) + 1

    isolated = n - len(degree)
    degrees = list(degree.values())
    avg_degree_connected = (sum(degrees) / len(degrees)) if degrees else 0.0
    avg_degree_all = (sum(degrees) / n) if n else 0.0
    max_degree = max(degrees) if degrees else 0

    logger.info("─" * 60)
    logger.info("GRAPH CONNECTIVITY")
    logger.info("─" * 60)
    logger.info(f"Nodes:                        {n}")
    logger.info(f"Undirected edges:             {num_undirected_edges}")
    logger.info(f"Isolated nodes (no edges):    {isolated} ({100*isolated/n:.1f}%)")
    logger.info(f"Avg degree (connected nodes): {avg_degree_connected:.1f}")
    logger.info(f"Avg degree (all nodes):       {avg_degree_all:.1f}")
    logger.info(f"Max degree:                   {max_degree}")
    if isolated / n > 0.3:
        logger.warning(
            "⚠ Over 30% of nodes are isolated — these get NO benefit from "
            "the GNN's message passing. Consider loosening ALT_THRESH/"
            "INCL_THRESH in model.py, or check this before adding more data."
        )
    logger.info("")

    # ── 2. Label coverage ────────────────────────────────────────────────
    labels = build_labels_from_cdm(debris, cdms)

    n_cdm_confirmed = (labels == 1.0).sum().item()
    n_high = (labels > 0.5).sum().item()
    n_mid = ((labels > 0.3) & (labels <= 0.5)).sum().item()
    n_low = (labels <= 0.3).sum().item()

    # unique NORAD IDs referenced in CDMs (rough ground-truth coverage)
    cdm_ids = set()
    for cdm in cdms:
        for key in ("SAT_1_ID", "SAT_2_ID"):
            v = cdm.get(key)
            if v:
                cdm_ids.add(str(v))

    logger.info("─" * 60)
    logger.info("LABEL COVERAGE")
    logger.info("─" * 60)
    logger.info(f"Total CDM records fetched:           {len(cdms)}")
    logger.info(f"Unique NORAD IDs referenced in CDMs: {len(cdm_ids)}")
    logger.info(f"Nodes with CDM-confirmed label (1.0):{n_cdm_confirmed}")
    logger.info(f"Nodes with label > 0.5 (high risk):  {n_high} ({100*n_high/n:.1f}%)")
    logger.info(f"Nodes with 0.3 < label <= 0.5:        {n_mid} ({100*n_mid/n:.1f}%)")
    logger.info(f"Nodes with label <= 0.3 (low risk):   {n_low} ({100*n_low/n:.1f}%)")
    logger.info("")

    if n_cdm_confirmed < 10:
        logger.warning(
            f"⚠ Only {n_cdm_confirmed} nodes have ground-truth CDM labels. "
            "Almost all of your positive signal is coming from the heuristic "
            "trajectory/density components, not confirmed conjunctions. "
            "More TLE objects will add more heuristic-labeled nodes, not "
            "more ground truth — the CDM fetch limit/window is the real "
            "bottleneck here, not sample_size."
        )

    if n_high / n < 0.1 or n_high / n > 0.5:
        logger.warning(
            f"⚠ High-risk fraction is {100*n_high/n:.1f}% — if this is far "
            "from a realistic prior, check for label construction bias "
            "(same failure mode as the earlier BSTAR-sorted sampling bug)."
        )

    logger.info("─" * 60)
    logger.info("VERDICT")
    logger.info("─" * 60)
    if isolated / n <= 0.3 and n_cdm_confirmed >= 10:
        logger.info(
            "Graph connectivity and CDM coverage look reasonable at this "
            "sample size. More objects would still likely help generalization "
            "(check k-fold std from train_kfold.py to confirm), but connectivity "
            "isn't the blocker."
        )
    else:
        logger.info(
            "Fix connectivity and/or CDM coverage BEFORE scaling sample_size up — "
            "more objects under the current edge rule / CDM limit will mostly "
            "add more of the same problem, not more signal."
        )


if __name__ == "__main__":
    diagnose()