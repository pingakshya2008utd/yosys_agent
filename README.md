# YOSYS-AGENT

A closed-loop, LLM-guided tuner for the **synthesis stage** of
OpenROAD-flow-scripts. It proposes batches of Yosys/ABC configurations,
runs them in parallel, scores them against a baseline re-measured on the
same machine, and carries a persistent rule library so later designs
warm-start from earlier ones.

Implements the YOSYS-AGENT design plan (kept outside this repo): the ORFS-agent loop
(`RUN → READ → COLLATE → INSPECT → OPTIMIZE → AGGLOM → ALTER`, arXiv 2506.08332)
plus SymRTLO's dispatcher, goal-tagged rule library and verification gate
(arXiv 2504.10369). **RTL is never rewritten** — the only action space is the
flow configuration, which keeps equivalence checking structural and cheap.

---

## Results: resyn2 baseline vs. agent

Five nangate45 designs, 179 synthesis runs, 78.5 min wall-clock, **$1.34** of
API spend. Baseline is the validated `resyn2` configuration from
`results/synth_resyn2_summary.csv`, re-measured here (see
[The score](#the-score)).

| Design | Metric | resyn2 baseline | Agent best | % change |
|---|---|---:|---:|---:|
| **gcd** | delay (ps) | 644.27 | 325.19 | **−49.5%** |
| | area (µm²) | 460.18 | 471.09 | +2.4% |
| | cells | 362 | 330 | −8.8% |
| | logic depth | 12 | 14 | +16.7% |
| | flops | 35 | 35 | 0.0% |
| | **score** | **1.0000** | **0.6345** | **−36.6%** |
| **aes** | delay (ps) | 1,981.70 | 653.27 | **−67.0%** |
| | area (µm²) | 14,086.03 | 14,483.97 | +2.8% |
| | cells | 12,086 | 12,162 | +0.6% |
| | logic depth | 21 | 21 | 0.0% |
| | flops | 562 | 562 | 0.0% |
| | **score** | **1.0000** | **0.5043** | **−49.6%** |
| **jpeg** | delay (ps) | 9,312.47 | 1,129.88 | **−87.9%** |
| | area (µm²) | 91,928.80 | 92,000.89 | +0.1% |
| | cells | 74,546 | 65,521 | −12.1% |
| | logic depth | 35 | 31 | −11.4% |
| | flops | 4,376 | 4,376 | 0.0% |
| | **score** | **1.0000** | **0.3412** | **−65.9%** |
| **dynamic_node** | delay (ps) | 2,622.29 | 767.72 | **−70.7%** |
| | area (µm²) | 20,835.78 | 20,708.90 | −0.6% |
| | cells | 11,355 | 9,178 | −19.2% |
| | logic depth | 30 | 18 | −40.0% |
| | flops | 2,257 | 2,257 | 0.0% |
| | **score** | **1.0000** | **0.5732** | **−42.7%** |
| **bp_fe_top** | delay (ps) | 32,445.40 | 1,588.17 | **−95.1%** |
| | area (µm²) | 49,989.38 | 46,835.15 | −6.3% |
| | cells | 35,217 | 27,217 | −22.7% |
| | logic depth | 385 | 35 | −90.9% |
| | flops | 4,315 | 4,315 | 0.0% |
| | **score** | **1.0000** | **0.2709** | **−72.9%** |

Negative is better throughout. **Geometric-mean score 0.4424; worst case
0.6345 (gcd); 5/5 designs improved.** Flop counts are identical in every
design — the sequential-structure gate held throughout. Spreadsheet:
`results/yosys_agent_results.xlsx`.

Three things to know before quoting these numbers:

- **Synthesis only.** Nothing is placed or routed, and the delay metric is
  ABC's `stime -p` over liberty arcs at `WireLoad = "none"` — no SDC, no
  slack, no wire load. It ranks configs reliably within one design, but it is
  not a signoff number and will not translate 1:1 post-route.
- **`bp_fe_top`'s −95.1% reflects a bad baseline, not a 20× speedup.** resyn2
  leaves that design ~20× over its 1.62 ns clock (logic depth 385); the agent
  found a flow that fixes it. Against a well-tuned baseline the margin would
  be much smaller.
- **`jpeg` is leeway-limited.** 17 of its 21 runs were rejected, 15 for area
  > 5%, so it kept an early preset and never really explored. 0.3412 is real
  but not its ceiling.

### The score

```
score = w_delay × (delay / delay_base) + w_area × (area / area_base)
```

Lower is better. The baseline substitutes into its own formula as
`w_delay + w_area = 1.0000`, so it scores exactly 1.000 by construction and
the agent column reads directly as a fraction of baseline cost.

Weights are per-design, proposed by the `profiler` agent from the design's
size, style and clock:

| Design | w_delay | w_area |
|---|---:|---:|
| gcd | 0.75 | 0.25 |
| aes | 0.75 | 0.25 |
| jpeg | 0.75 | 0.25 |
| dynamic_node | 0.75 | 0.25 |
| bp_fe_top | 0.75 | 0.25 |

Baseline and candidates run through the same `harness/synth_templated.tcl`
with the same metric extraction, differing only in the knob-driven variables,
so the two columns are directly comparable. Each run's exact environment is
saved to `work/<design>/**/env.json`.

The baseline is the resyn2 *configuration* re-measured here, not a replay of
`results/synth_resyn2_summary.csv` — the harness TCL adds `opt -purge`, a
post-`dfflibmap` `opt`, and `splitnets`. gcd reproduces the CSV exactly; the
other four are within 0.5% on area. Ratios are unaffected because both sides
share the flow. `make_xlsx.py` writes the full comparison to its own sheet.

A config is rejected (score `+inf`) if it fails or times out, `check -assert`
fails, the port list or flop count changes, or a non-target metric degrades
past `--leeway`.

### Per-design search effort and winning flow

| Design | Runs | Iterations | Agent wall-clock | Winning `abc_flow` |
|---|---:|---:|---:|---|
| gcd | 41 | 5 | 102 s | `resyn2` |
| aes | 41 | 5 | 562 s | `resyn2` |
| jpeg | 23 | 2 | 1,924 s | `dch_map` |
| dynamic_node | 33 | 4 | 315 s | `dch_map` |
| bp_fe_top | 41 | 5 | 1,807 s | `syn2_if` |

Notably, gcd's and aes's winners both use the **`resyn2` flow itself** — the
agent's gain there comes from running the baseline's own script twice
(`resyn_loops: 2`) with a tightened delay target and extra coarse
optimisation, not from a different mapper.

---

## Quick start

```bash
cd yosys-agent
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
printf 'ANTHROPIC_API_KEY=sk-ant-...\n' > .env && chmod 600 .env   # gitignored

./.venv/bin/python run.py --design gcd --k 8 --iters 4
./.venv/bin/python run.py --designs gcd,dynamic_node,aes,bp_fe_top,jpeg \
    --baseline resyn2 --k 8 --iters 4 --patience 3 --leeway 0.05 --budget-s 2400
./.venv/bin/python run.py --report        # regenerate results/summary.md
./.venv/bin/python make_xlsx.py           # build the spreadsheet
./status.sh [-w]                          # progress of a running sweep
```

Key flags: `--baseline {resyn2,orfs}`, `--weights delay=1.0,area=0.0`,
`--leeway`, `--patience`, `--budget-s`, `--k`, `--iters`, `--no-llm`
(surrogate only, zero API calls), `--miter`, `--seed`.

---

## Designs requiring a SystemVerilog frontend

Of the 19 nangate45 designs, 5 set `SYNTH_HDL_FRONTEND = slang`: `ibex`,
`cva6`, `picorv32`, `tpu`, `mempool_group`. No `slang`, `sv2v` or Yosys plugin
is installed and `tools/yosys/` is an uninitialised submodule, so `preflight`
skips them with a recorded reason rather than failing mid-run. Yosys 0.33's
native parser rejects constructs they use, such as ibex's unpacked array ports
(`input logic [31:0] imd_val_q_i[2]`).

---

## References

### The papers this flow draws on

1. **ORFS-agent: Tool-Using Agents for Chip Design Optimization.**
   arXiv:2506.08332.
   Source of the iteration loop (`RUN → READ → COLLATE → INSPECT → OPTIMIZE →
   AGGLOM → ALTER`), the typed INSPECT/OPTIMIZE/AGGLOM tool trio so the LLM
   reasons over summaries rather than raw rows, the normalised objective with
   natural-language "leeway" constraints, and the cheap in-loop metric so the
   search is not gated on an expensive evaluator.

2. **SymRTLO: Enhancing RTL Code Optimization with LLMs and Neuron-Inspired
   Symbolic Reasoning.** arXiv:2504.10369.
   Source of the LLM **dispatcher** that profiles a design before any tuning
   (`agents/profiler.md`), the goal-tagged **rule library** retrieved per
   design with an elbow cutoff (`knowledge/rules.json`), the **verification
   gate** before a candidate is accepted (`agents/verifier.md`), and the
   feedback loop that re-selects strategy on failure. Its RTL-rewriting half
   is deliberately **not** used here — the designs are fixed inputs, so the
   action space is purely the flow configuration.

3. S. Ren, Z. Cheng, Y. Han, L. Shen, L. Jin, W. Tian, R. Fu, C. Wang, B. Yu,
   T.-Y. Ho. **AgenticPD: A Stage-Aware Agentic Framework for Closed-Loop
   Physical Design Optimization.** arXiv:2607.04758
   (<https://arxiv.org/abs/2607.04758>).
   Closest related work, and the clearest statement of the idea this harness
   leans on hardest: scope the search to a **single flow stage** so a trial
   does not re-run everything downstream. AgenticPD does that across the
   physical-design stages with a judge agent plus stage-specialised agents;
   this project applies the same reasoning to the synthesis stage alone,
   which is what makes a ~2 s–4 min trial affordable where full P&R would be
   hours. Its agent harness — structured observations, execution history and
   explicit context management rather than raw logs in the prompt — is the
   same discipline as the INSPECT tool trio here.

### Tools driven

4. C. Wolf, J. Glaser, J. Kepler. **Yosys — A Free Verilog Synthesis Suite.**
   Austrochip, 2013.
5. R. Brayton, A. Mishchenko. **ABC: An Academic Industrial-Strength
   Verification Tool.** CAV, 2010.
6. T. Ajayi et al. **Toward an Open-Source Digital Flow: First Learnings from
   the OpenROAD Project.** DAC, 2019.

### Logic-synthesis algorithms behind the searched knobs

7. A. Mishchenko, S. Chatterjee, R. Brayton. **DAG-Aware AIG Rewriting: A
   Fresh Look at Combinational Logic Synthesis.** DAC, 2006.
   The `rewrite` / `refactor` / `balance` primitives that `resyn2` is built
   from, and the reason `resyn2` must be expanded by hand here.
8. A. Mishchenko, S. Cho, S. Chatterjee, R. Brayton. **Combinational and
   Sequential Mapping with Priority Cuts.** ICCAD, 2007.
   Behind the `&nf` / `&if` mappers used by the `dch_map` and `syn2_if` flows.
9. A. Mishchenko, R. Brayton, J.-H. R. Jiang, S. Jang. **Scalable
   Don't-Care-Based Logic Optimization and Resynthesis.** FPGA, 2009.
   Behind `dch` / `&dch -f`, the `use_dch` knob.

### Search strategy in the harness

10. D. R. Jones, M. Schonlau, W. J. Welch. **Efficient Global Optimization of
    Expensive Black-Box Functions.** Journal of Global Optimization, 1998.
    The Expected-Improvement acquisition in `harness/tools.py`.
11. F. Hutter, H. Hoos, K. Leyton-Brown. **Sequential Model-Based Optimization
    for General Algorithm Configuration.** LION, 2011.
    The random-forest surrogate over a discrete/mixed configuration space.
12. M. D. McKay, R. J. Beckman, W. J. Conover. **A Comparison of Three Methods
    for Selecting Values of Input Variables in the Analysis of Output from a
    Computer Code.** Technometrics, 1979.
    Latin hypercube sampling, used for the warm-start batch and the candidate
    pool.
