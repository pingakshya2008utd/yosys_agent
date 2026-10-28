"""Parse a Yosys log into a metrics row. Pure Python - no LLM in the hot loop.

The `analyst` agent exists only as a fallback for when these regexes come up
empty (see run.py), so the per-run cost stays at zero tokens.
"""

from __future__ import annotations

import re
from typing import Any

# Nangate45 sequential cells (flops + latches)
SEQ_RE = re.compile(
    r"^\s+(S?DFFR?S?_X\d+|DLH_X\d+|DLL_X\d+|TLAT_X\d+)\s+(\d+)\s*$", re.M
)

_CELLS = re.compile(r"Number of cells:\s+(\d+)")
# `stat -top` prints the aggregated total as "Chip area for TOP module";
# a plain per-module block says "Chip area for module". Match both.
_AREA = re.compile(r"Chip area for (?:top )?module '\\?([^']+)':\s+([0-9.]+)")
_AREA_TOP = re.compile(r"Chip area for top module '\\?([^']+)':\s+([0-9.]+)")
# ABC `stime -p` summary line, e.g.
#   ABC: WireLoad = "none"  Gates = 327 (17.1 %)  Cap = 2.8 ff (0.0 %)
#        Area = 301.91 (100.0 %)   Delay = 644.27 ps  ( 29.4 %)
_ABC_DELAY = re.compile(r"Delay\s*=\s*([0-9.]+)\s*ps")
_ABC_AREA = re.compile(r"Area\s*=\s*([0-9.]+)")
_LTP = re.compile(r"Longest topological path in \S+ \(length=(\d+)\)")
_PEAK = re.compile(r"MEM:\s+([0-9.]+)\s*MB peak")
_PORTS = re.compile(r"^\s+(?:input|output|inout)\s+.*$", re.M)


def _seq_table(block: str) -> tuple[int, str]:
    pairs = [(m.group(1), int(m.group(2))) for m in SEQ_RE.finditer(block)]
    total = sum(n for _, n in pairs)
    desc = " ".join(f"{c}={n}" for c, n in pairs)
    return total, desc


def _last_stat_block(log: str, top: str) -> str:
    """Isolate the authoritative stat block.

    With `stat -top`, a hierarchical design prints one block per module and
    then an aggregated `=== design hierarchy ===` block - that aggregate is
    the only correct one, so prefer it. A flat design has no hierarchy block
    and falls back to `=== <top> ===`.
    """
    starts = [m.end() for m in re.finditer(r"=== design hierarchy ===", log)]
    if not starts:
        marker = re.compile(r"=== *\\?" + re.escape(top) + r" *===")
        starts = [m.end() for m in marker.finditer(log)]
    if not starts:
        return log
    tail = log[starts[-1]:]
    end = tail.find("Chip area")
    return tail[: end + 200] if end != -1 else tail[:20000]


def parse_log(log_text: str, top: str) -> dict[str, Any]:
    """Extract every metric from one Yosys log."""
    blk = _last_stat_block(log_text, top)

    cells = _CELLS.search(blk)
    seq_total, seq_desc = _seq_table(blk)

    # Prefer the hierarchy-aggregated "top module" area; it is the only one
    # that is correct for a non-flattened netlist.
    area = None
    for m in _AREA_TOP.finditer(log_text):
        area = float(m.group(2))
    if area is None:
        for m in _AREA.finditer(log_text):
            if m.group(1).lstrip("\\") == top:
                area = float(m.group(2))
    if area is None:
        last = None
        for m in _AREA.finditer(log_text):
            last = m
        if last:
            area = float(last.group(2))

    # ABC stime: take the LAST occurrence (the final mapped network)
    delay = None
    for m in _ABC_DELAY.finditer(log_text):
        delay = float(m.group(1))
    abc_area = None
    for m in _ABC_AREA.finditer(log_text):
        abc_area = float(m.group(1))

    # Two ltp passes: [0] raw through flops (unreliable), [-1] true depth
    ltps = [int(m.group(1)) for m in _LTP.finditer(log_text)]
    ltp_raw = ltps[0] if ltps else None
    depth = ltps[-1] if ltps else None
    if len(ltps) == 1:
        depth = None  # only the raw pass ran; don't pass it off as depth

    peak = None
    for m in _PEAK.finditer(log_text):
        peak = float(m.group(1))

    loops = log_text.count("Detected loop")

    return {
        "chip_area": area,
        "cell_count": int(cells.group(1)) if cells else None,
        "seq_cells": seq_total if seq_total else None,
        "seq_desc": seq_desc,
        "abc_delay_ps": delay,
        "abc_area": abc_area,
        "ltp_raw": ltp_raw,
        "logic_depth": depth,
        "ltp_loops": loops,
        "peak_rss_mb": peak,
    }


def netlist_signature(path: str, top: str | None = None) -> dict[str, Any]:
    """Cheap equivalence proxy: top port list + sequential cell count.

    Used by the verifier gate - a config that changes the port list or the
    flop count has changed the design, not just its implementation.

    The top module must be located by NAME, not taken as the first module in
    the file: with `flatten: False` the netlist is hierarchical and the first
    module is a submodule, which made every non-flattened config look like it
    had changed the port list.
    """
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return {"ports": None, "seq": None}

    head = None
    if top:
        m = re.search(r"^module\s+\\?" + re.escape(top) + r"\b", text, re.M)
        if m:
            end = text.find("endmodule", m.start())
            head = text[m.start(): end if end != -1 else len(text)]
    if head is None:
        head = text[: text.find("endmodule")] if "endmodule" in text else text

    ports = sorted(
        re.sub(r"\s+", " ", ln.strip().rstrip(";"))
        for ln in _PORTS.findall(head)
    )
    # NOTE: deliberately no flop count here. Counting flop instances in the
    # netlist text undercounts a hierarchical netlist - a submodule
    # instantiated twice has its flops written once - which made every
    # `flatten: false` config look like it had dropped flops (35 -> 18 on gcd)
    # and get falsely rejected. The flop-count gate uses the hierarchy-
    # aggregated `seq_cells` from `stat -top` instead; see score.score_row.
    return {"ports": ports}


def metrics_complete(m: dict[str, Any]) -> bool:
    """Did the Python parser get everything the objective needs?"""
    return m.get("chip_area") is not None and m.get("abc_delay_ps") is not None
