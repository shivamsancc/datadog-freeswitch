# Milestone 2 report: FreeSWITCH Datadog integration scaffolding

| | |
|---|---|
| Date | 2026-10-11 |
| Scope | Scaffolding, metadata, configuration spec, validation. No functional check code. |
| Lab repository | `main` @ `e67d043`, clean tree before this report. **Nothing in the lab was modified except this file.** |
| Integration checkout | `~/Desktop/Zoos/integrations-extras` (shallow clone of `DataDog/integrations-extras` @ `3084660`), **uncommitted** |
| Result | Scaffold generated and populated; all executed validations pass. Not submission-ready (see §8). |

## 1. Environment and tool versions

| Tool | Version | Where |
|---|---|---|
| Python | 3.13.15 | Existing ServBay framework build (`/Applications/ServBay/package/python/current/...`). Only used read-only to create a venv. Homebrew `python@3.11` untouched. |
| pipx | 1.17.13 | venv `~/.local/share/dd-tools/bootstrap` |
| ddev | 20.0.0 | pipx venv `~/.local/share/pipx/venvs/ddev` (Python 3.13.15) |
| hatch / uv | 1.18.1 / 0.13.0 | bundled inside the ddev venv |
| datadog-checks-base | 38.4.0 | resolved by hatch test env |
| Docker | 29.6.2 | present, not used |

Exact commands (installation):

```bash
P=/Applications/ServBay/package/python/current/Python.framework/Versions/Current/bin/python3.13
$P -m venv ~/.local/share/dd-tools/bootstrap
~/.local/share/dd-tools/bootstrap/bin/pip install pipx
export PIPX_HOME=$HOME/.local/share/pipx PIPX_BIN_DIR=$HOME/.local/bin
~/.local/share/dd-tools/bootstrap/bin/pipx install --python $P ddev
git clone --depth 1 https://github.com/DataDog/integrations-extras.git ~/Desktop/Zoos/integrations-extras
```

No system Python, Homebrew package or global pip was changed. `ddev config set repo extras` was **not** run (it writes the user
config); a throwaway config passed through `DDEV_CONFIG` (scratchpad) selects the `extras` repo and clone path instead.
Note that `ddev` needs `~/.local/share/pipx/venvs/ddev/bin` on `PATH` so hatch finds `uv`; otherwise `ddev test` fails with `uv: command not found`.

## 2. Upstream checkout

- **No fork exists**: `https://api.github.com/repos/shivamsancc/integrations-extras` returns 404. No fork was created or pushed.
- Work is in a local clone of upstream (`origin` = `DataDog/integrations-extras`, shallow, depth 1). Nothing is committed.
- Limitation: because the clone is shallow and has no fork remote, `ddev validate all changed` (diff against a base branch) could not be exercised the way upstream CI does;
  `ddev validate all freeswitch` was run instead.
- `ddev create` without `-l` writes to `~/dd/integrations-extras` (ddev's default); `-l .` was used to target the clone.

## 3. Repository preflight

- Audit read completely. Lab state: `main`, remote `origin` = `github.com/shivamsancc/datadog-freeswitch`, clean after the maintainer's commit `e67d043`.
  The audit's "uncommitted harness" concern no longer applies (those files are tracked).
- **Git processes:** no `.git/*.lock`, no stash entries. The running `git` processes are several `git fsmonitor--daemon` instances launched by
  **GitHub Desktop** (started 19:07–19:14 today). They are not demonstrably stale and do not belong to this task, so none were killed.
  They are harmless but duplicated; closing GitHub Desktop will stop them.

## 4. Generated scaffold and differences from the audit

Dry run `ddev create check -n freeswitch` (the legacy `-nt` flags are a deprecated shim in ddev 20) and then `ddev create check -l . freeswitch`.
The template is `check` (not `check_only`, which only adds check files to an existing integration).

Differences from the audit's §4 checklist:

| Audit expectation | Reality in ddev 20.0.0 / current extras |
|---|---|
| `datadog_checks/__init__.py` namespace shim | **Not generated**, not needed |
| `setup.py` / `requirements-dev.txt` / `tox.ini` | Not generated (hatchling only) |
| `CHANGELOG.md` **and** `changelog.d/1.added` | Template generates both. **Extras uses only `CHANGELOG.md`** (see §6) |
| `assets/dashboards` optional | Template generates a sample dashboard and a `<FILL IN>` manifest entry; removed |
| `service_checks.json` conditional | Not generated; not added (design decision) |
| `LICENSE` unconfirmed | Not generated. Extras integrations rely on the root `LICENSE` and `pyproject.toml` |
| `tests/test_e2e.py` recommended | Generated; removed (needs a real environment, there is none yet) |
| `dd_environment` fixture | Removed with e2e |
| `hatch.toml` default | `check-types = true` only. Needed `mypy-deps` for lint to pass in extras (see §7) |
| `pyproject` `authors` Datadog, `Source` = integrations-core, `requires-python >=3.13`, `datadog-checks-base>=37.36.0` | Template is core-flavoured. `authors = []` and `Source` → integrations-extras applied to match extras checks (e.g. `exim`). Python/base pins kept as generated |

Template headers (`(C) Datadog, Inc. 2026-present`, BSD 3-clause) match what current extras checks use and were kept.

## 5. Files created and their purpose

All under `~/Desktop/Zoos/integrations-extras`:

| Path | Purpose |
|---|---|
| `freeswitch/manifest.json` | Integration identity, tile, classifier tags, assets. `app_uuid` and `source_type_id` were generated by `ddev`, not hand-written |
| `freeswitch/metadata.csv` | 5 v1 metrics |
| `freeswitch/assets/configuration/spec.yaml` | Configuration source of truth |
| `freeswitch/datadog_checks/freeswitch/data/conf.yaml.example` | Generated by `ddev validate config --sync` |
| `freeswitch/datadog_checks/freeswitch/config_models/*` | Generated by `ddev validate models --sync` |
| `freeswitch/datadog_checks/freeswitch/{__about__,__init__,check}.py` | Package; `check.py` is an inert stub |
| `freeswitch/tests/{__init__,conftest,test_unit}.py` | Stub-level tests |
| `freeswitch/{README.md,CHANGELOG.md,pyproject.toml,hatch.toml}`, `images/.gitkeep` | Docs, packaging, test env |
| `.github/CODEOWNERS` (modified) | `/freeswitch/` entry (required by `validate codeowners`) |
| `.github/workflows/test-all.yml` (modified) | New job for `freeswitch`, generated by `ddev validate ci --sync` |

Only this report was added to the lab repository. The integration was not copied into the lab.

## 6. Metadata, configuration, changelog and licensing decisions

**Identity:** name `freeswitch`, package `datadog-freeswitch`, module `datadog_checks.freeswitch`, class `FreeswitchCheck`,
`__NAMESPACE__ = 'freeswitch'`, prefix `freeswitch.`, tile title `FreeSWITCH`.

**Metrics** (`metadata.csv`, exactly the five agreed; no extra tags):

| Metric | Type | Unit | Orientation | `sample_tags` |
|---|---|---|---|---|
| `freeswitch.can_connect` | gauge | | 1 | |
| `freeswitch.uptime` | gauge | second | 0 | |
| `freeswitch.channels.active` | gauge | | 0 | |
| `freeswitch.collection.duration` | gauge | second | -1 | |
| `freeswitch.collection.errors` | count | error | -1 | `error_type` |

`manifest.assets.integration.metrics.check` = `freeswitch.can_connect`. No service check, no `service_checks.json`, no dashboard.

**Configuration options** (spec, with model constraints generated):

| Option | Required | Default | Notes |
|---|---|---|---|
| `host` | yes | none | example `127.0.0.1` |
| `port` | no | 8021 | integer 1–65535 |
| `username` | no | none | `user@domain`, for restricted `userauth` (not implemented yet) |
| `password` | yes | none | `secret: true`; description recommends `ENC[...]` |
| `connect_timeout` | no | 3 s | 0.1–30 |
| `read_timeout` | no | 3 s | 0.1–30 |
| `tags` | no | none | `instances/tags` template |
| `min_collection_interval` | no | 15 | from `instances/global` (supported convention; also brings `empty_default_hostname`) |

`instances/default` was **not** used because it also adds `metric_patterns`, `disable_generic_tags` and
`enable_legacy_tags_normalization`, which this check would not implement. `password` is required because ESL always
needs one; `host` is required rather than defaulting silently to localhost. The defaults for timeouts are the audit's proposal and are not yet validated against a live check.

**Changelog:** extras convention is `CHANGELOG.md` only: 266 integrations have one, none uses `changelog.d/`, and extras
PR guidance (audit S6 `pr.md`) uses `CHANGELOG.md`. `changelog.d/` was deleted. The entry is under `## Unreleased` with an
`***Added***` block; new community integrations normally appear as `## 1.0.0 / <date>` with `Initial Release` plus a PR link,
which can only be written when the PR number and release version exist. `__version__` stays `0.0.1`.

**Licensing:** `license = "BSD-3-Clause"`, classifier `License :: OSI Approved :: BSD License` and `Private :: Do Not Upload` (generated, same as other
extras). No Marketplace fields (`legal_terms`, `pricing`, `vendor_id`) were added. No per-integration `LICENSE` file (extras root license applies).

**Identity/ownership: placeholders, needs maintainer input.**

| Field | Value now | Why |
|---|---|---|
| `manifest.author.support_email` | `TODO-maintainer-support-email@example.invalid` | Not established. Git config email is a work address (`shivam.anand@zoosglobal.com`) and the GitHub identity is ambiguous (`shivamzoos` in git config vs `shivamsancc` on the remote); neither was used |
| `manifest.author.name` / `homepage` | `Community` / extras repo | Convention used by 40+ extras manifests |
| `manifest.owner` | `integrations-developer-platform` | Most common value for extras (165) and what the template's `<FILL IN>` expects; confirm |
| `.github/CODEOWNERS` | `/freeswitch/ @DataDog/ecosystems-review` | Entry exists to satisfy validation; **no maintainer GitHub handle** is listed yet. Other entries have `@handle email @DataDog/ecosystems-review` |
| `sales_email` | omitted | Optional in extras manifests |

The template's default author `Datadog` / `help@datadoghq.com` was removed because it would misattribute ownership.

## 7. Validation results

All commands were executed from the integrations-extras clone with the throwaway `DDEV_CONFIG` (`-e` = extras). Exit status is the real one.

| # | Command | Result | Exit |
|---|---|---|---|
| 1 | `ddev -e validate config --sync freeswitch` | Passed (wrote `conf.yaml.example`) | 0 |
| 2 | `ddev -e validate models --sync freeswitch` | Passed (wrote 4 model files) | 0 |
| 3 | `ddev -e validate config freeswitch` | Passed, in sync | 0 |
| 4 | `ddev -e validate models freeswitch` | Passed, in sync | 0 |
| 5 | `ddev -e validate metadata freeswitch` | Passed | 0 |
| 6 | `ddev -e validate readmes freeswitch` | Passed | 0 |
| 7 | `ddev -e validate package freeswitch` | Passed | 0 |
| 8 | `ddev -e validate imports freeswitch` | Passed | 0 |
| 9 | `ddev -e validate legacy-signature freeswitch` | Passed | 0 |
| 10 | `ddev -e validate integration-style freeswitch` | Passed | 0 |
| 11 | `ddev -e validate ci` | **Failed first**: not in sync | 1 |
| 12 | `ddev -e validate ci --sync` then `ddev -e validate ci` | Synced, then passed (`test-all.yml` modified) | 0 |
| 13 | `ddev -e validate codeowners` | Passed (after adding the entry) | 0 |
| 14 | `ddev -e validate all freeswitch` | All 11 validations passed (the extras CI set) | 0 |
| 15 | `ddev -e test freeswitch` (2 unit tests) | 2 passed, 2 pydantic deprecation warnings from generated models | 0 |
| 16 | `ddev -e test -c freeswitch` (coverage) | 2 passed, total 96 % | 0 |
| 17 | `ddev -e test -fs freeswitch` (format + ruff fix) | Passed, no changes | 0 |
| 18 | `ddev -e test -s freeswitch` (ruff, ruff format, mypy) | **Failed first** (mypy `import-not-found`, `datadog_checks.base` not installed in the lint env). After adding `mypy-deps` to `hatch.toml`: `no issues found in 11 source files` | 1 → 0 |
| 19 | `ddev -e release build freeswitch` | Built wheel (artifact removed, not kept) | 0 |
| 20 | `uv build --wheel` in `freeswitch/` | Built `datadog_freeswitch-0.0.1-py3-none-any.whl` (12 files, outside the repo) | 0 |

Not run / not applicable:

| Item | Status |
|---|---|
| `ddev validate version` | **Skipped by the tool**: "only available for repo `core`" (exit 1 is the skip, not a failure) |
| `ddev validate all changed` | **Not run**: shallow clone, no base branch to diff |
| `ddev test -m integration` / e2e (`dd_agent_check`) | **Not run**: no environment; `test_e2e.py` was removed |
| `ddev validate manifest` / `service-checks` / `dashboards` / `license-headers` | **Not run**: not in extras' validation set (audit §3.1 row 17) |
| `datadog-agent integration install` of the wheel | **Not run**: no Agent installed (out of scope) |
| Validation by upstream CI | **Not run** |

Findings worth reporting:

- **Template defect in extras:** the generated `hatch.toml` sets `check-types = true` but `mypy` in the lint env cannot resolve `datadog_checks.base`.
  `ddev test -s` fails on a freshly scaffolded integration. The fix applied is `mypy-deps = ["datadog-checks-base>=37.36.0", "datadog-checks-dev", "pytest"]`. Upstream CI behaviour for this was not observed; confirm with reviewers or drop `check-types` if they prefer.
- `ddev release build` builds "as it is on the repo HEAD" per its help text yet succeeded on untracked files here; the wheel was verified by `uv build` independently.
- Wheel metadata checked: `Name: datadog-freeswitch`, `Requires-Python >=3.13`, `License-Expression: BSD-3-Clause`, `Source` = integrations-extras, contents are only `datadog_checks/freeswitch/**`.
- The stub's unit test asserts that nothing is submitted (no metrics, no service checks). The `instance` fixture uses a dummy password string that is not a secret.

## 8. Remaining blockers and maintainer decisions

| ID | Item | Needed |
|---|---|---|
| B1 | Public support email for `manifest.author.support_email` | Maintainer to provide (today an `example.invalid` placeholder) |
| B2 | Maintainer GitHub handle for CODEOWNERS (and which account: `shivamzoos` or `shivamsancc`) | Maintainer to provide |
| B3 | `manifest.owner` value and whether partner enrolment is required (audit R1) | Confirm with Datadog reviewers on a draft PR |
| B4 | Fork of `DataDog/integrations-extras` | Maintainer to create (or approve creating); needed before any push or PR |
| B5 | Release/version and changelog entry format (`## 1.0.0 / <date>` and PR link) | Decided at PR time |
| B6 | `source_type_id` 91734645 was generated by the tool; extras IDs may be assigned by Datadog | Confirm on the PR (audit R2) |
| B7 | Placement of the integration: it currently lives only in the sibling clone; nothing links it to this lab repo | Decide how to sync (the audit recommended fork + copy/sync) |
| B8 | `hatch.toml` `mypy-deps` workaround (see §7) | Confirm acceptable upstream |
| B9 | Dependency pin `datadog-checks-base>=37.36.0` (latest 38.4.0) and `requires-python >=3.13` kept as generated | Confirm minimum Agent version targeted |
| B10 | Duplicate GitHub Desktop `fsmonitor` daemons | Optional cleanup by the maintainer |

Decision D4 (gauge only, no service check) was followed. The `README.md` states that the check is under development; revise once collection exists.

## 9. Exact next milestone

**Milestone 3: minimal ESL client and Agent check** (audit §10). Prerequisites: B1/B2 answered or deferred explicitly, and approval to proceed.

Scope as defined in the audit:

1. `freeswitch/datadog_checks/freeswitch/esl.py`: stdlib-only ESL client (connect, `auth`, one `api` command per call, close),
   body cap 64 KiB, disconnect-notice handling, typed errors with fixed messages, command allowlist (`uptime s`, `show channels count`).
2. `check.py`: config parsing in `__init__`, connection lifecycle, the five metrics, `error_type` in `connect|timeout|auth|protocol|parse`.
3. Fake-ESL-server smoke tests; then a manual run against the lab, which stays unmodified.
4. `userauth` support stays a separate approved experiment.

Stop here until approved.
