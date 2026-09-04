"""
AEGIS GNN Training Script
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Training signal = CDMs from Space-Track + 30-day decay forecast.

Label construction (3-component soft label):
  1. CDM component:   object appears in CDM with miss < 1km → 1.0
  2. Decay component: BSTAR-projected altitude will cross a dense
                      shell (ISS ~408km, Starlink ~550km) within 30d
  3. Density component: object currently in a dense altitude band

Run: python -m backend.gnn.train
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os
import sys
import math
import random
import torch
import torch.nn.functional as F
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.gnn.model import (
    DebrisGNN, build_graph_from_tle_data,
    NODE_FEATURE_DIM, _project_altitude,
)
from backend.services.spacetrack import get_debris_tle_sample_with_cdm_positives, get_cdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHECKPOINT_PATH = "gnn/aegis_gnn.pt"
EPOCHS = 200          # was 50 — loss was still dropping steadily at epoch 50
LR = 1e-3
VAL_FRACTION = 0.2    # holdout for measuring real generalization, not train-set memorization
SEED = 42
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Altitude bands that are critically dense — crossing these raises risk
DENSE_BANDS = [
    (390, 420),   # ISS zone
    (535, 570),   # Starlink shell
    (770, 830),   # Most congested LEO band
]


def _will_cross_dense_band(alt_km: float, bstar: float) -> float:
    """
    Returns a 0-1 score: how likely is this object to decay INTO a
    dense altitude band within 30 days?

    High score = currently ABOVE a dense band and decaying toward/through
    it. Objects already resident in a band are NOT scored here — that's
    handled separately (and more mildly, at weight 0.5) by the density
    component in build_labels_from_cdm, so it isn't double-counted.
    (Previously this function also returned 0.6 for any object merely
    sitting inside a dense band, which inflated ~97% of a BSTAR-sorted
    training sample to "high risk" — fixed by removing that branch.)
    """
    if bstar <= 0:
        return 0.0

    alt_30d = _project_altitude(alt_km, bstar, 30) * 2000.0
    decay = alt_km - alt_30d   # how many km it will drop

    if decay <= 0:
        return 0.0

    for band_lo, band_hi in DENSE_BANDS:
        # Object is above the band now and will be inside or below it in 30d
        if alt_km > band_hi and alt_30d <= band_hi:
            penetration = min(band_hi - alt_30d, band_hi - band_lo)
            return min(penetration / (band_hi - band_lo), 1.0) * 0.9

    return 0.0


def build_labels_from_cdm(
    debris_objects: list[dict],
    cdm_records: list[dict],
) -> torch.Tensor:
    """
    3-component soft labels for 30-day collision risk.

    Component 1 — CDM (authoritative, weight 1.0):
      Object in a CDM with miss distance < 1km = label 1.0

    Component 2 — Trajectory crossing (physical, weight 0.9):
      Object will decay through a dense band in 30 days

    Component 3 — Density (contextual, weight 0.5):
      Object is currently in a dense altitude band

    Final label = max(component1, component2, component3)
    This ensures CDM events always dominate, but objects on
    dangerous decay trajectories still get meaningfully high labels.
    """
    high_risk_ids = set()
    for cdm in cdm_records:
        try:
            miss_dist = float(cdm.get("MISS_DISTANCE", 9999))
            if miss_dist < 1000:
                high_risk_ids.add(str(cdm.get("SAT_1_ID", "")))
                high_risk_ids.add(str(cdm.get("SAT_2_ID", "")))
        except (ValueError, TypeError):
            pass

    labels = []
    for obj in debris_objects:
        norad_id = str(obj.get("NORAD_CAT_ID", ""))

        try:
            alt   = float(obj.get("APOAPSIS", 500) or 500)
            bstar = abs(float(obj.get("BSTAR", 0) or 0))
        except (ValueError, TypeError):
            alt, bstar = 500.0, 0.0

        # Component 1: CDM
        if norad_id in high_risk_ids:
            labels.append(1.0)
            continue

        # Component 2: 30-day trajectory crossing a dense band
        traj_score = _will_cross_dense_band(alt, bstar)

        # Component 3: currently in a dense band
        density_score = 0.0
        for band_lo, band_hi in DENSE_BANDS:
            if band_lo <= alt <= band_hi:
                density_score = 0.5
                break

        label = max(traj_score, density_score)
        labels.append(label)

    return torch.tensor(labels, dtype=torch.float)


def _weighted_bce(probs: torch.Tensor, labels: torch.Tensor, pos_weight: float) -> torch.Tensor:
    """
    Manual class-weighted BCE (risk_head already applies sigmoid, so we
    have probabilities, not logits — F.binary_cross_entropy_with_logits
    doesn't apply here). Positive-class errors cost `pos_weight` times
    more than negative-class errors, so the model can't just play it safe
    and rarely predict HIGH on an imbalanced label set.
    """
    eps = 1e-7
    p = probs.clamp(eps, 1 - eps)
    per_sample = -(pos_weight * labels * torch.log(p) + (1 - labels) * torch.log(1 - p))
    return per_sample.mean()


def _prf1(preds: torch.Tensor, labels: torch.Tensor) -> tuple[float, float, float]:
    """
    Precision / recall / F1 at a 0.5 threshold.
    Labels are soft (continuous 0-1 targets, only exactly 1.0 for confirmed
    CDM hits) — so "true positive" means label > 0.5, matching the same
    threshold convention used everywhere else in this script. Comparing
    labels == 1 here would silently miss almost every true high-risk
    object that wasn't a confirmed CDM hit.
    """
    true_pos = (labels > 0.5).float()
    tp = ((preds == 1) & (true_pos == 1)).sum().item()
    fp = ((preds == 1) & (true_pos == 0)).sum().item()
    fn = ((preds == 0) & (true_pos == 1)).sum().item()
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def train():
    random.seed(SEED)
    torch.manual_seed(SEED)

    logger.info(f"Training on device: {DEVICE}")
    logger.info("Fetching TLE data from Space-Track (randomized sample + guaranteed CDM positives)...")

    # Uses get_debris_tle_sample_with_cdm_positives(), NOT the plain
    # get_debris_tle_sample() or the live-dashboard get_debris_tle().
    #
    #   get_debris_tle()                       -> BSTAR desc: only extreme
    #                                              decayers, wrong for training
    #   get_debris_tle_sample()                -> random debris sample, but a
    #                                              random sample essentially
    #                                              never intersects with the
    #                                              small set of CDM-referenced
    #                                              NORAD IDs by chance, so
    #                                              CDM-confirmed positives
    #                                              (Component 1, the only real
    #                                              ground truth) end up at 0
    #   get_debris_tle_sample_with_cdm_positives() -> random sample PLUS TLE
    #                                              records fetched directly
    #                                              for every CDM-referenced
    #                                              NORAD ID, so real
    #                                              CDM-confirmed positives are
    #                                              guaranteed to be present
    debris = get_debris_tle_sample_with_cdm_positives(limit=2000, pool_size=8000)
    cdms   = get_cdm(limit=100)

    if not debris:
        logger.error("No debris data fetched. Check Space-Track credentials.")
        return

    logger.info(f"Building graph from {len(debris)} objects (12 features, 30d window)...")
    graph  = build_graph_from_tle_data(debris, kp_index=2.0)
    labels = build_labels_from_cdm(debris, cdms)

    n = len(labels)
    n_cdm_confirmed = (labels == 1.0).sum().item()
    logger.info(
        f"Label distribution — "
        f"CDM-confirmed (1.0): {n_cdm_confirmed} / {n}, "
        f"high risk (>0.5): {(labels > 0.5).sum().item()} / {n}, "
        f"crossing dense band: {((labels > 0.3) & (labels < 1.0)).sum().item()}"
    )
    if n_cdm_confirmed == 0:
        logger.warning(
            "⚠ Still 0 CDM-confirmed labels after using "
            "get_debris_tle_sample_with_cdm_positives(). Check that CDM "
            "SAT_1_ID/SAT_2_ID values are resolvable via the gp class query "
            "(some CDM-referenced objects may be too recent/unnumbered)."
        )

    # ── Train/val split ──────────────────────────────────────────────────────
    # Graph edges stay intact (GAT still uses full neighborhood context for
    # every node's forward pass) — we only split WHICH nodes' predictions
    # count toward loss/metrics, the standard transductive-split approach.
    # This measures real generalization instead of train-set memorization.
    perm = list(range(n))
    random.shuffle(perm)
    val_size   = max(1, int(n * VAL_FRACTION))
    val_idx    = torch.tensor(perm[:val_size], dtype=torch.long)
    train_idx  = torch.tensor(perm[val_size:], dtype=torch.long)

    train_pos = (labels[train_idx] > 0.5).sum().item()
    train_neg = len(train_idx) - train_pos
    pos_weight = (train_neg / train_pos) if train_pos > 0 else 1.0
    logger.info(
        f"Train/val split: {len(train_idx)}/{len(val_idx)} | "
        f"pos_weight={pos_weight:.2f} (train pos={train_pos}, neg={train_neg})"
    )

    graph  = graph.to(DEVICE)
    labels = labels.to(DEVICE)
    train_idx = train_idx.to(DEVICE)
    val_idx   = val_idx.to(DEVICE)

    model     = DebrisGNN().to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)

    logger.info(f"Starting training: {EPOCHS} epochs")

    best_val_loss = float("inf")
    for epoch in range(EPOCHS):
        model.train()
        optimizer.zero_grad()

        risk_scores = model(graph)
        loss = _weighted_bce(risk_scores[train_idx], labels[train_idx], pos_weight)

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        scheduler.step()

        # ── Validation (no dropout, no grad) ────────────────────────────────
        model.eval()
        with torch.no_grad():
            val_scores = model(graph)
            val_loss = F.binary_cross_entropy(val_scores[val_idx], labels[val_idx]).item()
            val_preds = (val_scores[val_idx] > 0.5).float()
            precision, recall, f1 = _prf1(val_preds, labels[val_idx])

            # Diagnostic: are val positives actually scoring higher than
            # val negatives (real separation, just conservative calibration)
            # or is there no separation at all (genuine failure to generalize)?
            val_pos_mask = labels[val_idx] > 0.5
            val_neg_mask = ~val_pos_mask
            pos_scores = val_scores[val_idx][val_pos_mask]
            neg_scores = val_scores[val_idx][val_neg_mask]

        if (epoch + 1) % 10 == 0:
            train_high = (risk_scores[train_idx] > 0.5).sum().item()
            pos_mean = pos_scores.mean().item() if len(pos_scores) > 0 else float("nan")
            neg_mean = neg_scores.mean().item() if len(neg_scores) > 0 else float("nan")
            logger.info(
                f"Epoch {epoch+1:3d}/{EPOCHS} | "
                f"Train Loss: {loss.item():.4f} | Val Loss: {val_loss:.4f} | "
                f"Val P/R/F1: {precision:.2f}/{recall:.2f}/{f1:.2f} | "
                f"Train HIGH: {train_high}/{len(train_idx)} | "
                f"Val score mean pos={pos_mean:.3f} neg={neg_mean:.3f}"
            )

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            os.makedirs(os.path.dirname(CHECKPOINT_PATH), exist_ok=True)
            torch.save(model.state_dict(), CHECKPOINT_PATH)

    logger.info(f"Training complete. Best val loss: {best_val_loss:.4f}")
    logger.info(f"Model saved to: {CHECKPOINT_PATH}")
    logger.info("NOTE: Re-run after fetching more CDM data for better label quality.")


if __name__ == "__main__":
    train()