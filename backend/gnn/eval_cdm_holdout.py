"""
AEGIS GNN — CDM hold-out evaluation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why this exists:
  train.py / train_kfold.py report F1 against COMBINED labels, and most
  of those labels are altitude/drag rules the model also sees as input,
  so F1 mostly measures "did it re-learn the rules". The only real
  ground truth is the CDM-confirmed nodes (label == 1.0, ~40 of them).
  A single 80/20 split leaves ~6 of them in validation — far too few to
  quote.

  This script holds every CDM-confirmed node out EXACTLY ONCE:
    - CDM nodes and non-CDM nodes are each split into K folds
    - fold k's validation set = its CDM nodes + its share of the others
    - a fresh model trains for a FIXED number of epochs (no early
      stopping / checkpoint picking on the held-out fold, so there is no
      optimistic selection)
    - predictions for held-out nodes are pooled, so recall on CDM nodes
      is computed over ALL of them (e.g. 31/40), not a handful.

  It reports, for the GNN and a rule-only baseline:
    - overall precision / recall / F1 vs the combined labels
    - pooled recall on CDM-confirmed nodes
    - AUC: CDM nodes vs low-risk nodes, and CDM nodes vs heuristic-high
      nodes (the second shows whether the model can tell a real
      conjunction object from an object that merely LOOKS risky by the
      rules; ~0.5 means it cannot)
  Optional --ablation repeats the GNN with all graph edges removed, to
  show whether message passing helps.

Run: python -m backend.gnn.eval_cdm_holdout [--folds 5] [--epochs 200] [--ablation]
(copy into backend/gnn/ next to train.py)
"""
import os
import sys
import json
import random
import argparse
import logging

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.gnn.model import DebrisGNN, build_graph_from_tle_data
from backend.gnn.train import (
    build_labels_from_cdm, _weighted_bce, _will_cross_dense_band,
    DENSE_BANDS, LR, DEVICE,
)
from backend.services.spacetrack import get_debris_tle_sample_with_cdm_positives, get_cdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RESULTS_PATH = "gnn/cdm_holdout_results.json"
NODES_PATH   = "gnn/cdm_holdout_nodes.json"


# ── Pure-python helpers (no torch: easy to unit test) ────────────────────────

def make_folds(labels: list[float], n_folds: int, seed: int) -> list[list[int]]:
    """Each node lands in exactly one fold; CDM nodes (label == 1.0) are
    spread evenly so every fold holds out roughly the same number."""
    rng = random.Random(seed)
    cdm   = [i for i, l in enumerate(labels) if l == 1.0]
    other = [i for i, l in enumerate(labels) if l != 1.0]
    rng.shuffle(cdm)
    rng.shuffle(other)
    return [cdm[k::n_folds] + other[k::n_folds] for k in range(n_folds)]


def auc(pos: list[float], neg: list[float]) -> float:
    """P(score_pos > score_neg), ties count 0.5."""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def summarize(scores: list[float], labels: list[float]) -> dict:
    pred   = [s > 0.5 for s in scores]
    is_pos = [l > 0.5 for l in labels]
    is_cdm = [l == 1.0 for l in labels]

    tp = sum(1 for p, t in zip(pred, is_pos) if p and t)
    fp = sum(1 for p, t in zip(pred, is_pos) if p and not t)
    fn = sum(1 for p, t in zip(pred, is_pos) if not p and t)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec  = tp / (tp + fn) if tp + fn else 0.0
    f1   = 2 * prec * rec / (prec + rec) if prec + rec else 0.0

    cdm_scores   = [s for s, c in zip(scores, is_cdm) if c]
    cdm_hit      = sum(1 for s in cdm_scores if s > 0.5)
    low_scores   = [s for s, l in zip(scores, labels) if l <= 0.5]
    heur_hi      = [s for s, l in zip(scores, labels) if 0.5 < l < 1.0]

    return {
        "precision": prec, "recall": rec, "f1": f1,
        "cdm_recall_hits": cdm_hit, "cdm_total": len(cdm_scores),
        "cdm_recall": cdm_hit / len(cdm_scores) if cdm_scores else float("nan"),
        "flag_rate_on_low_risk": (
            sum(1 for s in low_scores if s > 0.5) / len(low_scores) if low_scores else float("nan")
        ),
        "auc_cdm_vs_low": auc(cdm_scores, low_scores),
        "auc_cdm_vs_heuristic_high": auc(cdm_scores, heur_hi),
    }


def heuristic_scores(debris: list[dict]) -> list[float]:
    """The rule-only baseline: train.py's trajectory + density components
    WITHOUT the CDM component (it never sees ground truth)."""
    out = []
    for obj in debris:
        try:
            alt   = float(obj.get("APOAPSIS", 500) or 500)
            bstar = abs(float(obj.get("BSTAR", 0) or 0))
        except (ValueError, TypeError):
            alt, bstar = 500.0, 0.0
        traj = _will_cross_dense_band(alt, bstar)
        dens = 0.5 if any(lo <= alt <= hi for lo, hi in DENSE_BANDS) else 0.0
        out.append(max(traj, dens))
    return out


# ── Training ─────────────────────────────────────────────────────────────────

def _train_and_score(graph, labels, train_idx, seed: int, epochs: int) -> torch.Tensor:
    """Fixed-epoch training; returns scores for ALL nodes from the FINAL model."""
    torch.manual_seed(seed)
    model = DebrisGNN().to(DEVICE)
    opt   = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    pos = (labels[train_idx] > 0.5).sum().item()
    neg = len(train_idx) - pos
    pos_weight = (neg / pos) if pos > 0 else 1.0

    for _ in range(epochs):
        model.train()
        opt.zero_grad()
        loss = _weighted_bce(model(graph)[train_idx], labels[train_idx], pos_weight)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()

    model.eval()
    with torch.no_grad():
        return model(graph).cpu()


def cross_validate(graph, labels_cpu, folds, epochs, seed0, tag) -> list[float]:
    n = len(labels_cpu)
    pooled = [float("nan")] * n
    labels = labels_cpu.to(DEVICE)
    for k, val_nodes in enumerate(folds):
        val_set = set(val_nodes)
        train_idx = torch.tensor([i for i in range(n) if i not in val_set],
                                 dtype=torch.long, device=DEVICE)
        n_cdm_val = sum(1 for i in val_nodes if labels_cpu[i].item() == 1.0)
        logger.info(f"[{tag}] fold {k+1}/{len(folds)}: train={len(train_idx)} "
                    f"val={len(val_nodes)} (CDM held out: {n_cdm_val})")
        scores = _train_and_score(graph, labels, train_idx, seed0 + k, epochs)
        for i in val_nodes:
            pooled[i] = float(scores[i])
    assert all(s == s for s in pooled), "some node was never held out"
    return pooled


def _num(obj: dict, key: str):
    try:
        return float(obj.get(key))
    except (TypeError, ValueError):
        return None


def _log_block(title: str, m: dict):
    logger.info("─" * 60)
    logger.info(title)
    logger.info("─" * 60)
    logger.info(f"Overall vs combined labels:  P={m['precision']:.3f}  R={m['recall']:.3f}  F1={m['f1']:.3f}")
    logger.info(f"CDM-confirmed recall:        {m['cdm_recall_hits']}/{m['cdm_total']} = {m['cdm_recall']:.3f}")
    logger.info(f"Flag rate on low-risk nodes: {m['flag_rate_on_low_risk']:.3f}")
    logger.info(f"AUC  CDM vs low-risk:        {m['auc_cdm_vs_low']:.3f}")
    logger.info(f"AUC  CDM vs heuristic-high:  {m['auc_cdm_vs_heuristic_high']:.3f}  (0.5 = cannot tell them apart)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ablation", action="store_true",
                    help="also run the GNN with all edges removed")
    args = ap.parse_args()

    logger.info(f"Device: {DEVICE}")
    debris = get_debris_tle_sample_with_cdm_positives(limit=2000, pool_size=8000)
    cdms   = get_cdm(limit=100)
    if not debris:
        logger.error("No debris data fetched. Check Space-Track credentials.")
        return

    graph_cpu  = build_graph_from_tle_data(debris, kp_index=2.0)
    labels_cpu = build_labels_from_cdm(debris, cdms)
    labels_l   = labels_cpu.tolist()
    n_cdm = sum(1 for l in labels_l if l == 1.0)
    logger.info(f"{len(debris)} nodes | CDM-confirmed: {n_cdm} | "
                f"high-risk (>0.5): {sum(1 for l in labels_l if l > 0.5)}")
    if n_cdm < 2:
        logger.error("Fewer than 2 CDM-confirmed nodes — nothing to hold out.")
        return

    n_folds = min(args.folds, n_cdm)
    folds = make_folds(labels_l, n_folds, args.seed)
    graph = graph_cpu.to(DEVICE)

    results = {"n_nodes": len(debris), "n_cdm": n_cdm, "folds": n_folds, "epochs": args.epochs}

    gnn_scores = cross_validate(graph, labels_cpu, folds, args.epochs, args.seed, "GNN")
    results["gnn"] = summarize(gnn_scores, labels_l)
    _log_block("GNN (graph edges ON)", results["gnn"])

    abl = None
    if args.ablation:
        g2 = graph_cpu.clone()
        g2.edge_index = torch.zeros((2, 0), dtype=torch.long)
        g2 = g2.to(DEVICE)
        abl = cross_validate(g2, labels_cpu, folds, args.epochs, args.seed, "NO-EDGES")
        results["gnn_no_edges"] = summarize(abl, labels_l)
        _log_block("GNN ablation (NO graph edges)", results["gnn_no_edges"])

    rule = heuristic_scores(debris)
    results["rule_baseline"] = summarize(rule, labels_l)
    _log_block("Rule-only baseline (trajectory + density, no CDM)", results["rule_baseline"])

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"Saved {RESULTS_PATH}")

    # Per-node dump (held-out scores + the fields needed to audit the CDM
    # vs negative populations) — read by check_cdm_population.py.
    nodes = [{
        "norad": str(o.get("NORAD_CAT_ID", "")),
        "name": o.get("OBJECT_NAME", ""),
        "type": str(o.get("OBJECT_TYPE", "")).upper(),
        "alt": _num(o, "APOAPSIS"), "incl": _num(o, "INCLINATION"),
        "ecc": _num(o, "ECCENTRICITY"), "bstar": _num(o, "BSTAR"),
        "mean_motion": _num(o, "MEAN_MOTION"), "launch_year": _num(o, "LAUNCH_YEAR"),
        "label": labels_l[i], "gnn": gnn_scores[i],
        "no_edges": abl[i] if abl is not None else None, "rule": rule[i],
    } for i, o in enumerate(debris)]
    with open(NODES_PATH, "w") as f:
        json.dump(nodes, f)
    logger.info(f"Saved {NODES_PATH} ({len(nodes)} nodes)")
    logger.info(
        "READ THIS: the rule baseline's overall F1 is computed against labels built "
        "from the same rules (plus CDM), so it is partly circular — compare the "
        "models on the CDM-confirmed rows (recall and AUC), not on overall F1."
    )


if __name__ == "__main__":
    main()