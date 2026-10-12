#!/usr/bin/env bash
# Unit tests for the baseline measurement helpers; run inside the running lab container.
set -euo pipefail
cd "$(dirname "$0")/../.."
docker compose exec -T freeswitch python3 - < lab/tests/unit/test_baseline.py
