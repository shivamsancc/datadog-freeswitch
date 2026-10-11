#!/usr/bin/env bash
# Start the FreeSWITCH lab. Creates .env and secrets/esl_password on first run.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { cp .env.example .env; echo "Created .env."; }

SECRET=secrets/esl_password
if [ ! -s "$SECRET" ]; then
  umask 077
  mkdir -p secrets && chmod 700 secrets
  # Migrate a password from an older .env (milestone 1) so the existing one is preserved.
  old=$(sed -n 's/^ESL_PASSWORD=//p' .env | head -n1)
  if [ -n "$old" ]; then
    printf '%s\n' "$old" > "$SECRET"
    echo "Migrated ESL password from .env to $SECRET."
  else
    command -v openssl >/dev/null || { echo "openssl is required to generate the ESL password" >&2; exit 1; }
    openssl rand -hex 24 > "$SECRET"
    echo "Generated ESL password in $SECRET."
  fi
fi
# Remove any legacy plaintext copy from .env.
if grep -q '^ESL_PASSWORD=' .env; then
  sed '/^ESL_PASSWORD=/d' .env > .env.tmp && mv .env.tmp .env && chmod 600 .env
fi
# The container user (uid 999) must be able to read the file. The directory (0700) is what keeps
# other host users out; docker/root on the host can always read it.
chmod 644 "$SECRET"

docker compose up -d --build --wait --wait-timeout 180
docker compose ps
