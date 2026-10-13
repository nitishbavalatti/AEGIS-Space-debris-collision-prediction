"""
AEGIS GNN — is the CDM-vs-negative AUC real signal, or a sampling artefact?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The CDM-confirmed nodes are fetched by NORAD ID (can be payloads, rocket
bodies, Starlink...), while the negatives are random DEBRIS from the lowest
NORAD IDs (mostly old debris). If the two groups simply differ in altitude,
age or object type, a model can separate them without learning anything
about conjunction risk, and an AUC of ~0.97 would be inflated.

Reads gnn/cdm_holdout_nodes.json (written by eval_cdm_holdout.py).
No torch, no Space-Track, runs in a second:
  python -m backend.gnn.check_cdm_population
"""
import json
import sys
import statistics
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else "gnn/cdm_holdout_nodes.json"
DENSE = [(390, 420), (535, 570), (770, 830)]
FEATURES = ["alt", "incl", "ecc", "bstar", "mean_motion", "launch_year"]


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    w = 0.0
    for p in pos:
        for n in neg:
            w += 1.0 if p > n else 0.5 if p == n else 0.0
    return w / (len(pos) * len(neg))


def vals(nodes, key):
    return [n[key] for n in nodes if n.get(key) is not None]


def med(x):
    return statistics.median(x) if x else float("nan")


def in_dense(a):
    return a is not None and any(lo <= a <= hi for lo, hi in DENSE)


def main():
    nodes = json.load(open(PATH))
    cdm = [n for n in nodes if n["label"] == 1.0]
    low = [n for n in nodes if n["label"] <= 0.5]
    print(f"{len(nodes)} nodes | CDM-confirmed: {len(cdm)} | low-risk negatives: {len(low)}\n")

    print("OBJECT TYPE")
    for name, grp in (("CDM", cdm), ("low-risk", low)):
        c = Counter(n["type"] or "?" for n in grp)
        print(f"  {name:9s}", dict(c))

    print("\nMEDIANS (CDM vs low-risk)")
    for k in ("alt", "incl", "launch_year"):
        print(f"  {k:12s} CDM={med(vals(cdm, k)):8.1f}   low-risk={med(vals(low, k)):8.1f}")
    print(f"  in dense band: CDM={sum(in_dense(n['alt']) for n in cdm)/max(1,len(cdm)):.0%}"
          f"   low-risk={sum(in_dense(n['alt']) for n in low)/max(1,len(low)):.0%}")

    print("\nSINGLE-FEATURE AUC (CDM vs low-risk; 0.5 = no separation)")
    rows = []
    for k in FEATURES:
        a = auc(vals(cdm, k), vals(low, k))
        rows.append((max(a, 1 - a), k, a))
    rows.sort(reverse=True)
    for best, k, a in rows:
        print(f"  {k:12s} AUC={a:.3f}  (separation {best:.3f})")
    top = rows[0][0]

    print("\nCDM RECALL BY OBJECT TYPE (held-out, threshold 0.5)")
    types = sorted({n["type"] or "?" for n in cdm})
    for model in ("gnn", "no_edges", "rule"):
        parts = []
        for t in types:
            g = [n for n in cdm if (n["type"] or "?") == t and n.get(model) is not None]
            parts.append(f"{t}: {sum(n[model] > 0.5 for n in g)}/{len(g)}")
        print(f"  {model:9s}", " | ".join(parts))

    deb_cdm = [n for n in cdm if n["type"] == "DEBRIS"]
    deb_low = [n for n in low if n["type"] == "DEBRIS"]
    print(f"\nTYPE-MATCHED AUC (DEBRIS CDM nodes: {len(deb_cdm)} vs DEBRIS low-risk: {len(deb_low)})")
    for model in ("gnn", "no_edges", "rule"):
        p = [n[model] for n in deb_cdm if n.get(model) is not None]
        q = [n[model] for n in deb_low if n.get(model) is not None]
        print(f"  {model:9s} AUC={auc(p, q):.3f}")

    print("\nVERDICT")
    if top >= 0.85:
        print(f"  ⚠ A single raw feature separates the groups at AUC {top:.2f}. The headline AUC "
              "is largely a population difference, not learned conjunction risk. Do NOT quote it as skill.")
    elif top >= 0.75:
        print(f"  ~ One feature separates the groups at AUC {top:.2f}: part of the headline AUC is "
              "population difference. Quote it with that caveat.")
    else:
        print(f"  ✓ No single feature separates the groups strongly (max {top:.2f}).")
    if len(deb_cdm) < 10:
        print(f"  ⚠ Only {len(deb_cdm)} DEBRIS-type CDM nodes: type-matched numbers are too noisy to quote.")


if __name__ == "__main__":
    main()