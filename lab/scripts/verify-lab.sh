#!/usr/bin/env bash
# Verify the running lab. Does not generate calls unless --call-test is given.
# The ESL password is never placed in argv: it is read inside the container from the Docker secret.
set -uo pipefail
cd "$(dirname "$0")/../.."

CALL_TEST=0
[ "${1:-}" = "--call-test" ] && CALL_TEST=1

SVC=freeswitch
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails + 1)); }

fs() { docker compose exec -T "$SVC" fs-cli -x "$1" 2>&1; }

cid=$(docker compose ps -q "$SVC")
[ -n "$cid" ] || { echo "FAIL  container not running (lab/scripts/lab-up.sh)"; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' "$cid")" = "true" ] && pass "container running" || fail "container running"
[ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = "healthy" ] && pass "healthcheck healthy" || fail "healthcheck healthy"

# ESL via the stdlib client (password from the secret file)
out=$(docker compose exec -T "$SVC" esl-check --expect UP 2>&1) && pass "esl-check: authenticated, status UP" || fail "esl-check status ($out)"
echo "      $(echo "$out" | sed -n '2p')"

# Wrong password: must exit 2 (authentication rejected). Test value is not a real secret.
docker compose exec -T -e LAB_BAD_PW=wrong-password "$SVC" esl-check --password-env LAB_BAD_PW >/dev/null 2>&1
rc=$?; [ "$rc" = 2 ] && pass "wrong ESL password rejected (exit 2)" || fail "wrong ESL password rejected (exit $rc)"

# Nothing listening -> exit 3
docker compose exec -T "$SVC" esl-check --port 9 --timeout 2 >/dev/null 2>&1
rc=$?; [ "$rc" = 3 ] && pass "unreachable ESL reported as failure (exit 3)" || fail "unreachable ESL (exit $rc)"

# From a second container on the lab network by service name
docker compose run --rm --no-deps -T --entrypoint esl-check "$SVC" --host freeswitch --expect UP >/dev/null 2>&1 \
  && pass "ESL reachable at freeswitch:8021 from lab network" || fail "ESL reachable at freeswitch:8021 from lab network"

# fs_cli through the wrapper
fs status | grep -q '^UP ' && pass "fs-cli wrapper status" || fail "fs-cli wrapper status"
[ "$(fs 'module_exists mod_event_socket')" = "true" ] && pass "mod_event_socket loaded" || fail "mod_event_socket loaded"
[ "$(fs 'module_exists mod_sofia')" = "true" ] && pass "mod_sofia loaded" || fail "mod_sofia loaded"

# Port exposure
if docker port "$cid" 2>/dev/null | grep -q '8021/'; then fail "ESL 8021 not published"; else pass "ESL 8021 not published"; fi
if docker port "$cid" | grep -vE '^(5060/(tcp|udp)|16[34][0-9]{2}/udp)' | grep -q .; then fail "only SIP/RTP published"; else pass "only SIP/RTP published"; fi
if docker port "$cid" | grep -qE '0\.0\.0\.0|::'; then echo "WARN  ports are bound to all host interfaces (HOST_BIND_ADDR)"; fi

# Secret exposure: password must not be in env, inspect output, or any process argument
pw=$(cat secrets/esl_password)
if docker inspect "$cid" | grep -qF -- "$pw"; then fail "password absent from docker inspect"; else pass "password absent from docker inspect"; fi
if docker compose exec -T "$SVC" sh -c 'cat /proc/[0-9]*/cmdline /proc/[0-9]*/environ 2>/dev/null | tr "\0" "\n"' | grep -qF -- "$pw"; then
  fail "password absent from container process args/env"; else pass "password absent from container process args/env"; fi
if docker top "$cid" | grep -qF -- "$pw"; then fail "password absent from docker top"; else pass "password absent from docker top"; fi

fs 'xml_locate dialplan context name default' | grep -q 'lab_tone' && pass "lab dialplan loaded" || fail "lab dialplan loaded"

if [ "$CALL_TEST" = 1 ]; then
  r=$(fs 'originate {origination_uuid=lab-verify}loopback/9197/default &echo')
  echo "$r" | grep -q '^+OK' && pass "single loopback call to 9197 (-> $r)" || fail "loopback call: $r"
  fs hupall >/dev/null
fi

echo; [ "$fails" -eq 0 ] && echo "All checks passed." || { echo "$fails check(s) failed."; exit 1; }
