"""
AEGIS GNN — K-Fold Cross-Validation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why this exists:
  train.py reports val metrics from a SINGLE random 80/20 split with a
  fixed seed. With ~500 nodes and an imbalanced, soft-labeled target,
  one split can over- or under-state accuracy just from which nodes
  happened to land in val. This script re-trains the same model K times
  on K different folds (same graph/edges each time — only which nodes
  count toward loss/metrics changes) and reports mean ± std for val
  loss, precision, recall, and F1.

  Use this number in the report instead of a single-split number —
  "F1 = 0.71 ± 0.06 over 5 folds" is a defensible claim; "F1 = 0.74"
  from one split with seed=42 is not.

Run: python -m backend.gnn.train_kfold
"""
import os
import sys
import random
import statistics
import torch
import torch.nn.functional as F
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.gnn.model import DebrisGNN, build_graph_from_tle_data
from backend.gnn.train import (
    build_labels_from_cdm, _weighted_bce, _prf1,
    EPOCHS, LR, DEVICE,
)
from backend.services.spacetrack import get_debris_tle_sample_with_cdm_positives, get_cdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

N_FOLDS = 5
SEEDS = [42, 7, 123, 2024, 99]   # one seed per fold's shuffle — NOT model init;
                                  # model itself starts from the same init each
                                  # fold so differences reflect data split, not
                                  # random weight luck


def _train_one_fold(graph, labels, train_idx, val_idx, epochs: int = EPOCHS):
    """
    Trains a fresh DebrisGNN on one fold. Returns best val metrics
    (selected by best val loss, same rule as train.py) so numbers are
    comparable across folds.
    """
    model = DebrisGNN().to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    train_pos = (labels[train_idx] > 0.5).sum().item()
    train_neg = len(train_idx) - train_pos
    pos_weight = (train_neg / train_pos) if train_pos > 0 else 1.0

    best_val_loss = float("inf")
    best_metrics = None

    for epoch in range(epochs):
        model.train()
        optimizer.zero_grad()
        risk_scores = model(graph)
        loss = _weighted_bce(risk_scores[train_idx], labels[train_idx], pos_weight)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        scheduler.step()

        model.eval()
        with torch.no_grad():
            val_scores = model(graph)
            val_loss = F.binary_cross_entropy(val_scores[val_idx], labels[val_idx]).item()
            val_preds = (val_scores[val_idx] > 0.5).float()
            precision, recall, f1 = _prf1(val_preds, labels[val_idx])

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_metrics = {
                "val_loss": val_loss,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }

    return best_metrics


def _mean_std(values: list[float]) -> tuple[float, float]:
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    return mean, std


def run_kfold(n_folds: int = N_FOLDS, epochs: int = EPOCHS):
    logger.info(f"Training on device: {DEVICE}")
    logger.info("Fetching TLE data from Space-Track (unbiased training sample)...")

    debris = get_debris_tle_sample_with_cdm_positives(limit=500)
    cdms   = get_cdm(limit=100)

    if not debris:
        logger.error("No debris data fetched. Check Space-Track credentials.")
        return

    logger.info(f"Building graph from {len(debris)} objects...")
    graph_cpu = build_graph_from_tle_data(debris, kp_index=2.0)
    labels_cpu = build_labels_from_cdm(debris, cdms)
    n = len(labels_cpu)

    logger.info(
        f"Label distribution — high risk (>0.5): "
        f"{(labels_cpu > 0.5).sum().item()} / {n}"
    )

    fold_results = []

    for fold, seed in enumerate(SEEDS[:n_folds], start=1):
        random.seed(seed)
        torch.manual_seed(seed)  # also reseeds model init — see note below

        perm = list(range(n))
        random.shuffle(perm)
        val_size = max(1, int(n * 0.2))
        val_idx = torch.tensor(perm[:val_size], dtype=torch.long).to(DEVICE)
        train_idx = torch.tensor(perm[val_size:], dtype=torch.long).to(DEVICE)

        graph = graph_cpu.clone().to(DEVICE)
        labels = labels_cpu.clone().to(DEVICE)

        logger.info(f"── Fold {fold}/{n_folds} (seed={seed}) "
                    f"— train={len(train_idx)}, val={len(val_idx)} ──")

        metrics = _train_one_fold(graph, labels, train_idx, val_idx, epochs)
        fold_results.append(metrics)

        logger.info(
            f"Fold {fold} best — val_loss={metrics['val_loss']:.4f} "
            f"P={metrics['precision']:.2f} R={metrics['recall']:.2f} "
            f"F1={metrics['f1']:.2f}"
        )

    # ── Aggregate ────────────────────────────────────────────────────────
    val_losses  = [m["val_loss"] for m in fold_results]
    precisions  = [m["precision"] for m in fold_results]
    recalls     = [m["recall"] for m in fold_results]
    f1s         = [m["f1"] for m in fold_results]

    vl_mean, vl_std = _mean_std(val_losses)
    p_mean, p_std   = _mean_std(precisions)
    r_mean, r_std   = _mean_std(recalls)
    f1_mean, f1_std = _mean_std(f1s)

    logger.info("═" * 60)
    logger.info(f"{n_folds}-FOLD RESULTS (mean ± std)")
    logger.info(f"Val Loss:  {vl_mean:.4f} ± {vl_std:.4f}")
    logger.info(f"Precision: {p_mean:.3f} ± {p_std:.3f}")
    logger.info(f"Recall:    {r_mean:.3f} ± {r_std:.3f}")
    logger.info(f"F1:        {f1_mean:.3f} ± {f1_std:.3f}")
    logger.info("═" * 60)
    logger.info(
        "NOTE: same graph/edges used in every fold (transductive setting, "
        "same as train.py) — only the train/val NODE split changes per fold. "
        "A high std here means single-split numbers from train.py are not "
        "reliable on their own; report mean ± std instead."
    )

    return {
        "val_loss": (vl_mean, vl_std),
        "precision": (p_mean, p_std),
        "recall": (r_mean, r_std),
        "f1": (f1_mean, f1_std),
        "folds": fold_results,
    }


if __name__ == "__main__":
    run_kfold()