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

See [UPSTREAM_CONTRIBUTION.md](UPSTREAM_CONTRIBUTION.md). Short version: the `integrations-extras` checkout is what is
submitted; `integration/` mirrors it. Compare with:

```shell
diff -ru \
  --exclude=dist --exclude=.benchmarks --exclude=__pycache__ --exclude=.pytest_cache --exclude=.venv \
  --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.coverage \
  ../integrations-extras/freeswitch integration
```

No output means the two are aligned.
