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

## Action space (9 dimensions)

| Knob | Range | Effect |
|---|---|---|
| `abc_flow` | `resyn2`, `dch_map`, `syn2_if` | script family: the validated resyn2 body; a dch/map core; or an ORFS-`abc_speed`-style `&syn2`/`&if -g -K 6`/`&synch2`/`&nf` loop |
| `abc_D_scale` | 0.50–1.10 | multiplies the SDC period for ABC's `-D` delay target |
| `resyn_loops` | 1–4 | repetitions of the flow's core |
| `use_dch` | bool | `&dch -f` / `dch -f` choice computation; strong for delay, slow |
| `mapper` | `&nf`, `map`, `map -a`, `map -B 0.9` | delay- vs area-driven mapping (ignored by `resyn2`/`syn2_if`) |
| `post` | 4 choices | post-map sizing (`buffer;upsize;dnsize`, `topo;dnsize`, …) |
| `extra_opt` | 5 choices | coarse-stage Yosys opt (`share`, `alumacc`, …) |
| `adder_map` | bool | `extract_fa` + map to the platform's adder cells |
| `flatten` | bool | `synth -flatten` |

---

## Metrics

All four come from one Yosys invocation, so measurement costs no extra passes:

| Field | Source |
|---|---|
| `chip_area` | `stat -liberty -top` → `Chip area for top module` (µm²) |
| `cell_count`, `seq_cells` | `stat -liberty -top` table |
| `abc_delay_ps` | ABC `stime -p` → `Delay = <x> ps` |
| `logic_depth` | `ltp -noff` **after deleting all sequential cells** |

Parsing is pure Python (`harness/metrics.py`) — zero tokens in the hot loop.
The `analyst` agent is only invoked when those regexes come up empty.

### Two metric traps

**`yosys sta` is unusable here.** The pass exists in Yosys 0.33 but aborts with
``Module `INV_X1' is used with parameters but is not parametric!`` on any
liberty-mapped netlist. ABC's `stime -p` is therefore the sole delay source,
and there is no second timing engine in the loop. It reports a max arrival over
liberty arcs at `WireLoad = "none"`: no SDC, no clocks, no slack, no wire load,
single corner. It ranks configs against each other on one design reliably —
which is what the optimizer needs — but it is **not a signoff number**.

**`ltp -noff` does not do what its name suggests on a mapped netlist.** It
recognises only Yosys-internal `$_DFF_*` cells, not library flops like
`DFF_X1`, so it walks straight *through* the flops, hits sequential loops,
breaks them arbitrarily and reports a meaningless length — while emitting
hundreds of thousands of `Detected loop` warnings. True combinational depth
requires deleting the sequentials first:

```tcl
delete t:DFF* t:SDFF* t:DLH* t:DLL* t:TLAT* t:fakeram45_*
ltp -noff
```

Both passes run; `logic_depth` is the second, and serves as the independent
cross-check on `abc_delay_ps`.

---

## Step 0 findings (why this does not drive ORFS directly)

The plan says to discover the knob surface rather than trust its priors. On
this machine, discovery found:

1. **ORFS's `scripts/synth.tcl` cannot run on the installed Yosys.** It calls
   `abc_new`, absent from Yosys 0.33. The plan's "Action Space A" (driving
   `make synth`) is not viable; `harness/synth_templated.tcl` owns the flow.
2. **`ABC_CLOCK_PERIOD_IN_PS` does not exist** in this ORFS checkout. The
   equivalent lever survives as `abc_D_scale` → `abc -D <ps>`.
3. **`abc` has no `-dont_use` in Yosys 0.33**, so the platform's
   `DONT_USE_CELLS` (`AOI211_X1`, `OAI211_X1`, …) cannot be excluded. Affects
   baseline and candidates equally, so ratios hold, but absolute areas differ
   from an ORFS `make synth`.
4. **Yosys invokes `yosys-abc -s`, which skips `abc.rc`, so no ABC aliases
   exist at all** — not just `resyn2`, but `b`, `rw`, `rf`, `rwz`, `rfz` too.
   Every script is written in primitives:
   `balance; rewrite; refactor; balance; rewrite; rewrite -z; balance; refactor -z; rewrite -z; balance`.
5. `&nf` is a GIA-space mapper and must be wrapped in `&get -n` / `&put`;
   `map` variants must not be.
6. In a `+`-prefixed ABC script string Yosys replaces commas with blanks, so
   the rendered string uses commas, never spaces.
7. **`dch` requires a strashed network.** With a non-`&` mapper and
   `resyn_loops > 1`, a naive loop emits `dch -f; map; dch -f; map` and ABC
   aborts — `map` leaves a mapped netlist. Every iteration re-strashes.
8. **`stat` must be given `-top`.** Without it a non-flattened run reports only
   the top module's own cells and area. The aggregate lives in a
   `=== design hierarchy ===` block labelled `Chip area for top module`.
9. **Never count flops by scanning netlist text.** A submodule instantiated
   twice has its flops written once, so a hierarchical gcd netlist shows 18
   flop instances for a 34-flop design. The gate uses `seq_cells` from
   `stat -top`.

---

## Deviations from the plan

- **No `temperature`.** The plan asks for temperature 0.1 with top-k/nucleus
  disabled. Sampling parameters are **removed** on Claude Opus 5 and Sonnet 5
  and return HTTP 400. Determinism comes from a strict
  `output_config.format` JSON schema plus a fixed `effort`. LLM proposals are
  therefore not bit-reproducible; the surrogate and LHS paths are seeded.
- **Retiming pinned off** (`knobs.RETIME = False`). `dretime` changes the
  sequential boundary, so flop counts legitimately differ and the cheap gate
  cannot verify it — the plan's §7 defensible call.
- **SAT miter off by default** (`--miter`). It does not converge on these
  liberty-mapped netlists: every output returns *unproven*. **Unproven ≠
  disproved** — no counterexample was found, the solver simply did not close.
  The code reports `proven` / `disproved` / `inconclusive`, and only
  `disproved` rejects a config. Structural checks always run, which is exactly
  what the `accept_unverified` verdicts reflect.
- **Orchestrator is code, not an LLM.** Every decision it makes is mechanical.
  `agents/orchestrator.md` records the contract it implements.

---

## Caveats on the results above

- **`bp_fe_top`'s ×0.049 is real but reflects a bad baseline, not a 20×
  speedup.** A 32,445 ps baseline against a 1.62 ns clock means resyn2 misses
  timing by ~20× on this design. It is corroborated independently — logic depth
  collapses 385 → 35 — so the measurement is sound, but the honest reading is
  that resyn2 produces a pathologically deep netlist here (flagged as an
  outlier in the original sweep too) and the agent found a flow that fixes it.
  Against a well-tuned baseline the margin would be far smaller.
- **`jpeg` is leeway-limited, not fully optimised.** It kept its iteration-0
  preset; 17 of 21 runs were rejected, 15 for area > 5%. Iteration 1 rejected
  all 8, collapsing K from 8 → 4 → 1 via the all-fail recovery path. 0.3412 is
  a real score, but the search never explored. A ~15% leeway would likely do
  better, since the objective already penalises area.
- **`gcd` is the one estimator disagreement.** Delay ratio 0.505 while depth
  ratio is 1.167 — depth got *worse* (12 → 14) as delay improved. Plausible as
  genuine cell-sizing gain at that size, but it is the single row where the two
  estimators point opposite ways. `results/summary.md` flags it.
- **gcd and aes were still improving when the budget ran out** (both found
  their best on the final iteration; gcd jumped 0.8669 → 0.6345). Raising
  `--iters` would likely help those two. `dynamic_node` converged in
  iteration 1 and stopped early on patience.
- **Synthesis only.** Nothing is placed or routed; these gains need not
  survive P&R. The clock period is treated as a spec, not a knob.

---

## Layout

```
run.py                       orchestrator (non-LLM glue)
make_xlsx.py                 builds results/yosys_agent_results.xlsx
status.sh                    progress of a running sweep
agents/*.md                  agent system prompts, loaded by run.py
harness/knobs.py             schema, presets, ABC script rendering
harness/synth_templated.tcl  the parameterised Yosys flow
harness/runner.py            ORFS introspection + parallel launcher
harness/metrics.py           log parsing (pure Python)
harness/tools.py             INSPECT / OPTIMIZE / AGGLOM + rule retrieval
harness/score.py             normalisation, leeway, ranking
harness/llm.py               Anthropic client, strict JSON, token accounting
knowledge/rules.json         cross-design rule library (4 seeds → 10 learned)
knowledge/history/*.jsonl    every run ever
results/<design>/            best_config.json, trajectory.csv
results/summary.md           report
results/yosys_agent_results.xlsx   baseline-vs-optimized spreadsheet
work/<design>/               per-run dirs: env.json, config.json, log, netlist
```

## Budget tiers

`K` is capped by core count (Yosys is single-threaded); the `large` tier is
additionally capped at `free_GB / 24`. Tiers run smallest-first so
`rules.json` is populated before the expensive designs — the main transfer
win, and it worked here: the library grew from 4 seeds to 10 rules, and jpeg
retrieved 5 learned rules at entry.

Per-run timeout is `max(90 s, 8 × baseline_synth_time)`, capped by tier. A
small multiple of the baseline would silently kill the delay-targeted arms,
which are legitimately 3–4.5× slower by design.

## Designs requiring a SystemVerilog frontend

Of the 19 nangate45 designs, 5 set `SYNTH_HDL_FRONTEND = slang`: `ibex`,
`cva6`, `picorv32`, `tpu`, `mempool_group`. No `slang`, `sv2v` or Yosys plugin
is installed and `tools/yosys/` is an uninitialised submodule, so `preflight`
skips them with a recorded reason rather than failing mid-run. Yosys 0.33's
native parser rejects constructs they use, such as ibex's unpacked array ports
(`input logic [31:0] imd_val_q_i[2]`).
