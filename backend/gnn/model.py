"""
AEGIS GNN Model
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Graph Neural Network for cascade collision risk prediction.

Architecture:
  - Nodes  = debris objects
  - Edges  = proximity relationships (objects within 50km of each other)
  - Input  = orbital parameters + solar weather features + decay forecast
  - Output = per-node collision risk score [0, 1] over 30-day window

Why GNN over physics-only?
  Physics models compute pairwise conjunction probability in isolation.
  GNN propagates risk through the debris NETWORK — if object A will
  hit B, and B's fragments will threaten C, D, E… the GNN learns these
  graph-level cascade patterns. Classic propagators miss this entirely.

30-day window:
  Node features include projected altitude at T+7, T+15, T+30 days
  using atmospheric drag (BSTAR). This lets the GNN reason not just
  about where an object is now, but where it will be — and whether
  its decaying trajectory will intersect with a dense orbital shell.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

try:
    from torch_geometric.nn import GATConv, global_mean_pool
    from torch_geometric.data import Data, Batch
    TORCH_GEOMETRIC_AVAILABLE = True
except ImportError:
    TORCH_GEOMETRIC_AVAILABLE = False


# ─── Node Feature Schema ──────────────────────────────────────────────────────
# Each debris node has 12 features (expanded from 9 for 30-day window):
#
#  0: altitude_km_normalized        (LEO: 200-2000km → 0-1)
#  1: inclination_rad               (0 - π)
#  2: eccentricity                  (0 - 1)
#  3: bstar_drag                    (atmospheric drag coeff, normalized)
#  4: mean_motion                   (revs/day, normalized)
#  5: raan_rad                      (right ascension of ascending node, 0-2π)
#  6: arg_perigee_rad               (argument of perigee, 0-2π)
#  7: solar_kp_proxy                (current Kp index 0-9, normalized)
#  8: debris_age_years              (years since launch, normalized 0-50yr)
#
#  ── 30-day window features (new) ─────────────────────────────────────────
#  9:  projected_alt_7d_normalized  (estimated altitude in 7 days via drag)
# 10:  projected_alt_15d_normalized (estimated altitude in 15 days via drag)
# 11:  projected_alt_30d_normalized (estimated altitude in 30 days via drag)
#
# The 3 projected altitude features let the GNN reason about TRAJECTORY,
# not just current position. An object with high BSTAR decaying from 450km
# toward 400km (ISS altitude) over 30 days gets a very different risk profile
# than a stable object at 450km.
#
NODE_FEATURE_DIM = 12

EDGE_FEATURE_DIM = 3


def _project_altitude(alt_km: float, bstar: float, days: int) -> float:
    """
    Estimate altitude after `days` using simplified atmospheric drag decay.

    Uses the Jacchia-Roberts simplified drag model:
      Δalt ≈ -2π * n * BSTAR * alt * days_in_revs
    where n is mean motion in rev/day.

    This is approximate but sufficient for 30-day risk window features.
    Returns normalized altitude (0-1 for 200-2000km range).
    """
    if alt_km <= 0 or bstar <= 0:
        return min(alt_km / 2000.0, 1.0)

    # Mean motion at this altitude (rev/day)
    mu = 398600.4418  # km³/s²
    r = 6371.0 + alt_km
    n_rad_s = math.sqrt(mu / r ** 3)
    n_rev_day = n_rad_s * 86400 / (2 * math.pi)

    # Simplified decay: Δalt per day ≈ -2π * BSTAR * alt * n
    decay_per_day = 2 * math.pi * bstar * alt_km * n_rev_day
    projected = max(100.0, alt_km - decay_per_day * days)
    return min(projected / 2000.0, 1.0)


class DebrisGNN(nn.Module):
    """
    Graph Attention Network for space debris cascade risk prediction.

    Uses 3 GAT layers with residual connections, then a risk regression head.
    GAT (vs GCN) is preferred because not all neighbors contribute equally —
    a high-mass debris chunk matters more than a tiny screw.

    Input features now include 30-day projected altitudes so the model
    learns to score cascade risk across a time horizon, not just NOW.
    """

    def __init__(
        self,
        node_features: int = NODE_FEATURE_DIM,
        hidden_dim: int = 64,
        num_heads: int = 4,
        dropout: float = 0.3,
    ):
        super().__init__()

        self.dropout = dropout

        if not TORCH_GEOMETRIC_AVAILABLE:
            raise ImportError(
                "torch-geometric not installed. Run: pip install torch-geometric"
            )

        self.input_proj = nn.Linear(node_features, hidden_dim)

        self.gat1 = GATConv(hidden_dim, hidden_dim // num_heads, heads=num_heads, dropout=dropout)
        self.gat2 = GATConv(hidden_dim, hidden_dim // num_heads, heads=num_heads, dropout=dropout)
        self.gat3 = GATConv(hidden_dim, hidden_dim // num_heads, heads=num_heads, dropout=dropout)

        self.norm1 = nn.LayerNorm(hidden_dim)
        self.norm2 = nn.LayerNorm(hidden_dim)
        self.norm3 = nn.LayerNorm(hidden_dim)

        self.risk_head = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1),
            nn.Sigmoid(),
        )

    def forward(self, data):
        x, edge_index = data.x, data.edge_index

        x = F.relu(self.input_proj(x))

        residual = x
        x = self.gat1(x, edge_index)
        x = F.elu(x)
        x = self.norm1(x + residual)
        x = F.dropout(x, p=self.dropout, training=self.training)

        residual = x
        x = self.gat2(x, edge_index)
        x = F.elu(x)
        x = self.norm2(x + residual)
        x = F.dropout(x, p=self.dropout, training=self.training)

        residual = x
        x = self.gat3(x, edge_index)
        x = F.elu(x)
        x = self.norm3(x + residual)

        risk_scores = self.risk_head(x)
        return risk_scores.squeeze(-1)


def build_graph_from_tle_data(debris_objects: list[dict], kp_index: float = 0.0):
    """
    Converts a list of TLE/orbital records into a PyG Data object.
    Now includes 30-day projected altitude features (features 9, 10, 11).
    """
    import numpy as np
    from datetime import datetime

    if not TORCH_GEOMETRIC_AVAILABLE:
        return None

    n = len(debris_objects)
    if n == 0:
        return None

    node_features = []
    positions = []

    current_year = datetime.utcnow().year

    for obj in debris_objects:
        try:
            alt   = float(obj.get("APOAPSIS", 500))
            incl  = float(obj.get("INCLINATION", 45))
            ecc   = float(obj.get("ECCENTRICITY", 0.001))
            bstar = float(obj.get("BSTAR", 1e-4))
            mm    = float(obj.get("MEAN_MOTION", 15.0))
            raan  = float(obj.get("RA_OF_ASC_NODE", 180))
            argp  = float(obj.get("ARG_OF_PERICENTER", 90))
            launch_year = int(obj.get("LAUNCH_YEAR", 2000) or 2000)
            age   = min((current_year - launch_year) / 50.0, 1.0)

            # ── 30-day projected altitudes ─────────────────────────────────
            alt_7d  = _project_altitude(alt, abs(bstar), 7)
            alt_15d = _project_altitude(alt, abs(bstar), 15)
            alt_30d = _project_altitude(alt, abs(bstar), 30)

            features = [
                min(alt / 2000.0, 1.0),             # 0  current alt
                np.radians(incl) / np.pi,            # 1  inclination
                min(ecc, 1.0),                       # 2  eccentricity
                min(abs(bstar) * 1e4, 1.0),          # 3  drag
                min(mm / 17.0, 1.0),                 # 4  mean motion
                np.radians(raan) / (2 * np.pi),      # 5  RAAN
                np.radians(argp) / (2 * np.pi),      # 6  arg of perigee
                min(kp_index / 9.0, 1.0),            # 7  Kp solar proxy
                age,                                  # 8  object age
                alt_7d,                               # 9  projected alt T+7d
                alt_15d,                              # 10 projected alt T+15d
                alt_30d,                              # 11 projected alt T+30d
            ]
            node_features.append(features)
            positions.append((alt, incl, raan))
        except (ValueError, TypeError):
            node_features.append([0.5] * NODE_FEATURE_DIM)
            positions.append((500, 45, 180))

    x = torch.tensor(node_features, dtype=torch.float)

    # ── Build edges ───────────────────────────────────────────────────────
    # Two objects are connected if they share similar altitude AND inclination.
    # Also connect objects whose PROJECTED altitude will intersect within 30d —
    # this adds edges for objects on collision course even if not currently close.
    edge_src, edge_dst = [], []
    ALT_THRESH  = 50.0
    INCL_THRESH = 5.0

    for i in range(n):
        for j in range(i + 1, n):
            alt_now  = abs(positions[i][0] - positions[j][0])
            incl_diff = abs(positions[i][1] - positions[j][1])

            # Current proximity edge
            if alt_now < ALT_THRESH and incl_diff < INCL_THRESH:
                edge_src.extend([i, j])
                edge_dst.extend([j, i])
                continue

            # Future trajectory crossing edge (30-day window)
            # If they're not close now but will be due to differential decay
            alt_i_30 = node_features[i][11] * 2000.0
            alt_j_30 = node_features[j][11] * 2000.0
            if abs(alt_i_30 - alt_j_30) < ALT_THRESH and incl_diff < INCL_THRESH:
                edge_src.extend([i, j])
                edge_dst.extend([j, i])

    if edge_src:
        edge_index = torch.tensor([edge_src, edge_dst], dtype=torch.long)
    else:
        edge_index = torch.zeros((2, 0), dtype=torch.long)

    return Data(x=x, edge_index=edge_index)


def load_model(checkpoint_path: str | None = None) -> "DebrisGNN | None":
    """
    Load the trained checkpoint. Returns None — not an untrained model —
    on ANY failure to load real weights (missing file, dimension
    mismatch, corrupt checkpoint, etc).

    Why this matters: an untrained DebrisGNN is not a harmless "slightly
    worse" fallback. With random weights, the 3 stacked LayerNorm layers
    tend to homogenize node activations regardless of input differences,
    and pushing that through the final Sigmoid saturates most objects
    toward the same score (empirically ~97/100 for this architecture) —
    it looks like confident, differentiated CRITICAL-risk output for
    every object, when it's actually noise. That's strictly worse than
    having no model, because it's indistinguishable from a real high-risk
    signal downstream (dashboard, alerts, current_risk_score aggregate)
    unless someone reads the server logs.

    Callers (see tasks.py: `if gnn_model:`) already treat None as "GNN
    unavailable" and fall back to the heuristic scorer (_score_debris),
    which varies with real altitude/BSTAR/eccentricity — a much safer
    default than a confidently-wrong untrained network.

    If checkpoint_path is None, we also return None (no silent
    untrained-model serving) — a model must be explicitly loaded from a
    real checkpoint to be used.
    """
    if not TORCH_GEOMETRIC_AVAILABLE:
        print("[GNN] torch-geometric not available — GNN disabled, using heuristic fallback")
        return None

    if not checkpoint_path:
        print("[GNN] No checkpoint path provided — GNN disabled, using heuristic fallback")
        return None

    try:
        state = torch.load(checkpoint_path, map_location="cpu")
    except Exception as e:
        print(f"[GNN] Checkpoint load failed ({checkpoint_path}): {e} — "
              f"GNN disabled, using heuristic fallback")
        return None

    # Handle checkpoint trained with an older feature schema (e.g. the
    # pre-30-day-window 9-feature model). A shape mismatch means the
    # saved weights are NOT compatible with the current architecture —
    # loading them would either crash or (via strict=False) leave most
    # of the network untrained anyway. Fail safe instead.
    if "input_proj.weight" in state:
        old_dim = state["input_proj.weight"].shape[1]
        if old_dim != NODE_FEATURE_DIM:
            print(f"[GNN] Checkpoint has {old_dim} input features, model expects "
                  f"{NODE_FEATURE_DIM} — retraining needed. GNN disabled, "
                  f"using heuristic fallback until retrained "
                  f"(run: python -m backend.gnn.train)")
            return None

    model = DebrisGNN()
    try:
        model.load_state_dict(state)
    except Exception as e:
        print(f"[GNN] state_dict load failed: {e} — GNN disabled, using heuristic fallback")
        return None

    model.eval()
    print(f"[GNN] Loaded checkpoint: {checkpoint_path}")
    return model


def predict_risk(
    model: "DebrisGNN",
    debris_objects: list[dict],
    kp_index: float = 0.0,
) -> list[dict]:
    """
    Run GNN inference on debris objects.
    Returns list with 'risk_score' (0-100) and 'risk_window_30d' added.
    """
    if model is None or not debris_objects:
        return debris_objects

    graph = build_graph_from_tle_data(debris_objects, kp_index)
    if graph is None:
        return debris_objects

    with torch.no_grad():
        scores = model(graph)

    scores_list = (scores * 100).tolist()

    for obj, score in zip(debris_objects, scores_list):
        obj["risk_score"]     = round(score, 1)
        obj["risk_window_30d"] = True   # flag: score represents 30d horizon
        obj["risk_level"] = (
            "CRITICAL" if score >= 80 else
            "HIGH"     if score >= 60 else
            "MEDIUM"   if score >= 40 else
            "LOW"
        )

    return debris_objects