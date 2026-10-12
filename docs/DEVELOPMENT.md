# Development guide

## Where does my change go?

| Change | Work in | Also |
|---|---|---|
| Lab image, config, scripts, measurement | `lab/` | Update `lab/README.md` and run `lab/tests/static-checks.sh` |
| Check behavior, config model, metrics, README of the integration | `integration/` **and** the `freeswitch/` directory of the `integrations-extras` checkout | Keep both identical; see [UPSTREAM_CONTRIBUTION.md](UPSTREAM_CONTRIBUTION.md) |
| Repository-level docs | `README.md`, `docs/` | |

## Lab workflow

All commands run from the repository root.

```shell
lab/tests/static-checks.sh        # no containers: compose config, ignore rules, no default password, 8021 unpublished
lab/scripts/lab-up.sh             # builds and starts; creates .env and secrets/esl_password on first run
lab/scripts/verify-lab.sh         # needs the running lab
lab/tests/run-unit-tests.sh       # runs the baseline math/parser tests inside the running container
lab/scripts/lab-down.sh           # add --purge to also delete the log and db volumes
```

Notes:

- `docker-compose.yml` lives at the root and refers to `lab/docker/freeswitch` for both the build context and the
  read-only config bind mount.
- Scripts `cd` to the repository root themselves, so they work from any directory. Output goes to `results/` (ignored).
- Changing `lab/docker/freeswitch/conf` is read by the container on its next reload or recreation; do not edit it
  casually while a measurement is running.

## Integration workflow

Requirements: Python 3.13.

```shell
cd integration
python3.13 -m venv .venv && . .venv/bin/activate
pip install "datadog-checks-base[deps]>=37.36.0" datadog-checks-dev pytest pytest-cov
pytest tests                                     # fake-server tests only
pytest tests --cov=datadog_checks.freeswitch --cov-report=term-missing
```

The upstream tooling (`ddev`) identifies an integration by its **directory name**. This repository names the directory
`integration/`, so use `ddev` from the `integrations-extras` checkout, where the directory is `freeswitch/`:

```shell
cd ../integrations-extras
ddev -x test freeswitch              # tests (-x = work on the current location)
ddev -x test -c freeswitch           # with coverage
ddev -x test -s freeswitch           # ruff format/check and mypy
ddev -x validate config freeswitch   # and: models, package, readmes, metadata, ...
ddev -x release build freeswitch     # wheel (build in a scratch copy to keep an existing dist/ intact)
```

Several `ddev validate` commands need an `integrations-core` checkout and are skipped or fail without one; run the
individual validators rather than `validate all` when it is absent.

## Secrets safety

- Never commit `.env`, `secrets/`, `results/` or logs; the ignore files cover them. Check with `git status` before staging.
- Do not pass the ESL password on a command line, in an environment variable of a long-lived process, or in logs. The lab
  tools read it from a file.
- For a live Agent test, prefer a restricted `userauth` account and Agent secrets management. If a temporary plaintext
  Agent config is unavoidable, restrict it (`root:<agent group>`, mode `0640`), keep it only for the test and restore the
  original afterwards.
- Test fixtures use obviously fake passwords only.

## Synchronizing `integration/` with upstream

The upstream checkout is authoritative and `integration/` is a manually synchronized copy. The comparison command, the
safe synchronization steps and the table of which command runs where are in
[UPSTREAM_CONTRIBUTION.md](UPSTREAM_CONTRIBUTION.md).

## Read-only checks of the running lab

These change nothing and are safe at any time:

```shell
docker compose config -q                                              # Compose file parses
docker compose config | grep -E "context:|source:"                    # intended paths
docker inspect "$(docker compose ps -q freeswitch)" \
  --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'   # actual bind-mount source
docker compose ps                                                     # container health
```

## Migrating the running lab to `lab/docker`

State today: the repository contains both the legacy `docker/` and the new `lab/docker/`. They differ only in comment
lines (script paths in `esl-load`, `esl-baseline` and the dialplan comment). The lab container that is running was created
from the legacy path and bind-mounts `docker/freeswitch/conf` read-only. Nothing in the repository has recreated it with
the new paths, so the first `up` against the new layout is **unvalidated**.

Do this only in an approved maintenance window, because the container is replaced (its log and db volumes are kept):

```shell
# 1. Static checks, no containers affected
lab/tests/static-checks.sh
docker compose config | grep -E "context:|source:.*lab/docker"     # build context and config mount must be under lab/docker

# 2. Recreate with the new paths (rebuilds cached layers as needed)
lab/scripts/lab-up.sh

# 3. Verify
docker inspect "$(docker compose ps -q freeswitch)" \
  --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' | grep "usr/local/freeswitch/conf"
lab/scripts/verify-lab.sh
```

Success means the config mount's source is under `lab/docker/freeswitch/conf`, the container is healthy and
`verify-lab.sh` passes. Only after that, and with explicit approval, remove the legacy `docker/` directory as a normal,
reviewed change (a separate commit). Do not remove it earlier: as long as the old container is running, its mount source
is that directory.
