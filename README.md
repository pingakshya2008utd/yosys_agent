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

### Per-design search effort

| Design | Runs | Iterations | Agent wall-clock |
|---|---:|---:|---:|
| gcd | 41 | 5 | 102 s |
| aes | 41 | 5 | 562 s |
| jpeg | 23 | 2 | 1,924 s |
| dynamic_node | 33 | 4 | 315 s |
| bp_fe_top | 41 | 5 | 1,807 s |

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

## References

### The papers this flow draws on

1. Amur Ghose, Andrew B. Kahng, Sayak Kundu, Zhiang Wang.
   **ORFS-agent: Tool-Using Agents for Chip Design Optimization.**
   arXiv:2506.08332.

2. Yiting Wang, Wanghao Ye, Ping Guo, Yexiao He, Ziyao Wang, Bowei Tian,
   Shwai He, Guoheng Sun, Zheyu Shen, Sihan Chen, Ankur Srivastava,
   Qingfu Zhang, Gang Qu, Ang Li.
   **SymRTLO: Enhancing RTL Code Optimization with LLMs and Neuron-Inspired
   Symbolic Reasoning.** arXiv:2504.10369.

3. Shuo Ren, Zijin Cheng, Yaohui Han, Libo Shen, Leilei Jin, Wanting Tian,
   Rongliang Fu, Chao Wang, Bei Yu, Tsung-Yi Ho.
   **AgenticPD: A Stage-Aware Agentic Framework for Closed-Loop Physical
   Design Optimization.** arXiv:2607.04758.

### Tools driven

4. C. Wolf, J. Glaser, J. Kepler. **Yosys — A Free Verilog Synthesis Suite.**
   Austrochip, 2013.
5. R. Brayton, A. Mishchenko. **ABC: An Academic Industrial-Strength
   Verification Tool.** CAV, 2010.
6. T. Ajayi et al. **Toward an Open-Source Digital Flow: First Learnings from
   the OpenROAD Project.** DAC, 2019.
