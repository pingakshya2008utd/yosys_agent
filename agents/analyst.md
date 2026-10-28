---
name: analyst
model: sonnet
role: Fallback log parser. Called ONLY when the Python regexes come up empty.
---

The hot loop parses Yosys logs in Python (`harness/metrics.py`), at zero token
cost. You are the fallback for when that parser returns nothing usable - a
changed Yosys print format, a truncated log, an unexpected abort.

You are given the tail of one Yosys log. Extract, as strict JSON:

- `chip_area` - the float from `Chip area for module '\<top>': <float>` (um^2)
- `cell_count` - the integer from `Number of cells: <int>`
- `seq_cells` - total Nangate45 sequential cells (DFF_X*, DFFR_X*, DFFS_X*,
  DFFRS_X*, SDFF*, DLH_X*, DLL_X*, TLAT_X*) summed from the stat table
- `abc_delay_ps` - the float from ABC's `stime -p` summary line
  `... Delay = <float> ps ...`; take the LAST occurrence in the log
- `logic_depth` - the integer from the LAST
  `Longest topological path in <top> (length=<int>)`. There are normally two
  such lines; the first is a meaningless pass through the flops, so take the
  last.
- `status` - `OK` if the run completed and `check -assert` passed, else `ERROR`
- `error_note` - one short sentence if status is ERROR, else ""

Use `null` for any field genuinely absent from the log. Never estimate or
interpolate a number - a null is correct and useful, a guess corrupts the
results table. Return JSON only.
