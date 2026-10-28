"""Normalised objective, leeway reject, ranking.

score = w_delay * (delay / delay_base) + w_area * (area / area_base)

Lower is better; the baseline scores exactly 1.000 by construction.
A rejected config scores +inf.
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_WEIGHTS = {"delay": 0.60, "area": 0.40}
DEFAULT_LEEWAY = 0.02  # 2% - a non-target metric may not degrade more


def parse_weights(text: str | None) -> dict[str, float]:
    if not text:
        return dict(DEFAULT_WEIGHTS)
    out = {"delay": 0.0, "area": 0.0}
    for part in text.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        if k in out:
            out[k] = float(v)
    total = out["delay"] + out["area"]
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in out.items()}


def score_row(
    row: dict[str, Any],
    base: dict[str, Any],
    weights: dict[str, float],
    leeway: float = DEFAULT_LEEWAY,
) -> tuple[float, str]:
    """Return (score, reason). reason is '' when accepted."""
    if row.get("status") != "OK":
        return math.inf, row.get("status", "ERROR").lower()

    d, a = row.get("abc_delay_ps"), row.get("chip_area")
    d0, a0 = base.get("abc_delay_ps"), base.get("chip_area")
    if not d or not a or not d0 or not a0:
        return math.inf, "missing metrics"

    # Sanity gate: the design must not have changed, only its implementation.
    sig, sig0 = row.get("signature"), base.get("signature")
    if sig and sig0 and sig.get("ports") is not None and sig0.get("ports") is not None:
        if sig.get("ports") != sig0.get("ports"):
            return math.inf, "port list changed"
    # Flop count from `stat -top` (hierarchy-aggregated), never from counting
    # instances in the netlist text - that undercounts hierarchical netlists.
    s, s0 = row.get("seq_cells"), base.get("seq_cells")
    if s is not None and s0 is not None and s != s0:
        return math.inf, f"flop count changed ({s0} -> {s})"

    dr, ar = d / d0, a / a0

    # Leeway: whichever metric is NOT the optimisation target may not degrade
    # beyond `leeway`. With both weights non-zero, neither is a free dimension,
    # so the rule applies to any metric carrying less than half the weight.
    if weights["delay"] < 0.5 and dr > 1.0 + leeway:
        return math.inf, f"delay degraded {100 * (dr - 1):.1f}% > {100 * leeway:.0f}%"
    if weights["area"] < 0.5 and ar > 1.0 + leeway:
        return math.inf, f"area degraded {100 * (ar - 1):.1f}% > {100 * leeway:.0f}%"

    return weights["delay"] * dr + weights["area"] * ar, ""


def annotate(
    rows: list[dict[str, Any]],
    base: dict[str, Any],
    weights: dict[str, float],
    leeway: float = DEFAULT_LEEWAY,
) -> list[dict[str, Any]]:
    for r in rows:
        s, why = score_row(r, base, weights, leeway)
        r["score"] = None if math.isinf(s) else round(s, 6)
        r["reject_reason"] = why
        if base.get("abc_delay_ps") and r.get("abc_delay_ps"):
            r["delay_ratio"] = round(r["abc_delay_ps"] / base["abc_delay_ps"], 4)
        if base.get("chip_area") and r.get("chip_area"):
            r["area_ratio"] = round(r["chip_area"] / base["chip_area"], 4)
    return rows


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows, key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0)
    )


def best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ok = [r for r in rows if r.get("score") is not None]
    return min(ok, key=lambda r: r["score"]) if ok else None
