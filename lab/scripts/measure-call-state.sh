#!/usr/bin/env bash
# Independent FreeSWITCH state measurement, before / during / after a test.
#
#   lab/scripts/measure-call-state.sh --snapshot
#   lab/scripts/measure-call-state.sh [--interval S] [--settle S] [--out-dir DIR] -- <run-call-load.sh args>
#
# Data comes from FreeSWITCH itself (`status`, `show channels`, `show calls`) over authenticated ESL
# plus /proc and cgroup counters inside the container. The ESL password is never passed in argv.
# "TOTAL" figures are server-wide; "TEST" figures come from the harness (calls it created).
set -uo pipefail
cd "$(dirname "$0")/../.."

INTERVAL=1; SETTLE=6; OUT_DIR=results
F() { docker compose exec -T freeswitch "$@"; }
die() { echo "ERROR: $*" >&2; exit 4; }

docker compose ps -q freeswitch | grep -q . || die "lab is not running (lab/scripts/lab-up.sh)"

if [ "${1:-}" = "--snapshot" ]; then F esl-measure --snapshot; exit $?; fi

while [ $# -gt 0 ] && [ "$1" != "--" ]; do
  case "$1" in
    --interval) INTERVAL=${2:-}; shift 2 || die "--interval needs a value" ;;
    --settle)   SETTLE=${2:-}; shift 2 || die "--settle needs a value" ;;
    --out-dir)  OUT_DIR=${2:-}; shift 2 || die "--out-dir needs a value" ;;
    *) die "unknown argument: $1 (put run-call-load.sh arguments after --)" ;;
  esac
done
[ "${1:-}" = "--" ] || die "usage: $0 [--interval S] [--settle S] [--out-dir DIR] -- <run-call-load.sh args>"
shift
[[ "$INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "--interval must be numeric"
[[ "$SETTLE" =~ ^[0-9]+$ ]] || die "--settle must be an integer number of seconds"

TS=$(date -u +%Y%m%dT%H%M%SZ)
DIR="$OUT_DIR/$TS"
mkdir -p "$DIR" || die "cannot create $DIR"
echo "output directory: $DIR"

SAMPLER_PID=""
stop_sampler() {
  F sh -c 'touch /tmp/lab-measure.stop' >/dev/null 2>&1
  [ -n "$SAMPLER_PID" ] && wait "$SAMPLER_PID" 2>/dev/null
  SAMPLER_PID=""
}
trap 'stop_sampler' EXIT
trap 'stop_sampler; exit 130' INT
trap 'stop_sampler; exit 143' TERM

echo "== before: $(date -u +%FT%TZ)"
F esl-measure --snapshot | tee "$DIR/before.json"

F esl-measure --interval "$INTERVAL" --max-seconds 1800 > "$DIR/samples.jsonl" &
SAMPLER_PID=$!
sleep 3                       # a few idle samples before load

T_START=$(date -u +%FT%TZ)
echo "== load: $T_START  (run-call-load.sh $*)"
"$(dirname "$0")/run-call-load.sh" --result-file "$DIR/load.json" "$@"
LOAD_RC=$?
T_END=$(date -u +%FT%TZ)
echo "== load finished: $T_END exit=$LOAD_RC; settling ${SETTLE}s"
sleep "$SETTLE"
stop_sampler

echo "== after: $(date -u +%FT%TZ)"
F esl-measure --snapshot | tee "$DIR/after.json"

F esl-measure --summarize < "$DIR/samples.jsonl" > "$DIR/summary.json"
echo "== sampler summary (TOTAL = whole server; lab_legs = test calls' near legs only)"
cat "$DIR/summary.json"
if [ -s "$DIR/load.json" ]; then
  echo "== harness result (TEST-generated counters)"
  cat "$DIR/load.json"
fi

RETURNED=$(grep -o '"returned_to_baseline": [a-z]*' "$DIR/summary.json" | awk '{print $2}')
echo "== verdict: load_exit=$LOAD_RC returned_to_baseline=${RETURNED:-unknown}"
[ "$LOAD_RC" = 0 ] && [ "$RETURNED" = true ] || exit 1
