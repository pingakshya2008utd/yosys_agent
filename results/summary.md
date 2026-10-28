# YOSYS-AGENT results

- Yosys: `Yosys 0.33 (git sha1 2584903a060)`
- ORFS commit: `64939f371`
- Platform: nangate45 (`NangateOpenCellLibrary_typical.lib`)

## Main table

| design | delay_base (ps) | delay_opt | d% | area_base (um2) | area_opt | a% | score_base | score_opt | runs | agent_s | base_s | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| aes | 1981.70 | 653.27 | -67.0% | 14086.0 | 14484.0 | +2.8% | 1.000 | **0.5043** | 41 | 562 | 26.6 | reject |
| bp_fe_top | 32445.40 | 1588.17 | -95.1% | 49989.4 | 46835.2 | -6.3% | 1.000 | **0.2709** | 41 | 1807 | 81.5 | accept_unverified |
| dynamic_node | 2622.29 | 767.72 | -70.7% | 20835.8 | 20708.9 | -0.6% | 1.000 | **0.5732** | 33 | 315 | 15.2 | accept_unverified |
| gcd | 644.27 | 325.19 | -49.5% | 460.2 | 471.1 | +2.4% | 1.000 | **0.6345** | 41 | 102 | 1.2 | accept_unverified |
| jpeg | 9312.47 | 1129.88 | -87.9% | 91928.8 | 92000.9 | +0.1% | 1.000 | **0.3412** | 23 | 1924 | 137.8 | accept_unverified |

- Designs improved over baseline: **5/5** (regressed or tied: 0)
- Geometric-mean score: **0.4424**  (worst case: **0.6345** on `gcd`)

Per the reporting rules, the per-design worst case is quoted alongside the geometric mean, and designs that failed to beat baseline are kept in the table rather than dropped.

## Transfer

| design | iterations run | iterations to best | rules retrieved |
|---|---|---|---|
| aes | 5 | 5 | ['R-005', 'R-001', 'R-006'] |
| bp_fe_top | 5 | 4 | ['R-007', 'R-001'] |
| dynamic_node | 4 | 1 | ['R-002', 'R-001'] |
| gcd | 5 | 5 | ['R-001'] |
| jpeg | 2 | 1 | ['R-001', 'R-005', 'R-007', 'R-008', 'R-009'] |

## Estimator agreement

`abc_delay_ps` (ABC `stime -p`) is the delay objective. `logic_depth` is the independent cross-check.

| design | delay ratio | logic-depth ratio | agree? |
|---|---|---|---|
| aes | 0.330 | 1.000 | yes |
| bp_fe_top | 0.049 | 0.091 | yes |
| dynamic_node | 0.293 | 0.600 | yes |
| gcd | 0.505 | 1.167 | NO |
| jpeg | 0.121 | 0.886 | yes |

## Overhead

- LLM calls: 27
- Estimated LLM cost: **$1.3355**
- Tokens by model: `{"claude-opus-5": {"input": 148433, "output": 22463, "cache_read": 0}, "claude-sonnet-5": {"input": 9478, "output": 1280, "cache_read": 0}}`

## Limitations

- **Synthesis-only QoR.** Nothing here is placed or routed; post-synthesis
  gains need not survive P&R.
- **No slack, no SDC, no wire load.** The delay number is ABC's `stime -p`
  over liberty arcs at `WireLoad = "none"`. It is a *relative* ranking
  signal, valid for comparing configs on one design, and must never be
  reported as a timing signoff number. Yosys's own `sta` pass is unusable
  here (it aborts on liberty blackboxes in 0.33), so there is no second
  timing engine in the loop.
- **Retiming excluded.** `dretime` changes the sequential boundary, which
  the cheap flop-count gate cannot verify, so it is pinned off.
- **DONT_USE_CELLS not honoured.** `abc` in Yosys 0.33 has no `-dont_use`,
  so the platform's excluded cells (AOI211_X1, OAI211_X1, ...) can still be
  mapped. ORFS would exclude them, so absolute areas differ slightly from an
  ORFS `make synth` run.
- **No SDC period tuning.** The clock period is a spec, not a knob.
- **Determinism.** `temperature` is not settable on current Claude models
  (removed; a 400), so proposals are not bit-reproducible. The surrogate and
  LHS paths are seeded and reproducible.
