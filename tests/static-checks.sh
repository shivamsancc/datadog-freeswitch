#!/usr/bin/env bash
# Static checks that need no running container.
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0
ok()  { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; fail=1; }

docker compose config -q && ok "docker compose config" || bad "docker compose config"
grep -rIl "ClueCon" docker docker-compose.yml .env.example 2>/dev/null | grep -v '^$' && bad "default ESL password referenced" || ok "no default ESL password in config"
git check-ignore -q .env && ok ".env is git-ignored" || bad ".env is not git-ignored"
if git ls-files --error-unmatch .env >/dev/null 2>&1; then bad ".env is tracked"; else ok ".env not tracked"; fi
grep -q '8021' docker-compose.yml && grep -nE '^\s*-\s*".*8021' docker-compose.yml && bad "8021 published" || ok "8021 not published in compose"
grep -rnE 'fs_cli[^|]*(-p|--password)[ =]' scripts docker-compose.yml docker/freeswitch/bin README.md | grep -v 'not\|never' && bad "password passed to fs_cli via argv" || ok "no fs_cli -p usage"
grep -q 'secrets/' .gitignore && ok "secrets/ is git-ignored" || bad "secrets/ not ignored"
exit $fail
