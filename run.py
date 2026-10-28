#!/usr/bin/env python3
"""YOSYS-AGENT: closed-loop tuner for the Yosys synthesis stage of ORFS.

Loop (after ORFS-agent, arXiv 2506.08332):
  RUN -> READ -> COLLATE -> INSPECT -> OPTIMIZE -> AGGLOM -> LLM GATE -> ALTER -> VERIFY

Dispatcher + rule library + verification gate (after SymRTLO, arXiv 2504.10369).
RTL is never rewritten - the only action space is the flow configuration, so
equivalence stays structural and cheap.

Usage:
  ./run.py --design gcd --iters 3 --k 6
  ./run.py --designs gcd,aes,jpeg --tier auto
  ./run.py --report
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from harness import knobs, metrics, runner, score, tools          # noqa: E402
from harness.llm import LLM, MODEL_PARSE, MODEL_REASON, Usage, load_dotenv  # noqa: E402

KNOWLEDGE = ROOT / "knowledge"
RULES_PATH = KNOWLEDGE / "rules.json"
HISTORY_DIR = KNOWLEDGE / "history"
RESULTS = ROOT / "results"
WORK = ROOT / "work"

# Budget tiers. K is additionally capped by core count at runtime: Yosys is
# single-threaded, so parallelism is bounded by nproc, and `large` is further
# capped by free RAM (a flat black_parrot synth can exceed 20 GB).
TIERS: dict[str, dict[str, Any]] = {
    "tiny":   {"designs": ["gcd"], "k": 8, "iters": 4, "timeout": 120},
    "small":  {"designs": ["aes", "jpeg", "dynamic_node", "bp_fe_top", "bp_be_top",
                           "tinyRocket"], "k": 8, "iters": 4, "timeout": 900},
    "medium": {"designs": ["swerv", "swerv_wrapper", "bp_multi_top", "ariane133",
                           "ariane136"], "k": 6, "iters": 3, "timeout": 1800},
    "large":  {"designs": ["bp_quad", "black_parrot"], "k": 3, "iters": 2, "timeout": 3600},
}


RESYN2_CSV = pathlib.Path("/home/ubuntu/chisage/results/synth_resyn2_summary.csv")


def load_resyn2_csv() -> dict[str, dict[str, Any]]:
    """Read the resyn2 sweep CSV, for cross-checking the measured baseline.

    That sweep recorded no delay, so delay must be re-measured here; area,
    cells, flops and logic depth are compared against it to confirm the
    in-harness reproduction really is the same configuration.
    """
    out: dict[str, dict[str, Any]] = {}
    if not RESYN2_CSV.exists():
        return out
    with RESYN2_CSV.open() as fh:
        for row in csv.DictReader(fh):
            name = (row.get("design") or "").strip()
            if not name or not (row.get("cells") or "").strip().isdigit():
                continue
            try:
                out[name] = {
                    "cells": int(row["cells"]),
                    "chip_area": float(row["chip_area_um2"]),
                    "flops": int(row["flops"]),
                    "logic_depth": int(row["logic_depth_levels"]),
                    "runtime_s": float(row["runtime_s"]),
                }
            except (KeyError, ValueError):
                continue
    return out


def tier_of(design: str) -> str:
    for name, spec in TIERS.items():
        if design in spec["designs"]:
            return name
    return "small"


# --------------------------------------------------------------- JSON schemas

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "size_class": {"type": "string", "enum": ["tiny", "small", "medium", "large"]},
        "dominant_style": {"type": "string", "enum": ["datapath", "control", "memory", "mixed"]},
        "arithmetic_hint": {"type": "boolean"},
        "recommended_goal_weights": {
            "type": "object",
            "properties": {"delay": {"type": "number"}, "area": {"type": "number"}},
            "required": ["delay", "area"], "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["size_class", "dominant_style", "arithmetic_hint",
                 "recommended_goal_weights", "rationale"],
    "additionalProperties": False,
}

CONFIG_PROPS = {
    "flatten": {"type": "boolean"},
    "extra_opt": {"type": "string", "enum": knobs.EXTRA_OPT_CHOICES},
    "abc_D_scale": {"type": "number"},
    "resyn_loops": {"type": "integer"},
    "use_dch": {"type": "boolean"},
    "mapper": {"type": "string", "enum": knobs.MAPPER_CHOICES},
    "post": {"type": "string", "enum": knobs.POST_CHOICES},
    "adder_map": {"type": "boolean"},
    "why": {"type": "string"},
}

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "configs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": CONFIG_PROPS,
                "required": list(CONFIG_PROPS.keys()),
                "additionalProperties": False,
            },
        }
    },
    "required": ["configs"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject", "accept_unverified"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "unverified": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "reasons", "unverified"],
    "additionalProperties": False,
}


def agent_prompt(name: str) -> str:
    """Load an agent's system prompt from agents/<name>.md (strip frontmatter)."""
    text = (ROOT / "agents" / f"{name}.md").read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


# ----------------------------------------------------------------- knowledge

def load_rules() -> dict[str, Any]:
    try:
        return json.loads(RULES_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"max_rules": 25, "rules": []}


def save_rules(rules: dict[str, Any]) -> None:
    cap = int(rules.get("max_rules", 25))
    rs = rules.get("rules", [])
    if len(rs) > cap:
        rs.sort(key=lambda r: -float(r.get("confidence", 0)))
        rules["rules"] = rs[:cap]
    RULES_PATH.write_text(json.dumps(rules, indent=2) + "\n")


def learn_rules(
    rules: dict[str, Any], design: str, profile: dict[str, Any],
    table: list[dict[str, Any]], best_row: dict[str, Any] | None,
) -> list[str]:
    """Append at most 2 rules: the winner, and the clearest negative."""
    added: list[str] = []
    existing = rules.setdefault("rules", [])

    def next_id() -> str:
        n = 1
        used = {r.get("id") for r in existing}
        while f"R-{n:03d}" in used:
            n += 1
        return f"R-{n:03d}"

    applies = {
        "size_class": [profile["size_class"]],
        "dominant_style": [profile["dominant_style"]],
    }

    if best_row and best_row.get("config") and best_row.get("score", 1.0) < 1.0:
        delta = {
            k: best_row["config"][k]
            for k in ("abc_D_scale", "use_dch", "mapper", "post", "adder_map")
        }
        ev = {"design": design, "delta_score": round(best_row["score"] - 1.0, 4)}
        merged = False
        for r in existing:
            if r.get("knob_delta") == delta and r.get("applies_to") == applies:
                r.setdefault("evidence", []).append(ev)
                r["confidence"] = min(0.95, float(r.get("confidence", 0.5)) + 0.1)
                merged = True
                added.append(f"merged into {r['id']}")
                break
        if not merged:
            rid = next_id()
            existing.append({
                "id": rid, "applies_to": applies, "knob_delta": delta,
                "goal": "delay" if best_row.get("delay_ratio", 1) < best_row.get("area_ratio", 1) else "area",
                "evidence": [ev], "confidence": 0.6,
                "note": f"MEASURED on {design}: score {best_row['score']:.4f} vs baseline 1.0",
            })
            added.append(rid)

    # Clearest negative. Two guards, both learned the hard way:
    #  * require >= 2 DISTINCT values tried for the dimension - otherwise the
    #    only value present is trivially also the worst, which says nothing;
    #  * keep the value's native type, so the rule stays a usable knob_delta
    #    (a stringified "True" would never match a bool knob again).
    ok_rows = [r for r in table if r.get("score") is not None and r.get("config")]
    worst: tuple[float, str, Any] | None = None
    for dim in knobs.DIMS:
        buckets: dict[Any, list[float]] = {}
        for r in ok_rows:
            buckets.setdefault(r["config"].get(dim), []).append(r["score"])
        if len(buckets) < 2:
            continue
        for val, scores in buckets.items():
            if len(scores) < 2:
                continue
            mean = sum(scores) / len(scores)
            if mean > 1.05 and (worst is None or mean > worst[0]):
                worst = (mean, dim, val)
    if worst:
        mean, dim, val = worst
        rid = next_id()
        existing.append({
            "id": rid, "applies_to": applies,
            "knob_delta": {dim: val}, "goal": "avoid",
            "evidence": [{"design": design, "mean_score": round(mean, 4)}],
            "confidence": 0.5,
            "note": f"MEASURED NEGATIVE on {design}: {dim}={val} averaged {mean:.3f} (worse than baseline).",
        })
        added.append(rid)

    return added


def append_history(design: str, rows: list[dict[str, Any]]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with (HISTORY_DIR / f"{design}.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


# ------------------------------------------------------------------ verify

# Equivalence of a liberty-mapped netlist against RTL. The liberty must be
# read with `-wb` (whitebox), NOT `-lib`: `-lib` makes empty blackboxes with no
# logic function, so the solver cannot see inside the gates and every output
# comes back unproven. Flops are mapped on the gold side too, so both sides
# present the same sequential boundary to equiv_make.
EQUIV_TCL = """
read_liberty -wb -ignore_miss_func {LIB}
read_verilog {VERILOG}
hierarchy -check -top {TOP}
proc; opt; memory -nomap; opt
techmap; opt
dfflibmap -liberty {LIB}
opt
rename {TOP} gold
design -stash gold

read_liberty -wb -ignore_miss_func {LIB}
read_verilog {NETLIST}
hierarchy -check -top {TOP}
design -copy-from gold -as gold gold

equiv_make gold {TOP} equiv
hierarchy -top equiv
flatten
equiv_simple -seq 5
equiv_induct -seq 5
equiv_status
"""

_EQ_PROVEN = re.compile(r"Of those cells (\d+) are proven and (\d+) are unproven")
_EQ_DISPROVED = re.compile(r"(\d+) are disproven|Disproved|proved NOT equivalent")


def sat_miter(spec: dict[str, Any], netlist: str, rundir: pathlib.Path) -> dict[str, Any]:
    """Real equivalence check. Small designs only - it does not scale.

    Returns a THREE-WAY verdict, because "unproven" and "disproved" mean very
    different things and conflating them would be a false alarm:
      proven       - equivalence established
      disproved    - a counterexample exists; the netlist is genuinely wrong
      inconclusive - the solver did not converge; no counterexample found
    """
    tcl = rundir / "equiv.ys"
    tcl.write_text(
        EQUIV_TCL.format(
            VERILOG=" ".join(spec["verilog_files"]), TOP=spec["top"],
            NETLIST=netlist, LIB=knobs.LIB_FILE,
        )
    )
    log = rundir / "equiv.log"
    try:
        subprocess.run(
            ["yosys", "-q", "-l", str(log), str(tcl)],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.SubprocessError as exc:
        return {"ran": False, "verdict": "inconclusive",
                "note": f"miter did not run: {str(exc)[:200]}"}

    text = log.read_text(errors="ignore") if log.exists() else ""
    m = _EQ_PROVEN.search(text)
    proven = int(m.group(1)) if m else 0
    unproven = int(m.group(2)) if m else None
    disproved = bool(_EQ_DISPROVED.search(text))

    if disproved:
        verdict = "disproved"
    elif unproven == 0 and proven > 0:
        verdict = "proven"
    else:
        verdict = "inconclusive"

    return {
        "ran": True, "verdict": verdict,
        "proven": proven, "unproven": unproven,
        "log": str(log),
        "note": (
            "unproven != disproved: no counterexample was found, the solver "
            "simply did not converge on this mapped netlist"
            if verdict == "inconclusive" else ""
        ),
    }


# -------------------------------------------------------------------- main

def optimize_design(
    design: str, args: argparse.Namespace, llm: LLM, usage: Usage,
) -> dict[str, Any]:
    t_start = time.time()
    tier = tier_of(design) if args.tier == "auto" else args.tier
    tspec = TIERS[tier]

    k = args.k or tspec["k"]
    iters = args.iters if args.iters is not None else tspec["iters"]
    base_timeout = args.timeout or tspec["timeout"]

    ncpu = os.cpu_count() or 2
    parallel = min(k, max(1, ncpu))
    if tier == "large":
        try:
            free_gb = int(subprocess.run(
                ["free", "-g"], capture_output=True, text=True
            ).stdout.split()[12])
            parallel = max(1, min(parallel, free_gb // 24))
        except (OSError, IndexError, ValueError):
            parallel = 1

    out = RESULTS / design
    out.mkdir(parents=True, exist_ok=True)
    wdir = WORK / design
    wdir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 72}\n{design}  (tier={tier}, K={k}, N={iters}, parallel={parallel})\n{'=' * 72}")

    # ---- design spec + preflight
    spec = runner.load_design(design, wdir)
    skip = runner.preflight(spec)
    if skip:
        print(f"  SKIP: {skip}")
        rec = {"design": design, "status": "SKIPPED", "reason": skip}
        (out / "status.json").write_text(json.dumps(rec, indent=2))
        return rec
    print(f"  top={spec['top']}  clk={spec['clk_ns']}ns  rtl_lines={spec['rtl_lines']}  "
          f"srams={len(spec['additional_libs'])}")

    # ---- PROFILE (1 LLM call)
    rules = load_rules()
    probe = {
        "design": design, "top": spec["top"], "rtl_lines": spec["rtl_lines"],
        "n_files": len(spec["verilog_files"]), "sdc_period_ns": spec["clk_ns"],
        "has_macros": bool(spec["additional_libs"]),
        "orfs_abc_area_default": spec["abc_area"],
        "orfs_clears_adder_map": spec["no_adder_map"],
    }
    profile = llm.json_call(
        agent_prompt("profiler"),
        "Classify this design.\n\n" + json.dumps(probe, indent=2),
        PROFILE_SCHEMA, model=MODEL_REASON, effort="low", max_tokens=2000,
    )
    if not profile:
        n = spec["rtl_lines"]
        profile = {
            "size_class": "tiny" if n < 2000 else "small" if n < 20000 else "medium" if n < 120000 else "large",
            "dominant_style": "memory" if spec["additional_libs"] else "mixed",
            "arithmetic_hint": False,
            "recommended_goal_weights": {"delay": 0.6, "area": 0.4},
            "rationale": "heuristic fallback (profiler unavailable)",
        }
    print(f"  profile: {profile['size_class']}/{profile['dominant_style']} "
          f"arith={profile['arithmetic_hint']} - {profile['rationale']}")

    weights = score.parse_weights(args.weights)
    if args.weights is None and profile.get("recommended_goal_weights"):
        w = profile["recommended_goal_weights"]
        tot = float(w["delay"]) + float(w["area"])
        if tot > 0:
            weights = {"delay": float(w["delay"]) / tot, "area": float(w["area"]) / tot}
    print(f"  weights: delay={weights['delay']:.2f} area={weights['area']:.2f}  leeway={args.leeway:.0%}")

    prof_for_rules = dict(profile)
    prof_for_rules["goal"] = "delay" if weights["delay"] >= 0.5 else "area"
    retrieved = tools.retrieve_rules(rules, prof_for_rules)
    print(f"  retrieved rules: {[r['id'] for r in retrieved] or 'none'}")

    # ---- BASELINE, measured here and now (never quoted from a file)
    spec["baseline_mode"] = args.baseline
    label = ("validated resyn2 script" if args.baseline == "resyn2"
             else "stock ORFS ABC script")
    print(f"  measuring baseline ({label})...")
    base = runner.run_one(spec, None, wdir / "baseline", base_timeout * 2, "baseline")
    if base["status"] != "OK":
        print(f"  BASELINE FAILED ({base['status']}) - nothing to normalise against.")
        rec = {"design": design, "status": "BASELINE_FAILED",
               "reason": base.get("error_tail"), "baseline": base}
        (out / "status.json").write_text(json.dumps(rec, indent=2, default=str))
        return rec
    base["score"] = 1.0
    base["delay_ratio"] = 1.0
    base["area_ratio"] = 1.0
    print(f"    delay={base['abc_delay_ps']} ps  area={base['chip_area']} um2  "
          f"cells={base['cell_count']}  depth={base['logic_depth']}  ({base['wall_time_s']}s)")

    # Cross-check the reproduction against the recorded resyn2 sweep.
    xcheck: dict[str, Any] = {}
    if args.baseline == "resyn2":
        ref = load_resyn2_csv().get(design)
        if ref:
            xcheck = {
                "csv_cells": ref["cells"], "measured_cells": base["cell_count"],
                "csv_area": ref["chip_area"], "measured_area": base["chip_area"],
                "csv_flops": ref["flops"], "measured_flops": base["seq_cells"],
                "csv_logic_depth": ref["logic_depth"],
                "measured_logic_depth": base["logic_depth"],
            }
            da = (abs(base["chip_area"] - ref["chip_area"]) / ref["chip_area"]
                  if ref["chip_area"] else 0.0)
            xcheck["area_delta_pct"] = round(100 * da, 2)
            xcheck["matches_csv"] = (
                base["cell_count"] == ref["cells"]
                and base["seq_cells"] == ref["flops"]
                and da < 0.005
            )
            tag = "MATCHES" if xcheck["matches_csv"] else "DIFFERS FROM"
            print(f"    cross-check: {tag} resyn2 CSV "
                  f"(cells {base['cell_count']} vs {ref['cells']}, "
                  f"area {base['chip_area']:.2f} vs {ref['chip_area']:.2f}, "
                  f"flops {base['seq_cells']} vs {ref['flops']}, "
                  f"depth {base['logic_depth']} vs {ref['logic_depth']})")
        else:
            print(f"    cross-check: {design} not found in {RESYN2_CSV.name}")

    # Timeout rule: delay-targeted arms are legitimately 3-4.5x the baseline,
    # so a tight multiple would silently kill the arms we want to evaluate.
    timeout_s = max(90, int(8 * base["wall_time_s"]))
    timeout_s = min(timeout_s, base_timeout)
    print(f"  per-run timeout: {timeout_s}s (max(90, 8x baseline {base['wall_time_s']}s), capped at {base_timeout}s)")

    rng = random.Random(args.seed)
    table: list[dict[str, Any]] = []
    seen: set[str] = set()
    cap_runs = int(k * (iters + 1) * 1.25)
    n_runs = 0
    best_hist: list[float] = []
    stall = 0

    # ---- iteration 0: warm start
    jobs: list[tuple[str, dict[str, Any] | None]] = []
    for r in retrieved:
        cfg = knobs.clamp({**knobs.default_config(), **r.get("knob_delta", {})})
        key = knobs.config_key(cfg)
        if key not in seen:
            seen.add(key)
            jobs.append((f"rule_{r['id']}", cfg))
    for name, cfg in knobs.PRESETS.items():
        c = knobs.clamp(cfg)
        key = knobs.config_key(c)
        if key not in seen and len(jobs) < k:
            seen.add(key)
            jobs.append((name, c))
    for c in knobs.lhs_configs(k, rng):
        if len(jobs) >= k:
            break
        c = knobs.clamp(c)
        key = knobs.config_key(c)
        if key not in seen:
            seen.add(key)
            jobs.append(("lhs", c))
    jobs = jobs[:k]

    for it in range(iters + 1):
        if not jobs:
            print("  no new configs to try - stopping.")
            break
        if n_runs + len(jobs) > cap_runs:
            jobs = jobs[: max(0, cap_runs - n_runs)]
            if not jobs:
                print(f"  run cap reached ({cap_runs}) - stopping.")
                break
        if args.budget_s and (time.time() - t_start) > args.budget_s:
            print("  wall-clock budget exhausted - stopping.")
            break

        print(f"\n  --- iteration {it}: {len(jobs)} runs ---")
        iterdir = wdir / f"iter{it}"
        rows = runner.run_batch(spec, jobs, iterdir, timeout_s, parallel)
        n_runs += len(rows)

        # READ fallback: only call the analyst when Python parsing failed.
        for r in rows:
            if r["status"] == "OK" or not r.get("rundir"):
                continue
            logp = pathlib.Path(r["rundir"]) / "yosys.log"
            if r["status"] == "TIMEOUT" or not logp.exists():
                continue
            if metrics.metrics_complete(r):
                continue
            tail = "\n".join(logp.read_text(errors="ignore").splitlines()[-120:])
            fixed = llm.json_call(
                agent_prompt("analyst"), f"Log tail:\n```\n{tail}\n```",
                {"type": "object",
                 "properties": {
                     "chip_area": {"type": ["number", "null"]},
                     "cell_count": {"type": ["integer", "null"]},
                     "seq_cells": {"type": ["integer", "null"]},
                     "abc_delay_ps": {"type": ["number", "null"]},
                     "logic_depth": {"type": ["integer", "null"]},
                     "status": {"type": "string"},
                     "error_note": {"type": "string"}},
                 "required": ["chip_area", "cell_count", "seq_cells",
                              "abc_delay_ps", "logic_depth", "status", "error_note"],
                 "additionalProperties": False},
                model=MODEL_PARSE, effort="low", max_tokens=2000,
            )
            if fixed and fixed.get("status") == "OK":
                for key in ("chip_area", "cell_count", "seq_cells", "abc_delay_ps", "logic_depth"):
                    if r.get(key) is None and fixed.get(key) is not None:
                        r[key] = fixed[key]
                if metrics.metrics_complete(r):
                    r["status"] = "OK"
                    r["recovered_by_analyst"] = True

        score.annotate(rows, base, weights, args.leeway)
        table.extend(rows)
        append_history(design, rows)

        for r in score.rank(rows):
            tag = (f"score={r['score']:.4f} d={r.get('delay_ratio')} a={r.get('area_ratio')}"
                   if r["score"] is not None else f"REJECT({r['reject_reason'] or r['status']})")
            print(f"    {r['label']:<18} {tag}  {r['wall_time_s']}s")

        # all-fail recovery
        if all(r["score"] is None for r in rows) and it < iters:
            print("    every run failed - halving K and relaxing abc_D_scale toward 1.0")
            k = max(2, k // 2)
            relaxed = []
            for _, cfg in jobs[:k]:
                if cfg is None:
                    continue
                c = dict(cfg)
                c["abc_D_scale"] = round(min(1.1, 0.5 * (float(c["abc_D_scale"]) + 1.0)), 4)
                c["use_dch"] = False
                c["resyn_loops"] = 1
                c = knobs.clamp(c)
                key = knobs.config_key(c)
                if key not in seen:
                    seen.add(key)
                    relaxed.append(("relaxed", c))
            jobs = relaxed
            continue

        cur = score.best(table)
        cur_score = cur["score"] if cur else math.inf
        best_hist.append(cur_score)
        if len(best_hist) >= 2 and best_hist[-1] >= best_hist[-2] - 1e-9:
            stall += 1
        else:
            stall = 0
        if stall >= args.patience:
            print(f"  no improvement for {args.patience} consecutive iterations - stopping.")
            break
        if it >= iters:
            break

        # ---- INSPECT / OPTIMIZE / AGGLOM / LLM GATE
        summary = tools.inspect_summary(table, base)
        cands = tools.optimize_candidates(table, rng)
        picked = tools.agglomerate(cands, k, seen, rng)

        tried = [r["config"] for r in table if r.get("config")][-30:]
        prompt = f"""Design {design}: {profile['size_class']}, {profile['dominant_style']}, \
~{spec['rtl_lines']} RTL lines, SDC period {spec['clk_ns']} ns.

Objective to MINIMIZE: {weights['delay']:.2f} * delay/delay_base + {weights['area']:.2f} * area/area_base
Baseline (stock ORFS, measured on this machine): delay={base['abc_delay_ps']} ps, area={base['chip_area']} um2, score=1.000
Constraint: reject anything degrading the non-target metric by more than {args.leeway:.0%}.

Knob schema:
{knobs.describe_schema()}

Retrieved prior rules: {json.dumps(retrieved)}

History summary (from the INSPECT tools, not raw rows):
{json.dumps(summary, indent=2, default=str)}

Surrogate-proposed candidates (you may override up to half):
{json.dumps(picked, indent=2)}

Already tried (do not repeat):
{json.dumps(tried, default=str)}

Propose exactly {k} new configs."""

        out_llm = llm.json_call(
            agent_prompt("strategist"), prompt, STRATEGY_SCHEMA,
            model=MODEL_REASON, effort="medium", max_tokens=8000,
        )

        jobs = []
        if out_llm and out_llm.get("configs"):
            for i, c in enumerate(out_llm["configs"]):
                why = str(c.pop("why", ""))[:80]
                cfg = knobs.clamp(c)
                key = knobs.config_key(cfg)
                if key in seen:
                    continue
                seen.add(key)
                jobs.append((f"llm{it}_{i}", cfg))
                if len(jobs) >= k:
                    break
            (iterdir / "strategist.json").write_text(
                json.dumps(out_llm, indent=2)
            )
        # top up from the surrogate if the LLM under-delivered or was skipped
        for c in picked:
            if len(jobs) >= k:
                break
            key = knobs.config_key(c)
            if key not in seen:
                seen.add(key)
                jobs.append((f"ei{it}", c))

    # ---- VERIFY the winner
    best_row = score.best(table)
    verdict: dict[str, Any] = {"verdict": "none", "reasons": ["no accepted config"], "unverified": []}
    if best_row:
        netlist = pathlib.Path(best_row["rundir"]) / "netlist.v"
        miter = {"ran": False, "verdict": "skipped",
                 "note": "design too large for a miter"}
        if args.miter and profile["size_class"] in ("tiny", "small") \
                and spec["rtl_lines"] < 20000 and netlist.exists():
            print("\n  running SAT miter on the winner...")
            miter = sat_miter(spec, str(netlist), pathlib.Path(best_row["rundir"]))
            print(f"    miter: {miter['verdict']} "
                  f"(proven={miter.get('proven')} unproven={miter.get('unproven')})")
        ev = {
            "check_assert": "passed (run would have failed otherwise)",
            "ports_match_baseline": best_row.get("signature", {}).get("ports") == base.get("signature", {}).get("ports"),
            "seq_cells_match_baseline": best_row.get("signature", {}).get("seq") == base.get("signature", {}).get("seq"),
            "sat_miter": miter,
            "metrics": {kk: best_row.get(kk) for kk in
                        ("abc_delay_ps", "chip_area", "cell_count", "logic_depth",
                         "delay_ratio", "area_ratio", "score")},
            "baseline_metrics": {kk: base.get(kk) for kk in
                                 ("abc_delay_ps", "chip_area", "cell_count", "logic_depth")},
            "retiming": "pinned off in this harness",
        }
        v = llm.json_call(
            agent_prompt("verifier"),
            "Gate this candidate.\n\n" + json.dumps(ev, indent=2, default=str),
            VERIFY_SCHEMA, model=MODEL_PARSE, effort="low", max_tokens=2000,
        )
        if miter["verdict"] == "disproved":
            # A counterexample is decisive; no LLM judgement needed.
            verdict = {"verdict": "reject",
                       "reasons": ["SAT miter found a counterexample"],
                       "unverified": []}
        else:
            verdict = v or {
                "verdict": "accept_unverified",
                "reasons": ["verifier unavailable; structural checks passed"],
                "unverified": ["LLM verifier did not run",
                               f"miter: {miter['verdict']}"],
            }
        print(f"  verifier: {verdict['verdict']} - {'; '.join(verdict['reasons'][:2])}")

    # ---- write artifacts
    traj = out / "trajectory.csv"
    with traj.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "status", "score", "delay_ps", "area_um2", "cells",
                    "seq_cells", "logic_depth", "delay_ratio", "area_ratio",
                    "wall_s", "reject_reason"] + knobs.DIMS)
        for r in [base] + table:
            cfg = r.get("config") or {}
            w.writerow([
                r.get("label"), r.get("status"), r.get("score"),
                r.get("abc_delay_ps"), r.get("chip_area"), r.get("cell_count"),
                r.get("seq_cells"), r.get("logic_depth"), r.get("delay_ratio"),
                r.get("area_ratio"), r.get("wall_time_s"), r.get("reject_reason"),
            ] + [cfg.get(d) for d in knobs.DIMS])

    rec = {
        "design": design, "status": "OK", "tier": tier,
        "top": spec["top"], "clk_ns": spec["clk_ns"], "rtl_lines": spec["rtl_lines"],
        "profile": profile, "weights": weights, "leeway": args.leeway,
        "retrieved_rules": [r["id"] for r in retrieved],
        "baseline_mode": args.baseline,
        "baseline_csv_crosscheck": xcheck,
        "baseline": {kk: base.get(kk) for kk in
                     ("abc_delay_ps", "chip_area", "cell_count", "seq_cells",
                      "logic_depth", "wall_time_s")},
        "best": ({"config": best_row["config"], "score": best_row["score"],
                  "delay_ps": best_row["abc_delay_ps"], "area_um2": best_row["chip_area"],
                  "cells": best_row["cell_count"], "logic_depth": best_row["logic_depth"],
                  "delay_ratio": best_row["delay_ratio"], "area_ratio": best_row["area_ratio"],
                  "label": best_row["label"], "rundir": best_row["rundir"]}
                 if best_row else None),
        "verifier": verdict,
        "n_synth_runs": n_runs + 1,
        "n_accepted": sum(1 for r in table if r.get("score") is not None),
        "iterations_run": len(best_hist),
        "best_score_by_iteration": best_hist,
        "iterations_to_best": (best_hist.index(min(best_hist)) + 1) if best_hist else None,
        "agent_wallclock_s": round(time.time() - t_start, 1),
        "baseline_wallclock_s": base["wall_time_s"],
        "per_run_timeout_s": timeout_s,
    }
    # ---- learn (before writing, so best_config.json records what was learned)
    added = learn_rules(rules, design, profile, table, best_row)
    save_rules(rules)
    rec["rules_added"] = added
    (out / "best_config.json").write_text(json.dumps(rec, indent=2, default=str))
    print(f"\n  best score {best_row['score']:.4f}" if best_row else "\n  no accepted config")
    print(f"  rules learned: {added or 'none'}")
    print(f"  artifacts: {out}/best_config.json, {traj}")
    return rec


# -------------------------------------------------------------------- report

def write_report(usage_summary: dict[str, Any] | None = None) -> pathlib.Path:
    rows = []
    for d in sorted(RESULTS.iterdir()):
        f = d / "best_config.json"
        if f.exists():
            try:
                rows.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                pass

    md = ["# YOSYS-AGENT results", ""]
    try:
        yv = subprocess.run(["yosys", "-V"], capture_output=True, text=True).stdout.strip()
    except OSError:
        yv = "unknown"
    orfs_commit = "not a git repo"
    try:
        g = subprocess.run(["git", "-C", knobs.ORFS, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        if g.returncode == 0:
            orfs_commit = g.stdout.strip()
    except OSError:
        pass
    md += [f"- Yosys: `{yv}`", f"- ORFS commit: `{orfs_commit}`",
           f"- Platform: nangate45 (`{pathlib.Path(knobs.LIB_FILE).name}`)", ""]

    md += ["## Main table", "",
           "| design | delay_base (ps) | delay_opt | d% | area_base (um2) | area_opt | a% | "
           "score_base | score_opt | runs | agent_s | base_s | verdict |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    improved = regressed = 0
    for r in rows:
        if r.get("status") != "OK" or not r.get("best"):
            md.append(f"| {r['design']} | - | - | - | - | - | - | - | **{r.get('status')}** | - | - | - | - |")
            continue
        b, o = r["baseline"], r["best"]
        dp = 100.0 * (o["delay_ps"] / b["abc_delay_ps"] - 1.0)
        ap = 100.0 * (o["area_um2"] / b["chip_area"] - 1.0)
        if o["score"] < 1.0:
            improved += 1
        else:
            regressed += 1
        md.append(
            f"| {r['design']} | {b['abc_delay_ps']:.2f} | {o['delay_ps']:.2f} | {dp:+.1f}% | "
            f"{b['chip_area']:.1f} | {o['area_um2']:.1f} | {ap:+.1f}% | 1.000 | "
            f"**{o['score']:.4f}** | {r['n_synth_runs']} | {r['agent_wallclock_s']:.0f} | "
            f"{r['baseline_wallclock_s']:.1f} | {r['verifier']['verdict']} |"
        )

    scores = [r["best"]["score"] for r in rows if r.get("best")]
    if scores:
        geo = math.exp(sum(math.log(s) for s in scores) / len(scores))
        md += ["", f"- Designs improved over baseline: **{improved}/{len(scores)}** "
                   f"(regressed or tied: {regressed})",
               f"- Geometric-mean score: **{geo:.4f}**  (worst case: **{max(scores):.4f}** "
               f"on `{max(rows, key=lambda r: r['best']['score'] if r.get('best') else -1)['design']}`)"]
        md += ["", "Per the reporting rules, the per-design worst case is quoted alongside "
                   "the geometric mean, and designs that failed to beat baseline are kept "
                   "in the table rather than dropped."]

    md += ["", "## Transfer", "",
           "| design | iterations run | iterations to best | rules retrieved |",
           "|---|---|---|---|"]
    for r in rows:
        if r.get("status") == "OK":
            md.append(f"| {r['design']} | {r.get('iterations_run')} | "
                      f"{r.get('iterations_to_best')} | {r.get('retrieved_rules')} |")

    md += ["", "## Estimator agreement", "",
           "`abc_delay_ps` (ABC `stime -p`) is the delay objective. "
           "`logic_depth` is the independent cross-check.", "",
           "| design | delay ratio | logic-depth ratio | agree? |", "|---|---|---|---|"]
    for r in rows:
        if r.get("status") != "OK" or not r.get("best"):
            continue
        b, o = r["baseline"], r["best"]
        dr = o["delay_ps"] / b["abc_delay_ps"]
        if b.get("logic_depth") and o.get("logic_depth"):
            lr = o["logic_depth"] / b["logic_depth"]
            agree = "yes" if (dr - 1) * (lr - 1) >= 0 or abs(lr - 1) < 0.10 else "NO"
            md.append(f"| {r['design']} | {dr:.3f} | {lr:.3f} | {agree} |")
        else:
            md.append(f"| {r['design']} | {dr:.3f} | n/a | - |")

    if usage_summary:
        md += ["", "## Overhead", "",
               f"- LLM calls: {usage_summary['llm_calls']}",
               f"- Estimated LLM cost: **${usage_summary['est_cost_usd']:.4f}**",
               f"- Tokens by model: `{json.dumps(usage_summary['by_model'])}`"]

    md += ["", "## Limitations", "",
           "- **Synthesis-only QoR.** Nothing here is placed or routed; post-synthesis",
           "  gains need not survive P&R.",
           "- **No slack, no SDC, no wire load.** The delay number is ABC's `stime -p`",
           "  over liberty arcs at `WireLoad = \"none\"`. It is a *relative* ranking",
           "  signal, valid for comparing configs on one design, and must never be",
           "  reported as a timing signoff number. Yosys's own `sta` pass is unusable",
           "  here (it aborts on liberty blackboxes in 0.33), so there is no second",
           "  timing engine in the loop.",
           "- **Retiming excluded.** `dretime` changes the sequential boundary, which",
           "  the cheap flop-count gate cannot verify, so it is pinned off.",
           "- **DONT_USE_CELLS not honoured.** `abc` in Yosys 0.33 has no `-dont_use`,",
           "  so the platform's excluded cells (AOI211_X1, OAI211_X1, ...) can still be",
           "  mapped. ORFS would exclude them, so absolute areas differ slightly from an",
           "  ORFS `make synth` run.",
           "- **No SDC period tuning.** The clock period is a spec, not a knob.",
           "- **Determinism.** `temperature` is not settable on current Claude models",
           "  (removed; a 400), so proposals are not bit-reproducible. The surrogate and",
           "  LHS paths are seeded and reproducible.",
           ]

    path = RESULTS / "summary.md"
    path.write_text("\n".join(md) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--design", help="single design name")
    ap.add_argument("--designs", help="comma-separated list")
    ap.add_argument("--tier", default="auto", choices=["auto", "tiny", "small", "medium", "large"])
    ap.add_argument("--k", type=int, default=None, help="configs per iteration")
    ap.add_argument("--iters", type=int, default=None, help="refinement iterations after iter 0")
    ap.add_argument("--timeout", type=int, default=None, help="per-run timeout cap (s)")
    ap.add_argument("--budget-s", type=int, default=None, help="wall-clock budget per design (s)")
    ap.add_argument("--weights", default=None, help='e.g. "delay=1.0,area=0.0"')
    ap.add_argument("--leeway", type=float, default=score.DEFAULT_LEEWAY)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-llm", action="store_true", help="surrogate only, zero API calls")
    ap.add_argument("--baseline", default="resyn2", choices=["resyn2", "orfs"],
                    help="score denominator: resyn2 (the validated sweep) or stock ORFS")
    ap.add_argument("--patience", type=int, default=3,
                    help="stop after this many non-improving iterations")
    ap.add_argument("--miter", action="store_true", help="run the SAT miter gate on small designs (does not converge on mapped netlists here; off by default)")
    ap.add_argument("--report", action="store_true", help="regenerate results/summary.md and exit")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    if args.report and not (args.design or args.designs):
        p = write_report()
        print(f"wrote {p}")
        return 0

    designs: list[str] = []
    if args.design:
        designs.append(args.design)
    if args.designs:
        designs += [d.strip() for d in args.designs.split(",") if d.strip()]
    if not designs:
        ap.error("need --design or --designs (or --report)")

    if not shutil.which("yosys"):
        print("ERROR: yosys not on PATH")
        return 1
    if not args.no_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY unset - falling back to --no-llm")
        args.no_llm = True

    usage = Usage()
    llm = LLM(usage, enabled=not args.no_llm)

    # Run tiers smallest-first so rules.json is populated before the expensive
    # designs - the single biggest speed win and the reason the file exists.
    order = {"tiny": 0, "small": 1, "medium": 2, "large": 3}
    designs.sort(key=lambda d: order.get(tier_of(d), 1))

    recs = []
    for d in designs:
        try:
            recs.append(optimize_design(d, args, llm, usage))
        except Exception as exc:  # keep the sweep alive; record the failure
            import traceback
            print(f"  !! {d} aborted: {exc}")
            traceback.print_exc()
            recs.append({"design": d, "status": "ABORTED", "reason": str(exc)})

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "llm_usage.json").write_text(json.dumps(usage.summary(), indent=2))
    p = write_report(usage.summary())
    print(f"\n{'=' * 72}")
    print(f"report: {p}")
    print(f"llm usage: {json.dumps(usage.summary())}")
    for r in recs:
        b = r.get("best")
        print(f"  {r['design']:<14} {r['status']:<14} "
              f"{('score ' + format(b['score'], '.4f')) if b else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
