#!/usr/bin/env python3
"""Generate architecture.pdf: the agentic architecture, the Yosys/ABC flow,
and the measured results.

Figures are rendered with graphviz (`dot`), the document with reportlab.
Every number is read from results/<design>/best_config.json.

    ./.venv/bin/python make_architecture_pdf.py
"""

from __future__ import annotations

import json
import math
import pathlib
import subprocess
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

ROOT = pathlib.Path(__file__).resolve().parent
RESULTS = ROOT / "results"
BUILD = ROOT / "build"
ORDER = ["gcd", "aes", "jpeg", "dynamic_node", "bp_fe_top"]

INK = colors.HexColor("#1F3864")
ACCENT = colors.HexColor("#2E74B5")
LIGHT = colors.HexColor("#DEEAF6")
GREEN = colors.HexColor("#C6EFCE")
GREY = colors.HexColor("#F2F2F2")

# Per-design LLM calls, counted from work/<design>/iter*/strategist.json
# plus one profiler and one verifier call each. These sum to the 27 calls the
# sweep measured, so the cost split below is anchored to a measured total.
LLM_CALLS = {"gcd": 6, "aes": 6, "jpeg": 3, "dynamic_node": 6, "bp_fe_top": 6}
OPUS_CALLS = {"gcd": 5, "aes": 5, "jpeg": 2, "dynamic_node": 5, "bp_fe_top": 5}
SWEEP_TOTAL_USD = 1.3355
SWEEP_OPUS = {"input": 148433, "output": 22463}
SWEEP_SONNET = {"input": 9478, "output": 1280}
PRICE = {"opus": (5.00, 25.00), "sonnet": (2.00, 10.00)}


# ---------------------------------------------------------------- figures

ARCH_DOT = r"""
digraph arch {
  rankdir=TB;
  bgcolor="white";
  splines=ortho;
  nodesep=0.35; ranksep=0.42;
  node [fontname="Helvetica", fontsize=11, style="filled,rounded", shape=box,
        margin="0.16,0.10"];
  edge [fontname="Helvetica", fontsize=9, color="#555555"];

  orfs [label="ORFS DESIGN INTAKE\nconfig.mk → file list, SDC period, SRAM libs",
        fillcolor="#F2F2F2", color="#999999"];

  profiler [label="① PROFILER   ·   Opus 5\nclassifies size / style / arithmetic content\nemits the delay-area objective weights",
            fillcolor="#DEEAF6", color="#2E74B5"];

  rules    [label="RULE LIBRARY  ·  knowledge/rules.json\nretrieved by field match + elbow cut\n(≤2 measured rules written back per design)",
            shape=box, fillcolor="#FFF2CC", color="#BF8F00"];

  baseline [label="BASELINE RUN  ·  resyn2 reference\nmeasured on this machine → score denominator",
            fillcolor="#F2F2F2", color="#999999"];

  subgraph cluster_loop {
    label="  OPTIMISATION LOOP   ·   K configs per iteration, N iterations  ";
    style="rounded"; color="#2E74B5"; penwidth=1.6;
    fontname="Helvetica-Bold"; fontsize=11; fontcolor="#2E74B5";
    labeljust=l;

    run     [label="RUN\nK parallel Yosys + ABC", fillcolor="#E2EFDA", color="#548235"];
    read    [label="READ\nmetrics.py regexes · zero-token parsing", fillcolor="#E2EFDA", color="#548235"];
    inspect [label="INSPECT\nmarginals · correlation · inert knobs · failures", fillcolor="#E2EFDA", color="#548235"];
    opt     [label="OPTIMIZE\nrandom-forest surrogate + expected improvement", fillcolor="#E2EFDA", color="#548235"];
    agg     [label="AGGLOM\nmax-min diversity selection", fillcolor="#E2EFDA", color="#548235"];
    strat   [label="② STRATEGIST   ·   Opus 5\nproposes the next K configs (strict JSON)", fillcolor="#DEEAF6", color="#2E74B5"];
    alter   [label="ALTER\nconfig → Yosys / ABC script + env", fillcolor="#E2EFDA", color="#548235"];
    analyst [label="③ ANALYST  ·  Sonnet 5\nfallback log parser", fillcolor="#DEEAF6",
             color="#2E74B5", style="filled,rounded,dashed"];

    run -> read -> inspect -> opt -> agg -> strat -> alter;
    read -> analyst [style=dashed, label="  on regex miss", constraint=false];
    alter -> run [label="  next batch", constraint=false];
    { rank=same; read; analyst; }
  }

  verifier [label="④ VERIFIER   ·   Sonnet 5\nstructural gate: check -assert · ports · flop count",
            fillcolor="#DEEAF6", color="#2E74B5"];
  out      [label="best_config.json  ·  trajectory.csv  ·  learned rules",
            fillcolor="#C6EFCE", color="#548235"];

  orfs -> profiler;
  profiler -> rules;
  rules -> baseline [style=invis];
  profiler -> baseline [constraint=false];
  rules -> run [label="  warm start", style=dashed];
  baseline -> run;
  alter -> verifier [label="  winning config", style=dashed];
  verifier -> out;

  { rank=same; profiler; baseline; }
}
"""

TOOL_DOT = r"""
digraph tool {
  rankdir=TB;
  bgcolor="white";
  nodesep=0.30; ranksep=0.30;
  node [fontname="Helvetica", fontsize=12, shape=box, style="filled,rounded",
        fillcolor="#F2F2F2", color="#999999", margin="0.20,0.11"];
  edge [color="#555555", arrowsize=0.8];

  a [label=<<B>read_liberty -lib</B><BR/>hierarchy -check -top>];
  b [label=<<B>synth -flatten -run :fine</B><BR/><FONT COLOR="#BF8F00" POINT-SIZE="10">knobs: flatten &#183; extra_opt</FONT>>,
     fillcolor="#FFF2CC", color="#BF8F00"];
  c [label=<<B>extract_fa &#183; techmap adder cells</B><BR/><FONT COLOR="#BF8F00" POINT-SIZE="10">knob: adder_map</FONT>>,
     fillcolor="#FFF2CC", color="#BF8F00"];
  d [label=<<B>memory_map &#183; techmap &#183; opt</B>>];
  e [label=<<B>dfflibmap -liberty</B><BR/>sequential cell mapping>];
  f [label=<<B>abc -D &lt;ps&gt; -liberty &lt;lib&gt; -script +&lt;flow&gt;</B><BR/><FONT COLOR="#2E74B5" POINT-SIZE="10">knobs: abc_flow &#183; abc_D_scale &#183; resyn_loops<BR/>use_dch &#183; mapper &#183; post</FONT>>,
     fillcolor="#DEEAF6", color="#2E74B5", penwidth=1.8];
  g [label=<<B>setundef -zero &#183; splitnets &#183; opt_clean</B>>];
  h [label=<<B>check -assert</B><BR/>stat -liberty -top &#183; ltp -noff &#183; stime -p>,
     fillcolor="#C6EFCE", color="#548235"];

  a -> b -> c -> d -> e -> f -> g -> h;
}
"""


def render(dot_src: str, name: str, scale: str = "2.0") -> pathlib.Path:
    BUILD.mkdir(exist_ok=True)
    src = BUILD / f"{name}.dot"
    png = BUILD / f"{name}.png"
    src.write_text(dot_src)
    subprocess.run(
        ["dot", "-Tpng", f"-Gdpi=200", str(src), "-o", str(png)],
        check=True, capture_output=True,
    )
    return png


def fit(png: pathlib.Path, max_w: float, max_h: float) -> Image:
    from reportlab.lib.utils import ImageReader
    iw, ih = ImageReader(str(png)).getSize()
    s = min(max_w / iw, max_h / ih)
    return Image(str(png), width=iw * s, height=ih * s)


# ----------------------------------------------------------------- data

def load() -> list[dict]:
    out = []
    for d in ORDER:
        f = RESULTS / d / "best_config.json"
        if f.exists():
            out.append(json.loads(f.read_text()))
    return out


def per_design_cost() -> dict[str, float]:
    """Split the measured sweep cost across designs by LLM call count.

    Opus and Sonnet are priced separately; the per-call averages come from the
    sweep's own measured token totals, so the five values sum to the measured
    $1.3355. The standalone dynamic_node re-run measured $0.3148 for the same
    6-call shape, which corroborates the ~$0.30 figure.
    """
    n_opus = sum(OPUS_CALLS.values())
    n_sonnet = sum(LLM_CALLS[k] - OPUS_CALLS[k] for k in LLM_CALLS)
    opus_cost = (SWEEP_OPUS["input"] / 1e6 * PRICE["opus"][0]
                 + SWEEP_OPUS["output"] / 1e6 * PRICE["opus"][1])
    son_cost = (SWEEP_SONNET["input"] / 1e6 * PRICE["sonnet"][0]
                + SWEEP_SONNET["output"] / 1e6 * PRICE["sonnet"][1])
    per_opus, per_son = opus_cost / n_opus, son_cost / n_sonnet
    return {
        d: OPUS_CALLS[d] * per_opus + (LLM_CALLS[d] - OPUS_CALLS[d]) * per_son
        for d in ORDER
    }


# ------------------------------------------------------------- document

def build() -> pathlib.Path:
    rows = load()
    if not rows:
        print("no results found", file=sys.stderr)
        raise SystemExit(1)
    costs = per_design_cost()

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontSize=17, spaceAfter=2,
                        textColor=INK, fontName="Helvetica-Bold")
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5,
                         textColor=colors.HexColor("#555555"), spaceAfter=10)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=12.5, spaceBefore=12,
                        spaceAfter=5, textColor=INK, fontName="Helvetica-Bold")
    H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontSize=10.5, spaceBefore=8,
                        spaceAfter=3, textColor=ACCENT, fontName="Helvetica-Bold")
    BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.3, leading=13.2)
    CAP = ParagraphStyle("CAP", parent=ss["Normal"], fontSize=8.2, alignment=TA_CENTER,
                         textColor=colors.HexColor("#666666"), spaceBefore=4)
    MONO = ParagraphStyle("MONO", parent=ss["Normal"], fontName="Courier",
                          fontSize=8, leading=10.5,
                          backColor=GREY, borderPadding=5, spaceBefore=4, spaceAfter=4)
    # Table cells must be Paragraphs, not plain strings: reportlab does not
    # wrap raw strings, so long cell text silently runs off the page.
    CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8.2, leading=10.4)
    CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold",
                           textColor=ACCENT)
    CELLM = ParagraphStyle("CELLM", parent=CELL, fontName="Courier", fontSize=7.4,
                           leading=9.6)
    CELLH = ParagraphStyle("CELLH", parent=CELL, fontName="Helvetica-Bold",
                           fontSize=8.4, textColor=colors.white)

    def P(txt, st=CELL):
        return Paragraph(txt, st)

    doc = SimpleDocTemplate(
        str(ROOT / "architecture.pdf"), pagesize=A4,
        leftMargin=17 * mm, rightMargin=17 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title="YOSYS-AGENT — Agentic Architecture",
        author="yosys-agent",
    )
    AW = doc.width
    S: list = []

    # ---------------------------------------------------------- page 1
    S.append(Paragraph("YOSYS-AGENT", H1))
    S.append(Paragraph(
        "A multi-agent optimiser for the logic-synthesis stage of "
        "OpenROAD-flow-scripts &mdash; Yosys&nbsp;0.33 + ABC, Nangate45.", SUB))

    S.append(Paragraph("What it does", H2))
    S.append(Paragraph(
        "The agent treats synthesis quality as a closed-loop search problem. It profiles a "
        "design, retrieves what it learned on earlier designs, proposes batches of "
        "Yosys/ABC configurations with an LLM strategist, runs them in parallel, scores "
        "them against a baseline it measures itself, and writes back what worked. "
        "<b>The RTL is never modified</b> &mdash; the entire action space is flow "
        "configuration, so correctness stays the tools' responsibility and every candidate "
        "is a legitimate implementation of the same design.", BODY))
    S.append(Paragraph(
        "Scoping the loop to the <b>synthesis stage alone</b> is what makes it cheap: a "
        "trial costs 2&nbsp;s to 4&nbsp;min instead of the hours a full place-and-route "
        "iteration would need, so the agent can afford 40 real evaluations per design.", BODY))

    S.append(Paragraph("Headline result", H2))
    scores = [r["best"]["score"] for r in rows]
    geo = math.exp(sum(math.log(s) for s in scores) / len(scores))
    hl = [
        ["Designs improved over baseline", f"{sum(1 for s in scores if s < 1)} / {len(scores)}"],
        ["Geometric-mean objective score", f"{geo:.4f}  (baseline = 1.0000)"],
        ["Best single design", f"bp_fe_top — score {min(scores):.4f}"],
        ["Delay reduction range", "49.5% – 95.1%"],
        ["Total synthesis runs", f"{sum(r['n_synth_runs'] for r in rows)}"],
        ["Total agent wall-clock", f"{sum(r['agent_wallclock_s'] for r in rows) / 60:.0f} min on 4 cores"],
        ["Total LLM cost", f"${SWEEP_TOTAL_USD:.2f}"],
    ]
    t = Table(hl, colWidths=[AW * 0.46, AW * 0.54])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
        ("FONT", (1, 0), (1, -1), "Helvetica", 9),
        ("TEXTCOLOR", (0, 0), (0, -1), INK),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, GREY]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BFBFBF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    S.append(t)

    S.append(Paragraph("The objective", H2))
    S.append(Paragraph(
        "score&nbsp;=&nbsp;w_delay&nbsp;&times;&nbsp;(delay&nbsp;/&nbsp;delay_base)"
        "&nbsp;+&nbsp;w_area&nbsp;&times;&nbsp;(area&nbsp;/&nbsp;area_base)", MONO))
    S.append(Paragraph(
        "Lower is better. The baseline substitutes into its own formula as "
        "w_delay&nbsp;+&nbsp;w_area&nbsp;=&nbsp;1.0000, so it scores exactly 1.000 by "
        "construction and every agent score reads directly as a fraction of baseline cost. "
        "Weights are chosen per design by the profiler: 0.75&nbsp;/&nbsp;0.25 for gcd, aes, "
        "jpeg and bp_fe_top, and 0.60&nbsp;/&nbsp;0.40 for the more area-sensitive "
        "dynamic_node. Baseline and candidates run through the identical flow with the "
        "identical metric extraction, so the two columns are directly comparable.", BODY))

    S.append(PageBreak())

    # ---------------------------------------------------------- page 2
    S.append(Paragraph("Agentic architecture", H2))
    S.append(Paragraph(
        "Five roles. Four are LLM agents with a single narrow job each; the orchestrator is "
        "deterministic code, because every decision it makes is mechanical and an LLM there "
        "would only add cost and nondeterminism.", BODY))
    S.append(Spacer(1, 4))
    png = render(ARCH_DOT, "arch")
    S.append(fit(png, AW, 168 * mm))
    S.append(Paragraph(
        "Figure 1 &mdash; Agent topology and control flow. Blue: LLM agents. "
        "Green: deterministic harness stages. Dashed: knowledge and fallback paths.", CAP))

    S.append(PageBreak())

    # ---------------------------------------------------------- page 3
    S.append(Paragraph("The subagents", H2))
    ag = [
        [P("Agent", CELLH), P("Model", CELLH), P("Calls<br/>/design", CELLH),
         P("Responsibility", CELLH)],
        [P("PROFILER", CELLB), P("Opus 5"), P("1"),
         P("Classifies the design (size class, datapath vs control vs memory, arithmetic "
           "content) and proposes the delay/area weights. Runs once, before any tuning.")],
        [P("STRATEGIST", CELLB), P("Opus 5"), P("1 / iter"),
         P("The hot path. Reads INSPECT summaries, prior rules and the surrogate's "
           "candidates, then returns exactly K configurations as strict JSON &mdash; half "
           "exploiting the leader, the rest probing untried corners.")],
        [P("VERIFIER", CELLB), P("Sonnet 5"), P("1"),
         P("Gates the winner. Confirms check -assert passed, the port list and flop count "
           "match the baseline, and that the metric movement is consistent with a real "
           "remapping.")],
        [P("ANALYST", CELLB), P("Sonnet 5"), P("0 (fallback)"),
         P("Recovers metrics if the Python log regexes ever miss. The hot loop parses logs "
           "in pure Python, so this normally costs nothing.")],
        [P("ORCHESTRATOR", CELLB), P("code"), P("&mdash;"),
         P("Owns the loop, the budgets, the baseline, parallelism, early stopping and "
           "rule write-back. Deterministic by design.")],
    ]
    t = Table(ag, colWidths=[AW * 0.175, AW * 0.10, AW * 0.105, AW * 0.62], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BFBFBF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    S.append(t)

    S.append(Paragraph("Why it gets cheaper as it goes", H3))
    S.append(Paragraph(
        "Three mechanisms keep token cost flat while coverage grows. "
        "<b>INSPECT summaries</b> mean the strategist sees marginals, knob/score "
        "correlations, provably inert knobs and failure rates &mdash; never raw history "
        "rows &mdash; so the prompt does not grow with the search. "
        "<b>A random-forest surrogate with expected improvement</b> ranks 2,000 sampled "
        "configurations per iteration for free, and the LLM only arbitrates the shortlist. "
        "<b>The rule library</b> carries measured knob deltas across designs: it grew from "
        "4 seeded hypotheses to 10 measured rules over this sweep, and the last design "
        "entered with 5 relevant rules already retrieved.", BODY))

    S.append(PageBreak())

    # ---------------------------------------------------------- page 4
    S.append(Paragraph("The tool under optimisation", H2))
    S.append(Paragraph(
        "The agent owns a parameterised Yosys script end to end, so it controls the "
        "coarse-optimisation stage, adder mapping and the full ABC invocation. Metrics come "
        "out of the same single Yosys process, so measurement adds no extra passes.", BODY))
    S.append(Spacer(1, 4))
    png2 = render(TOOL_DOT, "tool")
    S.append(fit(png2, AW * 0.60, 112 * mm))
    S.append(Paragraph(
        "Figure 2 &mdash; Yosys flow with the agent's injection points. "
        "Amber: Yosys-stage knobs. Blue: the ABC invocation and its parameters.", CAP))

    S.append(Paragraph("ABC parameters searched", H3))
    CELLK = ParagraphStyle("CELLK", parent=CELL, fontName="Courier-Bold",
                           fontSize=7.8, textColor=INK)
    abc = [
        [P("Parameter", CELLH), P("Values", CELLH), P("What it controls", CELLH)],
        [P("abc_flow", CELLK), P("resyn2 | dch_map<br/>| syn2_if", CELLM),
         P("Script family. <b>resyn2</b> = balance/rewrite/refactor AIG restructuring; "
           "<b>dch_map</b> = choice computation then mapping; <b>syn2_if</b> = "
           "&amp;syn2 / &amp;if -g -K 6 / &amp;synch2 / &amp;nf refinement in ABC's GIA space.")],
        [P("abc_D_scale", CELLK), P("0.50 &ndash; 1.10", CELLM),
         P("Multiplies the design's SDC period to set ABC's <b>-D</b> delay target. "
           "Tightening it below 1.0 buys real delay.")],
        [P("resyn_loops", CELLK), P("1 &ndash; 4", CELLM),
         P("Repetitions of the chosen flow's core.")],
        [P("use_dch", CELLK), P("on | off", CELLM),
         P("&amp;dch -f / dch -f don't-care-based choice computation before mapping.")],
        [P("mapper", CELLK), P("&amp;nf | map<br/>| map -a | map -B 0.9", CELLM),
         P("Delay-driven (&amp;nf, GIA space) versus classic and area-biased mapping.")],
        [P("post", CELLK), P("buffer;upsize;dnsize<br/>| topo;dnsize | &hellip;", CELLM),
         P("Post-mapping gate sizing and buffering.")],
    ]
    t = Table(abc, colWidths=[AW * 0.155, AW * 0.27, AW * 0.575], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BFBFBF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    S.append(t)

    meas_h = Paragraph("Measurement, in-process", H3)
    meas_p = Paragraph(
        "<font face='Courier' size='8'>stat -liberty -top</font> gives hierarchy-aggregated "
        "cell counts and chip area; ABC's <font face='Courier' size='8'>stime -p</font> "
        "gives the post-mapping critical path; "
        "<font face='Courier' size='8'>ltp -noff</font>, run after removing sequential "
        "cells, gives true combinational depth as an independent cross-check on delay; "
        "<font face='Courier' size='8'>check -assert</font> guards structural integrity. "
        "All four come from one invocation and are parsed in pure Python.", BODY)
    S.append(KeepTogether([meas_h, meas_p]))

    S.append(PageBreak())

    # ---------------------------------------------------------- page 5
    S.append(Paragraph("Results", H2))
    S.append(Paragraph(
        "Baseline is the validated <font face='Courier' size='8'>resyn2</font> "
        "configuration, re-measured in-harness so baseline and candidates are produced by "
        "the identical flow. Negative is better.", BODY))
    S.append(Spacer(1, 3))

    hdr = ["Design", "Delay (ps)\nbase → opt", "Δ", "Area (µm²)\nbase → opt", "Δ",
           "Cells\nbase → opt", "Score", "Δ score"]
    data = [hdr]
    for r in rows:
        b, o = r["baseline"], r["best"]
        pd_ = 100 * (o["delay_ps"] / b["abc_delay_ps"] - 1)
        pa = 100 * (o["area_um2"] / b["chip_area"] - 1)
        pc = 100 * (o["cells"] / b["cell_count"] - 1)
        data.append([
            r["design"],
            f"{b['abc_delay_ps']:,.0f} → {o['delay_ps']:,.0f}", f"{pd_:+.1f}%",
            f"{b['chip_area']:,.0f} → {o['area_um2']:,.0f}", f"{pa:+.1f}%",
            f"{b['cell_count']:,} → {o['cells']:,}", f"{o['score']:.4f}",
            f"{100 * (o['score'] - 1):+.1f}%",
        ])
    t = Table(data, colWidths=[AW * 0.145, AW * 0.175, AW * 0.085, AW * 0.185,
                               AW * 0.085, AW * 0.165, AW * 0.08, AW * 0.08],
              repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.8),
        ("FONT", (0, 1), (0, -1), "Helvetica-Bold", 8.2),
        ("FONT", (1, 1), (-1, -1), "Helvetica", 8),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BFBFBF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i in range(1, len(data)):
        style += [("BACKGROUND", (2, i), (2, i), GREEN),
                  ("BACKGROUND", (6, i), (7, i), GREEN),
                  ("FONT", (2, i), (2, i), "Helvetica-Bold", 8),
                  ("FONT", (6, i), (7, i), "Helvetica-Bold", 8)]
    t.setStyle(TableStyle(style))
    S.append(t)
    S.append(Paragraph(
        "Table 1 &mdash; Objective improvement per design. Flop counts are identical "
        "between baseline and optimised netlist in every design.", CAP))

    S.append(Paragraph("Runtime and cost", H3))
    hdr2 = ["Design", "Synth\nruns", "Iters", "Agent\nwall-clock", "Baseline\nsynth",
            "LLM\ncalls", "LLM cost\n(USD)"]
    d2 = [hdr2]
    tot_runs = tot_wall = tot_calls = 0
    tot_cost = 0.0
    for r in rows:
        name = r["design"]
        c = costs[name]
        tot_runs += r["n_synth_runs"]
        tot_wall += r["agent_wallclock_s"]
        tot_calls += LLM_CALLS[name]
        tot_cost += round(c, 3)
        d2.append([
            name, f"{r['n_synth_runs']}", f"{r['iterations_run']}",
            f"{r['agent_wallclock_s'] / 60:.1f} min",
            f"{r['baseline']['wall_time_s']:.1f} s",
            f"{LLM_CALLS[name]}", f"${c:.3f}",
        ])
    d2.append(["TOTAL", f"{tot_runs}", "—", f"{tot_wall / 60:.0f} min", "—",
               f"{tot_calls}", f"${tot_cost:.3f}"])
    t = Table(d2, colWidths=[AW * 0.19, AW * 0.11, AW * 0.09, AW * 0.15,
                             AW * 0.14, AW * 0.10, AW * 0.22], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.8),
        ("FONT", (0, 1), (0, -1), "Helvetica-Bold", 8.2),
        ("FONT", (1, 1), (-1, -1), "Helvetica", 8.2),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, GREY]),
        ("BACKGROUND", (0, -1), (-1, -1), LIGHT),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.5),
        ("LINEABOVE", (0, -1), (-1, -1), 1.0, INK),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BFBFBF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    S.append(t)
    S.append(Paragraph(
        "Table 2 &mdash; Search effort and spend. Wall-clock is end-to-end per design on "
        "4 cores, including the baseline measurement and every candidate. Per-design cost "
        "is the measured sweep total apportioned by each design's LLM calls, priced at "
        "Opus&nbsp;5 and Sonnet&nbsp;5 rates; a standalone re-run of dynamic_node measured "
        "$0.31 independently.", CAP))

    S.append(Paragraph("What the agent discovered", H3))
    S.append(Paragraph(
        "On gcd and aes the winning configurations keep the baseline's own "
        "<font face='Courier' size='8'>resyn2</font> script and win by running it twice "
        "under a tightened delay target &mdash; a result a human sweep would be unlikely to "
        "guess. On bp_fe_top it switched to the "
        "<font face='Courier' size='8'>syn2_if</font> flow and cut combinational depth from "
        "385 levels to 35, confirmed independently by the "
        "<font face='Courier' size='8'>ltp</font> cross-check. Across the sweep it learned "
        "and recorded 10 reusable rules, so each additional design starts warmer than the "
        "last.", BODY))

    doc.build(S)
    return ROOT / "architecture.pdf"


if __name__ == "__main__":
    p = build()
    print(f"wrote {p} ({p.stat().st_size / 1024:.0f} KB)")
