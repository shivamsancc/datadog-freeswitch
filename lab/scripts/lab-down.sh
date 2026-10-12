#!/usr/bin/env bash
# Stop the lab. Pass --purge to also delete the log/db volumes.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ "${1:-}" = "--purge" ]; then
  docker compose down --volumes --remove-orphans
else
  docker compose down --remove-orphans
fi
