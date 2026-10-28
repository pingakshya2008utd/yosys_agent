#!/usr/bin/env bash
# Show the status of a yosys-agent sweep. Safe to run any time.
#   ./status.sh          one snapshot
#   ./status.sh -w       re-draw every 15s until the sweep exits
cd "$(dirname "$0")" || exit 1

snap() {
    pid=$(pgrep -f "run\.py --design" | head -1)
    echo "=============================================================="
    if [ -n "$pid" ]; then
        echo "STATUS   : RUNNING  (pid $pid, elapsed $(ps -o etime= -p "$pid" | tr -d ' '))"
        echo "yosys    : $(pgrep -c yosys 2>/dev/null || echo 0) process(es) active"
    else
        echo "STATUS   : not running"
    fi

    echo "finished : $(ls -d results/*/best_config.json 2>/dev/null \
        | sed 's|results/||;s|/best_config.json||' | paste -sd' ' -)"

    log=$(ls -t *.log 2>/dev/null | head -1)
    [ -n "$log" ] && echo "log      : $log"

    # scores so far, straight from the artifacts on disk
    if ls results/*/best_config.json >/dev/null 2>&1; then
        echo "--- best score per finished design ---"
        for f in results/*/best_config.json; do
            ./.venv/bin/python -c "
import json,sys
d=json.load(open('$f'))
b=d.get('best') or {}
s=b.get('score')
print(f\"  {d['design']:<14} {d.get('status','?'):<8} \"
      f\"score={s if s is not None else 'n/a'}  \"
      f\"delay_x={b.get('delay_ratio','-')}  area_x={b.get('area_ratio','-')}\")
" 2>/dev/null
        done
    fi

    # what the running design is doing right now
    if [ -n "$log" ]; then
        echo "--- current design / last activity ---"
        grep -E "^[a-z0-9_]+  \(tier=|--- iteration|^  best score" "$log" | tail -4
        tail -3 "$log" | sed 's/^/  /'
    fi
    echo "=============================================================="
}

if [ "${1:-}" = "-w" ]; then
    while pgrep -f "run\.py --design" >/dev/null; do
        clear; snap; sleep 15
    done
    clear; snap; echo "sweep has exited."
else
    snap
fi
