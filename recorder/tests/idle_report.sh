#!/usr/bin/env bash
# M1: one-page record of idle CPU, RAM and temperature.
# usage: tests/idle_report.sh [minutes=10]   (run with the stack up but no load test running)
. "$(dirname "$0")/lib.sh"
MIN="${1:-10}"
STEP=10
samples=$(( MIN * 60 / STEP ))
csv="$(mktemp)"
echo "utc,cpu_pct,mem_used_mb,temp_c" > "$csv"

cpu_snapshot() { awk '/^cpu / {idle=$5+$6; total=0; for (i=2;i<=NF;i++) total+=$i; print idle, total}' /proc/stat; }
# CPU package temperature: the coretemp "Package id 0" sensor first, then the CPU thermal zones.
# acpitz is a fixed board sensor on many mini PCs (27 C on the N150 while the CPU was at 46 C),
# so it is only the last resort.
temp_c() {
  local h z want t=""
  for h in /sys/class/hwmon/hwmon*; do
    [[ "$(cat "$h/name" 2>/dev/null)" == coretemp ]] && t="$(cat "$h/temp1_input" 2>/dev/null)" && [[ -n "$t" ]] && break
  done
  if [[ -z "$t" ]]; then
    for want in x86_pkg_temp TCPU acpitz; do
      for z in /sys/class/thermal/thermal_zone*; do
        [[ "$(cat "$z/type" 2>/dev/null)" == "$want" ]] && t="$(cat "$z/temp" 2>/dev/null)" && [[ -n "$t" ]] && break 2
      done
    done
  fi
  [[ -n "$t" ]] && awk -v t="$t" 'BEGIN {printf "%.1f", t/1000}' || echo ""
}

echo "Sampling every ${STEP}s for ${MIN} min..."
read -r i0 t0 < <(cpu_snapshot)
for ((n = 0; n < samples; n++)); do
  sleep "$STEP"
  read -r i1 t1 < <(cpu_snapshot)
  cpu="$(awk -v di=$((i1 - i0)) -v dt=$((t1 - t0)) 'BEGIN {printf "%.1f", (1 - di/dt) * 100}')"
  mem="$(free -m | awk '/^Mem:/ {print $3}')"
  echo "$(date -u +%H:%M:%S),$cpu,$mem,$(temp_c)" >> "$csv"
  i0=$i1; t0=$t1
done

summary="$(python3 - "$csv" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
def col(k): return [float(r[k]) for r in rows if r[k]]
def line(label, k, unit):
    v = col(k)
    return f"| {label} | {sum(v)/len(v):.1f} {unit} | {max(v):.1f} {unit} |" if v else f"| {label} | n/a | n/a |"
print("| Metric | Average | Peak |\n|---|---|---|")
print(line("CPU busy (all cores)", "cpu_pct", "%"))
print(line("RAM used", "mem_used_mb", "MB"))
print(line("CPU temperature", "temp_c", "°C"))
PY
)"
echo "$summary"
mkdir -p "$REPORTS"
file="$REPORTS/m1-idle-$(date -u +%Y%m%d-%H%M%S).md"
{
  echo "# M1 idle record"
  echo
  echo "$(hostname), $(lscpu | awk -F': *' '/Model name/ {print $2}'), $(free -h | awk '/^Mem:/ {print $2}') RAM, kernel $(uname -r)."
  echo "Stack running: $(docker ps --format '{{.Names}}' | tr '\n' ' '). Sampled every ${STEP}s for ${MIN} min, ending $(date -u '+%Y-%m-%d %H:%M UTC')."
  echo
  echo "$summary"
  echo
  echo "Raw samples: $(basename "$file" .md).csv"
} > "$file"
cp "$csv" "${file%.md}.csv"; rm -f "$csv"
echo "report: $file"
