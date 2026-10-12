#!/usr/bin/env bash
# Repeatable FreeSWITCH resource + ESL-latency baseline (no Datadog components involved).
#
#   lab/scripts/run-baseline.sh [--reps N] [--scenarios "idle one five ten"] [--warmup S] [--window S]
#                           [--queries Q] [--rate R] [--out-dir DIR] [--label TEXT]
#
# Uses the bounded call harness (lab/scripts/run-call-load.sh); its safety limits are not changed.
# Results: <out-dir>/<UTC timestamp>-<id>/{env.json,raw.jsonl,phases/,summary.json,report.md}. Nothing is overwritten.
set -uo pipefail
cd "$(dirname "$0")/../.."

REPS=3; SCEN="idle one five ten"; WARMUP=8; WINDOW=25; QUERIES=15; RATE=2; OUT=results/baseline; LABEL=""
die() { echo "ERROR: $*" >&2; exit 4; }
is_int() { [[ "$1" =~ ^[0-9]+$ ]]; }
is_num() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; }
usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --reps) REPS=${2:-}; shift 2 || die "--reps needs a value" ;;
    --scenarios) SCEN=${2:-}; shift 2 || die "--scenarios needs a value" ;;
    --warmup) WARMUP=${2:-}; shift 2 || die "--warmup needs a value" ;;
    --window) WINDOW=${2:-}; shift 2 || die "--window needs a value" ;;
    --queries) QUERIES=${2:-}; shift 2 || die "--queries needs a value" ;;
    --rate) RATE=${2:-}; shift 2 || die "--rate needs a value" ;;
    --out-dir) OUT=${2:-}; shift 2 || die "--out-dir needs a value" ;;
    --label) LABEL=${2:-}; shift 2 || die "--label needs a value" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
is_int "$REPS" && [ "$REPS" -ge 1 ] && [ "$REPS" -le 10 ] || die "--reps must be 1..10"
is_int "$WARMUP" && [ "$WARMUP" -le 60 ] || die "--warmup must be an integer 0..60"
is_int "$WINDOW" && [ "$WINDOW" -ge 5 ] && [ "$WINDOW" -le 120 ] || die "--window must be an integer 5..120"
is_int "$QUERIES" && [ "$QUERIES" -ge 5 ] && [ "$QUERIES" -le 100 ] || die "--queries must be 5..100"
is_num "$RATE" && awk "BEGIN{exit !($RATE>=0.2 && $RATE<=5)}" || die "--rate must be 0.2..5 queries/second"
[[ "$LABEL" =~ ^[A-Za-z0-9._\ -]{0,60}$ ]] || die "--label may only contain letters, digits, space . _ -"
for s in $SCEN; do case "$s" in idle|one|five|ten) ;; *) die "unknown scenario '$s' (idle one five ten)";; esac; done

# Call lifetime must cover warm-up + the whole measurement; harness hold is capped at 60 s (unchanged).
HOLD_R=$(awk "BEGIN{h=4+$WARMUP+$WINDOW+8; print int(h)+(h>int(h))}")
HOLD_E=$(awk "BEGIN{h=4+$WARMUP+5*($QUERIES/$RATE+0.5)+8; print int(h)+(h>int(h))}")
[ "$HOLD_R" -le 60 ] || die "resource phase needs a ${HOLD_R}s call hold (> 60 s limit); lower --warmup/--window"
[ "$HOLD_E" -le 60 ] || die "ESL phase needs a ${HOLD_E}s call hold (> 60 s limit); lower --queries or raise --rate"

F() { docker compose exec -T freeswitch "$@"; }
CID=$(docker compose ps -q freeswitch)
[ -n "$CID" ] || die "lab is not running (lab/scripts/lab-up.sh)"
healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$CID" 2>/dev/null)" = healthy ]; }
healthy || die "FreeSWITCH container is not healthy"

# Never measure during startup: require >= 60 s of FreeSWITCH uptime.
for _ in $(seq 1 30); do
  up=$(F esl-check --command "uptime s" 2>/dev/null | tr -d '[:space:]')
  [[ "$up" =~ ^[0-9]+$ ]] && [ "$up" -ge 60 ] && break
  echo "waiting for FreeSWITCH uptime >= 60 s (now ${up:-?})"; sleep 5
done
[[ "${up:-}" =~ ^[0-9]+$ ]] && [ "$up" -ge 60 ] || die "FreeSWITCH uptime did not reach 60 s"
S=$(F esl-baseline settle --timeout 20) || die "could not query FreeSWITCH"
echo "$S" | grep -q '"returned_to_baseline":true' || die "channels are already active; wait for them to end first"

RUN_ID=$(openssl rand -hex 3)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DIR="$OUT/$STAMP-$RUN_ID"
mkdir -p "$OUT" && mkdir "$DIR" || die "cannot create $DIR (refusing to overwrite)"
mkdir "$DIR/phases"
STARTED=$(date -u +%FT%TZ)
echo "run $RUN_ID -> $DIR"

# ---- environment ----
host_cpu=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || (grep -m1 'model name' /proc/cpuinfo | cut -d: -f2) 2>/dev/null || echo unknown)
host_ncpu=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo unknown)
host_mem=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.1f GiB",$1/1073741824}')
[ -n "$host_mem" ] || host_mem=$(awk '/MemTotal/{printf "%.1f GiB",$2/1048576}' /proc/meminfo 2>/dev/null)
host_load=$(sysctl -n vm.loadavg 2>/dev/null || cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo unknown)
IMG_ID=$(docker inspect -f '{{.Image}}' "$CID" | cut -c1-19)
IMG_TAG=$(docker inspect -f '{{.Config.Image}}' "$CID")
GIT_REV=$(git rev-parse --short HEAD 2>/dev/null || echo none)
git diff --quiet HEAD 2>/dev/null || GIT_REV="$GIT_REV+dirty"
F esl-baseline env \
  --fact "host_os=$(uname -srm)" --fact "host_cpu=$host_cpu" --fact "host_cpus=$host_ncpu" --fact "host_memory=$host_mem" \
  --fact "host_loadavg_at_start=$host_load" \
  --fact "docker_server=$(docker info --format '{{.ServerVersion}} / {{.OperatingSystem}} / {{.Architecture}}')" \
  --fact "docker_vm_cpus=$(docker info --format '{{.NCPU}}')" \
  --fact "docker_vm_memory=$(docker info --format '{{.MemTotal}}' | awk '{printf "%.1f GiB",$1/1073741824}')" \
  --fact "compose_version=$(docker compose version --short)" \
  --fact "image=$IMG_TAG ($IMG_ID)" --fact "container_status=$(docker inspect -f '{{.State.Status}} since {{.State.StartedAt}}' "$CID")" \
  --fact "repo_revision=$GIT_REV" --fact "label=$LABEL" > "$DIR/env.json" || die "cannot collect environment"
cp "$DIR/env.json" "$DIR/raw.jsonl"

LOAD_PID=""
ABORTED=0
cleanup() {
  [ -n "$LOAD_PID" ] && kill -TERM "$LOAD_PID" 2>/dev/null && wait "$LOAD_PID" 2>/dev/null   # harness traps and cleans its own calls
  LOAD_PID=""
}
finish() {
  local rc=$1
  cleanup
  FINISHED=$(date -u +%FT%TZ)
  META=$(printf '{"run_id":"%s","started":"%s","finished":"%s","reps":"%s","scenarios":"%s","warmup":"%s","window":"%s","interval":"1","queries":"%s","rate":"%s","hold_resources":"%s","hold_esl":"%s"}' \
    "$RUN_ID" "$STARTED" "$FINISHED" "$REPS" "$SCEN" "$WARMUP" "$WINDOW" "$QUERIES" "$RATE" "$HOLD_R" "$HOLD_E")
  printf '%s\n' "$META" > "$DIR/meta.json"
  F esl-baseline report --run-meta "$META" < "$DIR/raw.jsonl" > "$DIR/report.md" 2>/dev/null
  F esl-baseline report --json --run-meta "$META" < "$DIR/raw.jsonl" > "$DIR/summary.json" 2>/dev/null
  echo "report:  $DIR/report.md"
  echo "summary: $DIR/summary.json"
  exit "$rc"
}
trap 'echo "interrupted: cleaning up" >&2; ABORTED=1; finish 130' INT
trap 'echo "terminated: cleaning up" >&2; ABORTED=1; finish 143' TERM

calls_for() { case "$1" in idle) echo 0;; one) echo 1;; five) echo 5;; ten) echo 10;; esac; }

run_phase() {  # scenario rep kind(resources|esl)
  local sc=$1 rep=$2 kind=$3 n hold base load_args=() pr lr sr
  n=$(calls_for "$sc")
  base="$DIR/phases/rep${rep}-${sc}-${kind}"
  if [ "$kind" = resources ]; then hold=$HOLD_R; else hold=$HOLD_E; fi
  healthy || { echo "FAIL: FreeSWITCH unhealthy before $sc rep $rep $kind" >&2; return 1; }

  LOAD_PID=""
  if [ "$n" -gt 0 ]; then
    case "$sc" in
      one)  load_args=(--scenario single) ;;
      five) load_args=(--scenario concurrent --calls 5) ;;
      ten)  load_args=(--scenario concurrent --calls 10 --allow-high) ;;
    esac
    lab/scripts/run-call-load.sh "${load_args[@]}" --hold "$hold" --ramp 5 --result-file "$base.load.json" \
      > "$base.load.log" 2>&1 &
    LOAD_PID=$!
  fi

  if [ "$kind" = resources ]; then
    F esl-baseline resources --scenario "$sc" --rep "$rep" --calls "$n" --warmup "$WARMUP" --window "$WINDOW" > "$base.json" 2> "$base.err"
  else
    F esl-baseline esl --scenario "$sc" --rep "$rep" --calls "$n" --warmup "$WARMUP" --queries "$QUERIES" --rate "$RATE" > "$base.json" 2> "$base.err"
  fi
  pr=$?

  lr=0
  if [ -n "$LOAD_PID" ]; then wait "$LOAD_PID"; lr=$?; LOAD_PID=""; fi
  F esl-baseline settle --timeout 30 > "$base.settle.json" 2>>"$base.err"; sr=$?

  if [ "$pr" != 0 ] || [ "$lr" != 0 ] || [ "$sr" != 0 ]; then
    echo "FAIL: $sc rep $rep $kind (phase=$pr harness=$lr settle=$sr); see $base.err / $base.load.log" >&2
    [ -s "$base.err" ] && sed 's/^/    /' "$base.err" >&2
    return 1
  fi
  local load_json=null
  [ -s "$base.load.json" ] && load_json=$(cat "$base.load.json")
  printf '{"phase":%s,"load":%s,"settle":%s}\n' "$(cat "$base.json")" "$load_json" "$(cat "$base.settle.json")" >> "$DIR/raw.jsonl"
  grep -q '"returned_to_baseline":true' "$base.settle.json" || { echo "FAIL: $sc rep $rep $kind did not return to baseline" >&2; return 1; }
  echo "ok: $sc rep $rep $kind"
  sleep 3
  return 0
}

for rep in $(seq 1 "$REPS"); do
  for sc in $SCEN; do
    for kind in resources esl; do
      run_phase "$sc" "$rep" "$kind" || { ABORTED=1; finish 1; }
    done
  done
done
finish 0
