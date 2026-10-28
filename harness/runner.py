"""Parallel Yosys launcher with timeout, plus ORFS design introspection."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import knobs, metrics

HERE = Path(__file__).resolve().parent
TCL = HERE / "synth_templated.tcl"
FLOW = Path(knobs.FLOW)

_PRINT_MK = "print-%:\n\t@echo \"$($*)\"\n"


def _mkvar(design: str, var: str, workdir: Path) -> str:
    """Expand an ORFS make variable against a design's own config.mk.

    Reading the variables through make (rather than re-parsing config.mk) is
    what keeps the file lists faithful to what ORFS itself would synthesise.
    """
    pmk = workdir / "print.mk"
    if not pmk.exists():
        pmk.parent.mkdir(parents=True, exist_ok=True)
        pmk.write_text(_PRINT_MK)
    cmd = [
        "make", "-s", "-C", str(FLOW), "-f", "Makefile", "-f", str(pmk),
        f"DESIGN_CONFIG=./designs/nangate45/{design}/config.mk", f"print-{var}",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return out.stdout.strip()
    except subprocess.SubprocessError:
        return ""


def _clk_ns(sdc_path: str) -> float:
    try:
        text = Path(sdc_path).read_text()
    except OSError:
        return 1.0
    m = re.search(r"set\s+clk_period\s+([0-9.]+)", text)
    if not m:
        m = re.search(r"create_clock.*?-period\s+([0-9.]+)", text)
    return float(m.group(1)) if m else 1.0


def load_design(design: str, workdir: Path) -> dict[str, Any]:
    """Introspect one nangate45 design into a spec dict."""
    cfg_mk = FLOW / "designs" / "nangate45" / design / "config.mk"
    if not cfg_mk.exists():
        raise FileNotFoundError(f"no config.mk for design {design!r}")
    raw = cfg_mk.read_text()

    top = _mkvar(design, "DESIGN_NAME", workdir)
    vfiles = _mkvar(design, "VERILOG_FILES", workdir).split()
    incs = _mkvar(design, "VERILOG_INCLUDE_DIRS", workdir).split()
    addlibs = _mkvar(design, "ADDITIONAL_LIBS", workdir).split()
    sdc = _mkvar(design, "SDC_FILE", workdir).strip()
    frontend = _mkvar(design, "SYNTH_HDL_FRONTEND", workdir).strip()

    sv = [f for f in vfiles if f.endswith((".sv", ".svh"))]

    return {
        "design": design,
        "top": top,
        "verilog_files": vfiles,
        "include_dirs": incs,
        "additional_libs": addlibs,
        "sdc": sdc,
        "clk_ns": _clk_ns(sdc),
        "frontend": frontend,
        "sv_files": sv,
        # ORFS sets ABC_AREA=1 for some designs; the baseline must honour it.
        "abc_area": bool(re.search(r"^\s*export\s+ABC_AREA\s*=\s*1", raw, re.M)),
        # gcd clears ADDER_MAP_FILE ("Adders degrade GCD")
        "no_adder_map": bool(re.search(r"^\s*export\s+ADDER_MAP_FILE\s*:?=\s*$", raw, re.M)),
        "rtl_lines": sum(
            len(Path(f).read_text(errors="ignore").splitlines())
            for f in vfiles if Path(f).exists()
        ),
    }


def preflight(spec: dict[str, Any]) -> str | None:
    """Return a reason string if this design cannot be synthesised here."""
    if spec["frontend"] and spec["frontend"] != "verilog":
        return (
            f"design requires SYNTH_HDL_FRONTEND={spec['frontend']}; no slang/sv2v "
            "or Yosys plugin is installed, and Yosys 0.33's native parser rejects "
            "constructs these designs use (e.g. unpacked array ports)"
        )
    if spec["sv_files"]:
        return f"design has SystemVerilog sources ({len(spec['sv_files'])} .sv files)"
    missing = [f for f in spec["verilog_files"] if not Path(f).exists()]
    if missing:
        return f"missing source files: {missing[:3]}"
    if not spec["top"]:
        return "could not determine DESIGN_NAME"
    return None


def run_one(
    spec: dict[str, Any],
    cfg: dict[str, Any] | None,
    rundir: Path,
    timeout_s: int,
    label: str,
) -> dict[str, Any]:
    """One Yosys invocation. cfg=None means the stock-ORFS baseline arm."""
    rundir.mkdir(parents=True, exist_ok=True)
    out_v = rundir / "netlist.v"
    log = rundir / "yosys.log"

    if cfg is None:
        # baseline arm; which baseline is chosen by spec["baseline_mode"]
        if spec.get("baseline_mode", "orfs") == "resyn2":
            env_extra = knobs.resyn2_baseline_env(spec, str(out_v))
        else:
            constr = rundir / "abc.constr"
            knobs.write_abc_constr(str(constr))
            env_extra = knobs.baseline_env(spec, str(out_v), str(constr))
    else:
        env_extra = knobs.render_env(spec, cfg, str(out_v))

    env = dict(os.environ)
    env.update(env_extra)
    (rundir / "env.json").write_text(json.dumps(env_extra, indent=2))
    if cfg is not None:
        (rundir / "config.json").write_text(json.dumps(cfg, indent=2))

    t0 = time.time()
    status = "OK"
    try:
        proc = subprocess.run(
            ["yosys", "-c", str(TCL), "-l", str(log)],
            env=env, capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            status = "ERROR"
        (rundir / "stderr.txt").write_text((proc.stderr or "")[-8000:])
    except subprocess.TimeoutExpired:
        status = "TIMEOUT"
    except OSError as exc:
        status = "ERROR"
        (rundir / "stderr.txt").write_text(str(exc))
    wall = time.time() - t0

    row: dict[str, Any] = {
        "label": label,
        "config": cfg,
        "status": status,
        "wall_time_s": round(wall, 2),
        "rundir": str(rundir),
    }

    if log.exists():
        text = log.read_text(errors="ignore")
        row.update(metrics.parse_log(text, spec["top"]))
        if status == "ERROR":
            errs = [ln for ln in text.splitlines() if ln.startswith("ERROR")]
            row["error_tail"] = errs[-3:] if errs else text.splitlines()[-3:]
    if status == "OK" and out_v.exists():
        row["signature"] = metrics.netlist_signature(str(out_v), spec["top"])
    if status == "OK" and not metrics.metrics_complete(row):
        row["status"] = "ERROR"
        row.setdefault("error_tail", ["metrics missing from log"])

    return row


def run_batch(
    spec: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any] | None]],
    iterdir: Path,
    timeout_s: int,
    parallel: int,
) -> list[dict[str, Any]]:
    """Run K configs concurrently. Yosys is single-threaded, so this is
    process-level parallelism bounded by core count / free RAM."""
    results: list[dict[str, Any]] = [None] * len(jobs)  # type: ignore[list-item]

    def task(i: int) -> None:
        label, cfg = jobs[i]
        results[i] = run_one(
            spec, cfg, iterdir / f"run{i:02d}_{label}", timeout_s, label
        )

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        list(pool.map(task, range(len(jobs))))
    return results
