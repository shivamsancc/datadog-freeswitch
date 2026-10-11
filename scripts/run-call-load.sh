#!/usr/bin/env bash
# Controlled synthetic-call load for the FreeSWITCH lab.
#
#   scripts/run-call-load.sh --scenario <idle|single|concurrent|normal|timeout|fail> [options]
#
# Calls are local loopback calls inside FreeSWITCH (dialplan context "default", extensions
# 9390/93xxx); nothing is routed to a carrier. Nothing is generated unless you run this script.
set -uo pipefail
cd "$(dirname "$0")/.."

# ---- safety limits (mirrored in docker/freeswitch/bin/esl-load, which re-validates) ----
DEFAULT_MAX_CALLS=5     # above this: --allow-high is required
HARD_MAX_CALLS=20       # absolute ceiling for concurrent calls
HARD_MAX_HOLD=60        # seconds per call
HARD_MAX_DURATION=600   # seconds per run
HARD_MAX_PAUSE=60
HARD_MAX_BATCHES=50

usage() {
  cat <<USAGE
Usage: $0 --scenario NAME [options]

Scenarios:
  idle        no calls; observe for --duration seconds (default 10)
  single      exactly 1 call that completes normally
  concurrent  --calls N simultaneous calls (default 3, min 2) that complete normally
  normal      --calls N calls (default 1) that complete normally after --hold seconds
  timeout     --calls N calls whose far end would hold ~120 s but are cut at --hold seconds
              (hangup cause ALLOTTED_TIMEOUT)
  fail        --calls N calls to an intentionally nonexistent extension (NO_ROUTE_DESTINATION)

Options:
  --calls N          concurrent calls per batch (default: scenario-specific; max $DEFAULT_MAX_CALLS,
                     up to $HARD_MAX_CALLS only with --allow-high)
  --hold S           seconds each call lasts / is cut at (default 5, 1..$HARD_MAX_HOLD)
  --duration S       0 (default) = exactly one batch; otherwise repeat batches until S seconds
                     are used (max $HARD_MAX_DURATION). For idle: observation time.
  --ramp R           calls started per second within a batch (default 2, 0.1..5)
  --pause S          seconds between batches (default 0, max $HARD_MAX_PAUSE)
  --max-batches N    cap on batches when --duration is set (default 1, max $HARD_MAX_BATCHES)
  --allow-high       permit more than $DEFAULT_MAX_CALLS concurrent calls (hard limit $HARD_MAX_CALLS)
  --result-file F    also write the JSON result to F
  --dry-run          validate and print the plan, place no calls
  -h, --help
USAGE
}

die() { echo "ERROR: $*" >&2; exit 4; }

SCENARIO=""; CALLS=""; HOLD=5; DURATION=0; RAMP=2; PAUSE=0; MAX_BATCHES=1
ALLOW_HIGH=0; RESULT_FILE=""; DRY=0

is_int()   { [[ "$1" =~ ^[0-9]+$ ]]; }
is_float() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; }

while [ $# -gt 0 ]; do
  case "$1" in
    --scenario)    SCENARIO=${2:-}; shift 2 || die "--scenario needs a value" ;;
    --calls)       CALLS=${2:-}; shift 2 || die "--calls needs a value" ;;
    --hold)        HOLD=${2:-}; shift 2 || die "--hold needs a value" ;;
    --duration)    DURATION=${2:-}; shift 2 || die "--duration needs a value" ;;
    --ramp)        RAMP=${2:-}; shift 2 || die "--ramp needs a value" ;;
    --pause)       PAUSE=${2:-}; shift 2 || die "--pause needs a value" ;;
    --max-batches) MAX_BATCHES=${2:-}; shift 2 || die "--max-batches needs a value" ;;
    --allow-high)  ALLOW_HIGH=1; shift ;;
    --result-file) RESULT_FILE=${2:-}; shift 2 || die "--result-file needs a value" ;;
    --dry-run)     DRY=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             usage >&2; die "unknown argument: $1" ;;
  esac
done

case "$SCENARIO" in idle|single|concurrent|normal|timeout|fail) ;; *) usage >&2; die "--scenario is required and must be one of the listed names" ;; esac
is_int "$HOLD"        && [ "$HOLD" -ge 1 ] && [ "$HOLD" -le $HARD_MAX_HOLD ] || die "--hold must be an integer 1..$HARD_MAX_HOLD"
is_int "$DURATION"    && [ "$DURATION" -le $HARD_MAX_DURATION ]               || die "--duration must be an integer 0..$HARD_MAX_DURATION"
is_int "$PAUSE"       && [ "$PAUSE" -le $HARD_MAX_PAUSE ]                     || die "--pause must be an integer 0..$HARD_MAX_PAUSE"
is_int "$MAX_BATCHES" && [ "$MAX_BATCHES" -ge 1 ] && [ "$MAX_BATCHES" -le $HARD_MAX_BATCHES ] || die "--max-batches must be an integer 1..$HARD_MAX_BATCHES"
is_float "$RAMP"      && awk "BEGIN{exit !($RAMP>=0.1 && $RAMP<=5)}"          || die "--ramp must be a number 0.1..5"
if [ -n "$CALLS" ]; then
  is_int "$CALLS" || die "--calls must be a positive integer"
  [ "$CALLS" -le $HARD_MAX_CALLS ] || die "--calls $CALLS exceeds the hard limit of $HARD_MAX_CALLS"
  if [ "$CALLS" -gt $DEFAULT_MAX_CALLS ] && [ "$ALLOW_HIGH" != 1 ]; then
    die "--calls above $DEFAULT_MAX_CALLS requires --allow-high (hard limit $HARD_MAX_CALLS)"
  fi
fi
case "$SCENARIO" in
  idle)       [ -z "$CALLS" ] || die "idle places no calls; do not pass --calls" ;;
  single)     [ -z "$CALLS" ] || [ "$CALLS" = 1 ] || die "single is exactly 1 call" ;;
  concurrent) [ -z "$CALLS" ] || [ "$CALLS" -ge 2 ] || die "concurrent needs --calls >= 2" ;;
  *)          [ -z "$CALLS" ] || [ "$CALLS" -ge 1 ] || die "--calls must be >= 1" ;;
esac

RUN_ID=$(openssl rand -hex 4)
ARGS=(--scenario "$SCENARIO" --run-id "$RUN_ID" --hold "$HOLD" --duration "$DURATION" --ramp "$RAMP"
      --pause "$PAUSE" --max-batches "$MAX_BATCHES")
[ -n "$CALLS" ] && ARGS+=(--calls "$CALLS")
[ "$ALLOW_HIGH" = 1 ] && ARGS+=(--allow-high)

echo "run_id=$RUN_ID scenario=$SCENARIO calls=${CALLS:-default} hold=${HOLD}s duration=${DURATION}s ramp=${RAMP}/s pause=${PAUSE}s max_batches=$MAX_BATCHES"
[ "$DRY" = 1 ] && { echo "dry-run: arguments valid, no calls placed."; exit 0; }

docker compose ps -q freeswitch | grep -q . || die "lab is not running (scripts/lab-up.sh)"
[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q freeswitch)")" = healthy ] \
  || die "FreeSWITCH container is not healthy"

STARTED=0
FINISHED=0
cleanup() {
  # Only channels whose UUID starts with lab-<RUN_ID>- are touched. Skipped after a normal finish
  # (the generator already verified it left nothing behind).
  if [ "$STARTED" = 1 ] && [ "$FINISHED" != 1 ]; then
    docker compose exec -T freeswitch esl-load --cleanup "$RUN_ID" >/dev/null 2>&1 || true
  fi
}
TMP_OUT=$(mktemp)
CHILD=""
on_exit() { [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; rm -f "$TMP_OUT"; cleanup; }
trap on_exit EXIT
trap 'echo "interrupted: cleaning up run $RUN_ID" >&2; exit 130' INT
trap 'echo "terminated: cleaning up run $RUN_ID" >&2; exit 143' TERM

# Run in the background and wait: bash only runs traps promptly while inside `wait`.
STARTED=1
docker compose exec -T freeswitch esl-load "${ARGS[@]}" > "$TMP_OUT" &
CHILD=$!
wait "$CHILD"
RC=$?
CHILD=""
FINISHED=1
OUT=$(cat "$TMP_OUT")
JSON=$(printf '%s\n' "$OUT" | sed -n 's/^RESULT_JSON //p' | tail -n1)
if [ -n "$RESULT_FILE" ] && [ -n "$JSON" ]; then
  mkdir -p "$(dirname "$RESULT_FILE")" && printf '%s\n' "$JSON" > "$RESULT_FILE"
fi
[ -n "$JSON" ] && echo "RESULT_JSON $JSON"
if [ "$RC" != 0 ]; then echo "FAILED: run $RUN_ID exited with code $RC" >&2; exit "$RC"; fi
echo "OK: run $RUN_ID finished as expected"
