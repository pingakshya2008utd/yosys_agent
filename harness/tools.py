"""The typed tool trio: INSPECT / OPTIMIZE / AGGLOM.

The point of these (per the ORFS-agent paper) is that the LLM reasons over
compact summaries, never over raw history rows. That keeps the hot prompt
small and the token cost flat as history grows.
"""

from __future__ import annotations

import math
import random
from typing import Any

from . import knobs

try:
    import numpy as np
    from sklearn.ensemble import RandomForestRegressor
    _HAVE_SK = True
except ImportError:  # degrade to pure LHS
    _HAVE_SK = False


# ----------------------------------------------------------------- INSPECT

def inspect_marginals(table: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean score per knob value, so the LLM sees which settings pay."""
    ok = [r for r in table if r.get("score") is not None and r.get("config")]
    out: dict[str, Any] = {}
    if not ok:
        return out
    for dim in knobs.DIMS:
        buckets: dict[str, list[float]] = {}
        for r in ok:
            v = str(r["config"].get(dim))
            buckets.setdefault(v, []).append(r["score"])
        out[dim] = {
            v: {"n": len(s), "mean_score": round(sum(s) / len(s), 4), "best": round(min(s), 4)}
            for v, s in sorted(buckets.items())
        }
    return out


def inspect_correlation(table: list[dict[str, Any]]) -> dict[str, float]:
    """Spearman-ish rank correlation between each knob and the score."""
    ok = [r for r in table if r.get("score") is not None and r.get("config")]
    out: dict[str, float] = {}
    if len(ok) < 4:
        return out
    scores = [r["score"] for r in ok]
    sr = _ranks(scores)
    for i, dim in enumerate(knobs.DIMS):
        xs = [knobs.to_vector(r["config"])[i] for r in ok]
        if len(set(xs)) < 2:
            continue
        xr = _ranks(xs)
        out[dim] = round(_pearson(xr, sr), 3)
    return out


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    for pos, i in enumerate(order):
        r[i] = float(pos)
    return r


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else 0.0


def inspect_failures(table: list[dict[str, Any]]) -> dict[str, Any]:
    """Failure counts by reason, plus failure rate per knob value.

    Per the plan's non-negotiables, failure rate per knob value is itself a
    signal the strategist uses - timeouts teach it the cost boundary.
    """
    bad = [r for r in table if r.get("score") is None]
    reasons: dict[str, int] = {}
    for r in bad:
        key = r.get("reject_reason") or r.get("status", "unknown")
        reasons[str(key)] = reasons.get(str(key), 0) + 1

    per_knob: dict[str, dict[str, str]] = {}
    for dim in knobs.DIMS:
        tot: dict[str, int] = {}
        fail: dict[str, int] = {}
        for r in table:
            if not r.get("config"):
                continue
            v = str(r["config"].get(dim))
            tot[v] = tot.get(v, 0) + 1
            if r.get("score") is None:
                fail[v] = fail.get(v, 0) + 1
        hot = {
            v: f"{fail.get(v, 0)}/{n}"
            for v, n in sorted(tot.items())
            if fail.get(v, 0)
        }
        if hot:
            per_knob[dim] = hot

    return {
        "n_failed": len(bad),
        "n_total": len(table),
        "by_reason": reasons,
        "failure_rate_per_knob_value": per_knob,
        "examples": [
            {
                "label": r.get("label"),
                "status": r.get("status"),
                "reason": r.get("reject_reason"),
                "config": r.get("config"),
                "stderr": (r.get("error_tail") or [])[:2],
            }
            for r in bad[:5]
        ],
    }


def inspect_inert(table: list[dict[str, Any]]) -> list[str]:
    """Knobs that provably did nothing on this design.

    A dimension counts as inert when at least two distinct values were tried
    and every one produced exactly the same score. Surfacing this matters: on
    gcd, `abc_D_scale` across 0.80-1.05 and all five `extra_opt` choices gave
    byte-identical results, so without this the strategist burns most of a
    batch perturbing knobs that cannot change the outcome.
    """
    ok = [r for r in table if r.get("score") is not None and r.get("config")]
    inert = []
    for dim in knobs.DIMS:
        vals: dict[Any, set[float]] = {}
        for r in ok:
            vals.setdefault(r["config"].get(dim), set()).add(round(r["score"], 6))
        if len(vals) < 2:
            continue
        all_scores = set().union(*vals.values())
        if len(all_scores) == 1:
            inert.append(dim)
    return inert


def inspect_summary(table: list[dict[str, Any]], base: dict[str, Any]) -> dict[str, Any]:
    """One compact dict combining the three INSPECT views plus the leaders."""
    ok = sorted(
        (r for r in table if r.get("score") is not None), key=lambda r: r["score"]
    )
    return {
        "n_runs": len(table),
        "n_accepted": len(ok),
        "baseline": {
            "delay_ps": base.get("abc_delay_ps"),
            "area_um2": base.get("chip_area"),
            "score": 1.0,
        },
        "top5": [
            {
                "score": r["score"],
                "delay_ratio": r.get("delay_ratio"),
                "area_ratio": r.get("area_ratio"),
                "config": r.get("config"),
            }
            for r in ok[:5]
        ],
        "marginals": inspect_marginals(table),
        "knob_score_correlation": inspect_correlation(table),
        "inert_knobs_no_observed_effect": inspect_inert(table),
        "failures": inspect_failures(table),
    }


# ---------------------------------------------------------------- OPTIMIZE

def optimize_candidates(
    table: list[dict[str, Any]],
    rng: random.Random,
    n_out: int = 200,
    n_samples: int = 2000,
) -> list[dict[str, Any]]:
    """Surrogate + Expected Improvement over an LHS pool.

    Falls back to pure LHS below 8 observations or without scikit-learn.
    """
    ok = [r for r in table if r.get("score") is not None and r.get("config")]
    pool = knobs.lhs_configs(n_samples, rng)
    # mix in neighbours of the current leaders, for local refinement
    for r in sorted(ok, key=lambda x: x["score"])[:3]:
        pool.extend(knobs.neighbours(r["config"], rng, n=40))

    if len(ok) < 8 or not _HAVE_SK:
        rng.shuffle(pool)
        return [knobs.clamp(c) for c in pool[:n_out]]

    X = np.array([knobs.to_vector(r["config"]) for r in ok])
    y = np.array([r["score"] for r in ok])
    model = RandomForestRegressor(
        n_estimators=200, min_samples_leaf=1, random_state=rng.randint(0, 10**6)
    )
    model.fit(X, y)

    Xp = np.array([knobs.to_vector(knobs.clamp(c)) for c in pool])
    # per-tree spread as the uncertainty estimate
    preds = np.stack([t.predict(Xp) for t in model.estimators_])
    mu, sigma = preds.mean(axis=0), preds.std(axis=0) + 1e-9
    best_y = y.min()
    z = (best_y - mu) / sigma
    from math import erf, sqrt
    cdf = np.array([0.5 * (1 + erf(v / sqrt(2))) for v in z])
    pdf = np.exp(-0.5 * z**2) / sqrt(2 * math.pi)
    ei = (best_y - mu) * cdf + sigma * pdf

    order = np.argsort(-ei)[:n_out]
    return [knobs.clamp(pool[int(i)]) for i in order]


# ------------------------------------------------------------------ AGGLOM

def agglomerate(
    candidates: list[dict[str, Any]],
    k: int,
    seen: set[str],
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Greedy max-min-distance selection so a batch is diverse, not K copies
    of the same point. Candidates arrive already ranked by predicted quality,
    so index order is the quality prior."""
    fresh = []
    for c in candidates:
        key = knobs.config_key(c)
        if key in seen:
            continue
        seen_local = {knobs.config_key(f) for f in fresh}
        if key in seen_local:
            continue
        fresh.append(c)
    if not fresh:
        return []

    chosen = [fresh[0]]
    vecs = {knobs.config_key(c): knobs.to_vector(c) for c in fresh}

    while len(chosen) < k and len(chosen) < len(fresh):
        best_c, best_d = None, -1.0
        for i, c in enumerate(fresh):
            key = knobs.config_key(c)
            if any(knobs.config_key(x) == key for x in chosen):
                continue
            v = vecs[key]
            d = min(
                sum((a - b) ** 2 for a, b in zip(v, knobs.to_vector(x)))
                for x in chosen
            )
            # quality prior: earlier candidates get a mild boost
            d *= 1.0 + 0.5 * (1.0 - i / max(1, len(fresh)))
            if d > best_d:
                best_c, best_d = c, d
        if best_c is None:
            break
        chosen.append(best_c)
    return chosen[:k]


# ------------------------------------------------------- rule retrieval

def retrieve_rules(rules: dict[str, Any], profile: dict[str, Any], cap: int = 5) -> list[dict[str, Any]]:
    """SymRTLO-style retrieval: field match, sort, elbow cut, cap.

    Simple field matching on purpose - embeddings are not worth it here.
    """
    scored = []
    for rule in rules.get("rules", []):
        applies = rule.get("applies_to", {})
        s = 0.0
        for field, want in applies.items():
            have = profile.get(field)
            if have is None:
                continue
            if isinstance(want, list):
                if have in want:
                    s += 1.0
            elif have == want:
                s += 1.0
        if profile.get("goal") and rule.get("goal") == profile["goal"]:
            s += 0.5
        s *= float(rule.get("confidence", 0.5))
        if s > 0:
            scored.append((s, rule))

    scored.sort(key=lambda t: -t[0])
    if not scored:
        return []
    # elbow: cut at the largest consecutive gap
    cut = len(scored)
    if len(scored) > 1:
        gaps = [(scored[i][0] - scored[i + 1][0], i + 1) for i in range(len(scored) - 1)]
        biggest = max(gaps, key=lambda t: t[0])
        if biggest[0] > 0:
            cut = biggest[1]
    return [r for _, r in scored[:min(cut, cap)]]
