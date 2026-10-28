---
name: orchestrator
model: opus
role: Main loop controller. Implemented as code in run.py; this file is the contract.
---

The orchestrator is the only component that holds the full picture. It is
implemented as deterministic Python in `run.py` rather than as an LLM agent,
because every decision it makes is mechanical and an LLM in this position
would only add cost and nondeterminism. This file records the contract it
implements, so the behaviour is reviewable in one place.

Per design:

1. Load `knowledge/rules.json`.
2. Build the design spec from ORFS (`make`-expanded VERILOG_FILES, include
   dirs, ADDITIONAL_LIBS, SDC period). Preflight it; skip with a recorded
   reason if it needs a SystemVerilog frontend that is not installed.
3. Call `profiler` once. Retrieve at most 5 rules by field match + elbow cut.
4. Measure the BASELINE: stock ORFS ABC script for that design, on this
   machine, in this session. Never quote a baseline from the repo. If the
   baseline fails, abort the design - there is nothing to normalise against.
5. Iteration 0: the warm-start batch - rule-matched configs plus named presets
   plus LHS fill.
6. Iterations 1..N: RUN -> READ -> COLLATE -> INSPECT -> OPTIMIZE -> AGGLOM ->
   LLM GATE (`strategist`) -> ALTER -> VERIFY.
7. Write `results/<design>/best_config.json` and `trajectory.csv`.
8. Append at most 2 rules to `knowledge/rules.json`: the winning knob delta,
   and the clearest negative result.

Hard rules:

- At most 2 LLM calls per iteration.
- Never put more than 30 history rows in a prompt; pass INSPECT summaries
  instead.
- If every run in a batch fails: halve K, relax `abc_D_scale` toward 1.0, and
  retry once before aborting the design.
- Cap total Yosys invocations per design at `K * (N + 1) * 1.25`; abort with a
  recorded reason rather than overrunning.
- Stop on: iteration budget reached, no improvement for 2 consecutive
  iterations, or the design wall-clock budget exhausted - whichever fires first.
- Every number in the final report must come from a file on disk.
- A failed config is recorded with its stderr tail, never silently dropped.
