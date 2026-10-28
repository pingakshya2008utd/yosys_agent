"""Knob schema and config -> Yosys/ABC rendering.

The schema here comes from Step 0 discovery against THIS machine's ORFS
checkout and Yosys build, not from priors. Findings that shaped it:

  * ABC_CLOCK_PERIOD_IN_PS does not exist in this ORFS. The equivalent lever
    survives as `abc_D_scale`, which we apply directly as `abc -D <ps>`.
  * This ORFS's scripts/synth.tcl calls `abc_new`, which Yosys 0.33 does not
    have. So ORFS "Action Space A" (driving `make synth`) is NOT viable here;
    we own the whole script instead (the plan's Action Space B).
  * `abc` in Yosys 0.33 has no `-dont_use`, so the platform's DONT_USE_CELLS
    (AOI211_X1, OAI211_X1, ...) cannot be excluded. Documented deviation.
  * Yosys invokes `yosys-abc -s`, which skips abc.rc, so NO ABC aliases exist.
    Every script must be written in primitive commands.
"""

from __future__ import annotations

import itertools
import random
from typing import Any

# --- platform paths (nangate45) ------------------------------------------
ORFS = "/home/ubuntu/chisage/OpenROAD-flow-scripts"
FLOW = f"{ORFS}/flow"
PLATFORM_DIR = f"{FLOW}/platforms/nangate45"
LIB_FILE = f"{PLATFORM_DIR}/lib/NangateOpenCellLibrary_typical.lib"
ADDER_MAP_FILE = f"{PLATFORM_DIR}/cells_adders.v"

# This platform's real ORFS ABC scripts and driver/load constraints, used
# verbatim for the baseline arm so the score denominator is stock ORFS
# re-measured on this machine (never a number quoted from the repo).
ORFS_ABC_SPEED = f"{FLOW}/scripts/abc_speed.script"
ORFS_ABC_AREA = f"{FLOW}/scripts/abc_area.script"
ABC_DRIVER_CELL = "BUF_X1"
ABC_LOAD_IN_FF = "3.898"

# The validated `resyn2` script from results/synth_resyn2_summary.csv, used as
# the baseline when --baseline resyn2. resyn2 AND every command inside it are
# abc.rc aliases, and Yosys runs `yosys-abc -s` which skips abc.rc, so both
# levels are expanded to primitives:
#   resyn2 -> b; rw; rf; b; rw; rwz; b; rfz; rwz; b
#          -> balance; rewrite; refactor; balance; rewrite; rewrite -z;
#             balance; refactor -z; rewrite -z; balance
RESYN2_BODY = (
    "strash;balance;rewrite;refactor;balance;rewrite;rewrite,-z;"
    "balance;refactor,-z;rewrite,-z;balance"
)
RESYN2_SCRIPT = f"+{RESYN2_BODY};map,{{D}};topo;stime,-p"

# --- the 8-dimensional action space --------------------------------------
# Deliberately small: the ORFS-agent paper shows 12 dims needs heavier
# surrogate tooling to beat 4; 8 is comfortable for a forest + EI.

EXTRA_OPT_CHOICES = [
    "",
    "opt -full",
    "opt -full; share; opt -fast",
    "opt -full; alumacc; opt -fast",
    "share; opt -full; opt -fast",
]

MAPPER_CHOICES = [
    "&nf",        # delay-driven mapper, operates in ABC's &-space
    "map",        # classic mapper
    "map -a",     # area mode
    "map -B 0.9", # area with a bias, as ORFS's abc_area.script uses
]

POST_CHOICES = [
    "",
    "topo;dnsize;upsize",
    "buffer;upsize;dnsize",
    "topo;dnsize",
]

# ABC flow family. `dch_map` is the dch/map core; `syn2_if` mirrors this
# platform's real ORFS abc_speed.script (&syn2 / &if -g -K 6 / &synch2 / &nf),
# which is a strong delay flow and worth having as a searchable arm.
# `resyn2` is the validated balance/rewrite/refactor script used as baseline.
FLOW_CHOICES = ["dch_map", "syn2_if", "resyn2"]

SCHEMA: dict[str, dict[str, Any]] = {
    "flatten":      {"type": "bool"},
    "extra_opt":    {"type": "cat",   "choices": EXTRA_OPT_CHOICES},
    "abc_D_scale":  {"type": "float", "lo": 0.50, "hi": 1.10},
    "resyn_loops":  {"type": "int",   "lo": 1, "hi": 4},
    "use_dch":      {"type": "bool"},
    "abc_flow":     {"type": "cat",   "choices": FLOW_CHOICES},
    "mapper":       {"type": "cat",   "choices": MAPPER_CHOICES},
    "post":         {"type": "cat",   "choices": POST_CHOICES},
    "adder_map":    {"type": "bool"},
}

DIMS = list(SCHEMA.keys())

# Retiming changes the sequential boundary, so flop counts legitimately
# differ and the cheap port/flop sanity gate cannot verify it. Per the plan's
# §7 guidance we pin it off and say so, rather than ship an unverified track.
RETIME = False


def default_config() -> dict[str, Any]:
    """The reference point: the resyn2 baseline expressed in the search space."""
    return {
        "flatten": True,
        "extra_opt": "",
        "abc_D_scale": 1.0,
        "resyn_loops": 1,
        "use_dch": False,
        "abc_flow": "resyn2",
        "mapper": "map",
        "post": "",
        "adder_map": False,
    }


# --- named presets (warm-start arms) -------------------------------------
# These all live inside the 8-dim space. The BASELINE is separate: it runs
# ORFS's own ABC script file (see baseline_env) and is not a point in here.
PRESETS: dict[str, dict[str, Any]] = {
    "resyn2_ref": default_config(),
    "speed_dch": {
        "flatten": True, "extra_opt": "opt -full", "abc_D_scale": 0.85,
        "resyn_loops": 2, "use_dch": True, "abc_flow": "dch_map",
        "mapper": "&nf", "post": "buffer;upsize;dnsize", "adder_map": False,
    },
    "speed_hard": {
        "flatten": True, "extra_opt": "opt -full; share; opt -fast",
        "abc_D_scale": 0.70, "resyn_loops": 3, "use_dch": True,
        "abc_flow": "dch_map", "mapper": "&nf",
        "post": "buffer;upsize;dnsize", "adder_map": True,
    },
    "orfs_speed_like": {
        "flatten": True, "extra_opt": "opt -full", "abc_D_scale": 0.80,
        "resyn_loops": 3, "use_dch": True, "abc_flow": "syn2_if",
        "mapper": "&nf", "post": "buffer;upsize;dnsize", "adder_map": False,
    },
    "area_map": {
        "flatten": True, "extra_opt": "opt -full", "abc_D_scale": 1.10,
        "resyn_loops": 1, "use_dch": False, "abc_flow": "dch_map",
        "mapper": "map -B 0.9", "post": "topo;dnsize", "adder_map": False,
    },
    "balanced": {
        "flatten": True, "extra_opt": "opt -full; alumacc; opt -fast",
        "abc_D_scale": 0.90, "resyn_loops": 2, "use_dch": True,
        "abc_flow": "dch_map", "mapper": "map",
        "post": "topo;dnsize;upsize", "adder_map": True,
    },
}


def clamp(cfg: dict[str, Any]) -> dict[str, Any]:
    """Coerce an arbitrary dict (e.g. from the LLM) into a legal config."""
    out = default_config()
    for k, spec in SCHEMA.items():
        if k not in cfg or cfg[k] is None:
            continue
        v = cfg[k]
        try:
            if spec["type"] == "bool":
                out[k] = bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "yes")
            elif spec["type"] == "int":
                out[k] = max(spec["lo"], min(spec["hi"], int(round(float(v)))))
            elif spec["type"] == "float":
                out[k] = round(max(spec["lo"], min(spec["hi"], float(v))), 4)
            else:  # categorical
                sv = str(v).strip()
                out[k] = sv if sv in spec["choices"] else out[k]
        except (TypeError, ValueError):
            pass  # keep the default for this dim
    return out


def config_key(cfg: dict[str, Any]) -> str:
    """Stable identity for dedup across iterations."""
    return "|".join(f"{k}={cfg[k]}" for k in DIMS)


def to_vector(cfg: dict[str, Any]) -> list[float]:
    """Numeric encoding for the surrogate model (ordinal for categoricals)."""
    vec: list[float] = []
    for k, spec in SCHEMA.items():
        v = cfg[k]
        if spec["type"] == "bool":
            vec.append(1.0 if v else 0.0)
        elif spec["type"] in ("int", "float"):
            lo, hi = spec["lo"], spec["hi"]
            vec.append((float(v) - lo) / (hi - lo) if hi > lo else 0.0)
        else:
            ch = spec["choices"]
            vec.append(ch.index(v) / max(1, len(ch) - 1) if v in ch else 0.0)
    return vec


def random_config(rng: random.Random) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for k, spec in SCHEMA.items():
        if spec["type"] == "bool":
            cfg[k] = rng.random() < 0.5
        elif spec["type"] == "int":
            cfg[k] = rng.randint(spec["lo"], spec["hi"])
        elif spec["type"] == "float":
            cfg[k] = round(rng.uniform(spec["lo"], spec["hi"]), 4)
        else:
            cfg[k] = rng.choice(spec["choices"])
    return cfg


def lhs_configs(n: int, rng: random.Random) -> list[dict[str, Any]]:
    """Latin-hypercube-ish fill: stratify each dim independently, then shuffle."""
    cols: dict[str, list[Any]] = {}
    for k, spec in SCHEMA.items():
        if spec["type"] == "bool":
            vals = [i % 2 == 0 for i in range(n)]
        elif spec["type"] == "int":
            span = spec["hi"] - spec["lo"] + 1
            vals = [spec["lo"] + (i % span) for i in range(n)]
        elif spec["type"] == "float":
            lo, hi = spec["lo"], spec["hi"]
            vals = [round(lo + (hi - lo) * (i + 0.5) / n, 4) for i in range(n)]
        else:
            ch = spec["choices"]
            vals = [ch[i % len(ch)] for i in range(n)]
        rng.shuffle(vals)
        cols[k] = vals
    return [{k: cols[k][i] for k in SCHEMA} for i in range(n)]


def neighbours(cfg: dict[str, Any], rng: random.Random, n: int = 8) -> list[dict[str, Any]]:
    """Single-knob perturbations around a config, for exploitation."""
    out = []
    for _ in range(n):
        c = dict(cfg)
        k = rng.choice(DIMS)
        spec = SCHEMA[k]
        if spec["type"] == "bool":
            c[k] = not c[k]
        elif spec["type"] == "int":
            c[k] = max(spec["lo"], min(spec["hi"], c[k] + rng.choice([-1, 1])))
        elif spec["type"] == "float":
            step = (spec["hi"] - spec["lo"]) * 0.15
            c[k] = round(max(spec["lo"], min(spec["hi"], c[k] + rng.uniform(-step, step))), 4)
        else:
            c[k] = rng.choice([x for x in spec["choices"] if x != c[k]])
        out.append(c)
    return out


# --- ABC script rendering -------------------------------------------------

def abc_script(cfg: dict[str, Any]) -> str:
    """Build the ABC -script string.

    Two correctness details that silently break ABC if got wrong:

    1. `&nf` is a GIA (&-space) mapper and needs `&get -n` / `&put` around it;
       `map` variants work on the regular network and must NOT be wrapped in
       &-space. So the core differs by mapper family.
    2. Yosys replaces commas with blanks in a `+`-prefixed script string, so
       the rendered string must use commas, never spaces.
    """
    mapper = cfg["mapper"]
    loops = max(1, int(cfg["resyn_loops"]))
    flow = cfg.get("abc_flow", "dch_map")

    body = "strash;ifraig;scorr;dc2;"
    if RETIME:
        body += "dretime;"
    body += "strash;"

    if flow == "resyn2":
        # The validated baseline script. `mapper`/`use_dch` do not apply;
        # resyn_loops repeats the balance/rewrite/refactor body.
        script = ""
        for _ in range(loops):
            script += RESYN2_BODY.replace(",", " ") + ";"
        script += f"map {{D}};"
    elif flow == "syn2_if":
        # Mirrors ORFS abc_speed.script: an initial dch/nf map, then repeated
        # &syn2 / &if -g -K 6 / &synch2 / &nf refinement, all inside GIA space.
        script = body + "&get -n;"
        script += "&dch -f;" if cfg["use_dch"] else "&st;"
        script += "&nf {D};"
        for _ in range(loops):
            script += "&st;&syn2;&if -g -K 6;&synch2;&nf {D};"
        script += "&put;"
    elif mapper.startswith("&"):
        # &get re-imports into GIA each loop, so repetition is safe.
        core = ("&get -n;&dch -f;" if cfg["use_dch"] else "&get -n;&st;")
        core += f"{mapper} {{D}};&put;"
        script = body + core * loops
    else:
        # Must re-strash at the top of EVERY loop: after `map` the network is
        # a mapped netlist, and `dch`/`dc2` reject that with
        #   "Error: This command works only for strashed networks."
        # so a second iteration aborts ABC without this.
        core = "strash;" + ("dch -f;" if cfg["use_dch"] else "")
        core += f"{mapper} {{D}};"
        script = body + core * loops

    if cfg["post"]:
        script += cfg["post"].strip().strip(";") + ";"
    # stime -p gives us the delay number; topo first so it is well defined.
    script += "topo;stime -p"
    return "+" + script.replace(" ", ",")


def _common_env(design: dict[str, Any], out_v: str) -> dict[str, str]:
    return {
        "YA_LIB_FILE": LIB_FILE,
        "YA_ADD_LIBS": " ".join(design.get("additional_libs", [])),
        "YA_VERILOG_FILES": " ".join(design["verilog_files"]),
        "YA_INC_DIRS": " ".join(design.get("include_dirs", [])),
        "YA_TOP": design["top"],
        "YA_OUT_V": out_v,
    }


def render_env(design: dict[str, Any], cfg: dict[str, Any], out_v: str) -> dict[str, str]:
    """Environment for harness/synth_templated.tcl, searched configs."""
    clk_ns = float(design["clk_ns"])
    abc_d_ps = max(1, int(round(clk_ns * 1000.0 * float(cfg["abc_D_scale"]))))
    env = _common_env(design, out_v)
    env.update({
        "YA_SYNTH_ARGS": "-flatten" if cfg["flatten"] else "",
        "YA_EXTRA_OPT": cfg["extra_opt"],
        "YA_ADDER_MAP": ADDER_MAP_FILE if cfg["adder_map"] else "",
        "YA_ABC_D": str(abc_d_ps),
        "YA_ABC_MODE": "string",
        "YA_ABC_SCRIPT": abc_script(cfg),
        "YA_ABC_CONSTR": "",
    })
    return env


def baseline_env(design: dict[str, Any], out_v: str, constr_path: str) -> dict[str, str]:
    """Environment reproducing the stock ORFS synthesis config for a design.

    ORFS picks abc_area.script when the design sets ABC_AREA=1 (gcd, jpeg on
    nangate45) and abc_speed.script otherwise. Both scripts use `-c` variants
    (stime -c, buffer -c, upsize -c, dnsize -c) which require the driving-cell
    / load constraint file ORFS writes, so we write the same file.
    """
    script = ORFS_ABC_AREA if design.get("abc_area") else ORFS_ABC_SPEED
    env = _common_env(design, out_v)
    env.update({
        # ORFS synthesises flat by default (SYNTH_HIERARCHICAL unset) and has
        # ADDER_MAP_FILE set for nangate45, except where a design clears it.
        "YA_SYNTH_ARGS": "-flatten",
        "YA_EXTRA_OPT": "",
        "YA_ADDER_MAP": "" if design.get("no_adder_map") else ADDER_MAP_FILE,
        "YA_ABC_D": "0",             # ORFS passes no -D; 0 means "omit"
        "YA_ABC_MODE": "file",
        "YA_ABC_SCRIPT": script,
        "YA_ABC_CONSTR": constr_path,
    })
    return env


def resyn2_baseline_env(design: dict[str, Any], out_v: str) -> dict[str, str]:
    """Environment reproducing the validated resyn2 sweep for a design.

    Matches results/synth_resyn2_summary.csv: flat, no extra coarse opt, no
    adder map, `abc -D <clk_ns * 1000>` with the fully expanded resyn2 script.
    The sweep that produced that CSV had no delay metric, so delay is measured
    here; area/cells/flops are cross-checked against the CSV by run.py.
    """
    clk_ns = float(design["clk_ns"])
    env = _common_env(design, out_v)
    env.update({
        "YA_SYNTH_ARGS": "-flatten",
        "YA_EXTRA_OPT": "",
        "YA_ADDER_MAP": "",
        "YA_ABC_D": str(max(1, int(round(clk_ns * 1000.0)))),
        "YA_ABC_MODE": "string",
        "YA_ABC_SCRIPT": RESYN2_SCRIPT,
        "YA_ABC_CONSTR": "",
    })
    return env


def write_abc_constr(path: str) -> None:
    with open(path, "w") as fh:
        fh.write(f"set_driving_cell {ABC_DRIVER_CELL}\n")
        fh.write(f"set_load {ABC_LOAD_IN_FF}\n")


def describe_schema() -> str:
    """Compact schema text for the strategist prompt."""
    lines = []
    for k, spec in SCHEMA.items():
        if spec["type"] == "bool":
            lines.append(f"- {k}: bool")
        elif spec["type"] in ("int", "float"):
            lines.append(f"- {k}: {spec['type']} in [{spec['lo']}, {spec['hi']}]")
        else:
            lines.append(f"- {k}: one of {spec['choices']!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe_schema())
    print()
    for name, cfg in PRESETS.items():
        print(f"{name:18s} {abc_script(cfg)}")
