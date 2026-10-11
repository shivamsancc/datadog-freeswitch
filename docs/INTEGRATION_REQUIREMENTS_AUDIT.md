# Datadog integration requirements audit for FreeSWITCH

| | |
|---|---|
| Audit date | 2026-10-11 (all web sources below were fetched on this date) |
| Scope | Audit only. No integration code, no FreeSWITCH/Docker changes, no installs, no commits. |
| Repository state audited | `main` @ `f2e2cb6` plus uncommitted working-tree changes (see §2) |
| Lab state during audit | Running, healthy, FreeSWITCH 1.10.12, FreeSWITCH uptime ≈ 57 min |

Evidence labels used throughout:

- **MANDATORY**: backed by a cited official source or an enforcing validation/CI file.
- **RECOMMENDED**: official guidance worded as advice, or inferred from an enforced convention.
- **OPTIONAL**: official sources show it is not required.
- **UNCONFIRMED**: could not be confirmed from public documentation; needs a question to Datadog reviewers or an experiment.

---

## 1. Executive summary

1. **Where it goes.** A community Agent check for FreeSWITCH belongs in
   [`DataDog/integrations-extras`](https://github.com/DataDog/integrations-extras). It is not bundled with the Agent and
   customers install it with `datadog-agent integration install` (§3, S8). `integrations-core` is Datadog-owned and
   new integrations are directed to extras (S5).
2. **Toolchain is heavier than the lab's.** Datadog's tooling (`ddev` 20.0.0, `datadog-checks-base` 38.4.0) requires
   **Python ≥ 3.13**, `pipx`, `hatch` and Docker. None of `ddev`, `pipx`, `hatch` or Python 3.13 is installed on this
   host (Python 3.11 only), so **no `ddev` command was run** and no validation result exists yet. Nothing here claims submission readiness.
3. **The check can be small and stdlib-only.** No third-party dependency beyond `datadog-checks-base` is needed. The existing lab ESL
   client is a good protocol core but is a one-shot CLI, not a reusable library, and must be restructured (§2.3).
4. **A prior attempt exists.** [integrations-extras#1609 "Add freeswitch Integration"](https://github.com/DataDog/integrations-extras/pull/1609)
   (29 files, by another author) was opened 2022-11-21 and **closed unmerged for inactivity** on 2023-08-21 after a docs-focused
   review. The name `freeswitch` is currently free on `master`. This is prior art, not a blocker (§11).
5. **Verified lab semantics drive the metric design.** In FreeSWITCH 1.10.12 on the lab, one bridged loopback test call is
   **4 channels**, `status` reports **4 sessions**, and `show calls` returns **3 rows**. Channel count is not call count,
   and `show calls` rows are not calls. v1 should expose only *channels*, not *calls* (§7).
6. **Security is the main design driver.** Anyone with the shared ESL password can run `system`, `bg_system`, `originate`, `hupall`,
   `shutdown`, `unload` (all present in the 1.10.12 lab; §6). FreeSWITCH supports per-user `userauth` with
   `esl-allowed-api` / `esl-allowed-events` (verified in the 1.10.12 source). The check should be designed and tested around a restricted
   monitoring user, and the lab does not currently have one.
7. **Datadog's own guidance discourages new service checks** ("New integrations generally should not submit their own service
   checks. Prefer metrics and metric-based monitors instead", S6 `base/basics.md`). Availability should therefore be a gauge
   (`freeswitch.can_connect`) first. The service check is an open question for reviewers (§7).
8. **Upstream CI cannot run a real FreeSWITCH.** There is no public, maintained FreeSWITCH image (milestone 1 finding;
   SignalWire's apt repo needs a token). Upstream tests must therefore run against a fake ESL server; real-FreeSWITCH
   tests stay local/optional (§8, §11).
9. **Proposed order:** scaffold + metadata → minimal ESL client/check → failure-path tests → real-Agent run → controlled
   overhead benchmark → docs/packaging/submission prep (§10). **Recommended next task: Milestone 2** (§12), after you decide on the
   open items listed there.

---

## 2. Current repository assessment

### 2.1 Inventory and git state

| Area | Files | State |
|---|---|---|
| Lab | `docker-compose.yml`, `docker/freeswitch/Dockerfile`, `modules.conf`, `conf/**` (12 files), `.env.example`, `.dockerignore`, `.gitignore` | Tracked (milestone 1–2 state at `HEAD`; Dockerfile/dialplan/.gitignore have later uncommitted edits) |
| ESL tooling | `docker/freeswitch/bin/esl-check`, `fs-cli` | Tracked |
| ESL tooling (later) | `bin/esl_lab.py`, `esl-load`, `esl-measure`, `esl-baseline` | **Untracked** |
| Scripts | `lab-up.sh`, `lab-down.sh`, `verify-lab.sh` tracked; `run-call-load.sh`, `measure-call-state.sh`, `run-baseline.sh` **untracked** | |
| Tests | `tests/static-checks.sh` tracked; `tests/run-unit-tests.sh`, `tests/unit/test_baseline.py` **untracked** | 7 static checks, 13 unit tests |
| Docs | `README.md` (354 lines, modified, uncommitted) | |
| Ignored (present) | `.env`, `secrets/`, `results/` | `results/baseline/` has 2 run directories (one smoke, one 3-rep reference); **not touched** |

`git log` has a single commit (`f2e2cb6 nb`). **Milestones 3 and 4 exist only in the working tree.** A fresh clone of
`HEAD` would not reproduce the call harness or baseline tooling, and `HEAD`'s Dockerfile does not match the working tree. Recommend the
maintainer commit before milestone 2 (this audit did not).

### 2.2 What is reusable

- **Lab**: reproducible FreeSWITCH 1.10.12 image, authenticated ESL, private network, unpublished 8021, narrow RTP range.
- **Call harness** (`esl-load`, `run-call-load.sh`): bounded, UUID-namespaced, cleans only its own calls, expected-outcome checks.
  Exactly what integration tests and overhead runs need for deterministic channel counts.
- **Baseline method** (`esl-baseline`, `run-baseline.sh`): CPU/memory by counter deltas over monotonic time, working set vs RSS,
  warm-up/window, ESL latency series, validation of channel counts, JSON + Markdown output.
- **Reference baseline** (`results/baseline/20261011T142631Z-1bebdd`): 3 reps × {idle, 1, 5, 10 calls}.

### 2.3 Existing ESL client: review against the needs of an Agent check

Files: `docker/freeswitch/bin/esl-check` (178 lines), `bin/esl_lab.py` (61 lines).

| Aspect | Current behaviour | Assessment for a long-running check |
|---|---|---|
| Authentication | `auth <password>`, requires `Reply-Text: +OK…`; rejection → fixed message, exit 2 | Correct protocol handling. Uses the **shared** password (`auth`), not `userauth`, so no least-privilege path exists yet. |
| Secret handling | Password read from `$ESL_PASSWORD_FILE` or named env var; never argv; fixed error strings; `except Exception` prints only the class name | Good. For the check, the secret must come from Agent config / secrets backend instead (§6). |
| Timeouts | One monotonic **overall deadline** per exchange; every `recv`/`send` uses the remaining time; connect uses the same timeout | Good and reusable. Needs separate configurable connect vs command timeouts. |
| Bounds | Header ≤ 8 KiB, body ≤ 1 MiB, `Content-Length` validated | Good. The check's commands return < 1 KiB, so a much smaller body cap (e.g. 64 KiB) is appropriate. |
| Message framing | Reads to `\n\n`, then `Content-Length` bytes; leftover buffer kept | Correct. Handles partial reads. |
| Unexpected messages | Wrong `Content-Type` → generic "unexpected response type" | A `text/disconnect-notice` (server closing, e.g. after ACL/auth failure) should be classified as connection loss, not a protocol error. |
| Connection reuse | None in `esl-check` (one command per process). `esl_lab.Conn` supports repeated `api()`; `esl-baseline` reconnects after a failure | A check needs this logic inside the check object: persistent connection, close on any error, bounded reconnect. After a **timeout the stream may be desynchronised** (late reply), so the connection must be discarded. `esl-baseline` already does this. |
| Auth/ACL | Server-side ACL `esl_lab` (default deny; `127.0.0.1/32` + lab subnet) | Fine for the lab. |
| Packaging | Hyphenated script files; `esl_lab.py` loads `esl-check` via `SourceFileLoader` | Not importable as a normal package. Must become `datadog_checks/freeswitch/esl.py` or similar. |
| Tests | No unit tests for `esl-check`/`esl_lab`; exercised only by `verify-lab.sh` (good/bad password, unreachable, health) | Needs a fake-server unit suite (§8). |
| Python | Lab image runs Python 3.11; Agent 7.84.2 embeds **3.13.15** | Syntax-compatible; re-test under 3.13. |
| Edge case found | `show channels as json` with 0 rows returns `{"row_count": 0}` (no `rows` key) | Already handled via `.get("rows", [])`; add an explicit test. |

### 2.4 Lab ESL exposure (facts, not assumptions)

- Compose publishes only 5060/udp+tcp and 16384–16483/udp on `127.0.0.1`; **8021 is not published** (verified in `docker-compose.yml`).
- `event_socket.conf.xml`: `listen-ip 0.0.0.0` *inside the container*, `password` from the Docker secret, `apply-inbound-acl esl_lab`.
- The ACL admits `127.0.0.1/32` and the whole `172.29.77.0/24` lab network. Any container on that network can attempt auth.
- Secret file: `secrets/esl_password`, mode 0644 inside a 0700 directory (so uid 999 in the container can read it). Not in `docker inspect`,
  process args or container env (verified in milestone 2).
- ESL is plaintext TCP. The 1.10.12 `mod_event_socket` source handles only `listen-ip`, `listen-port`, `password`,
  `apply-inbound-acl`, `nat-map`, `stop-on-bind-error`, `debug`; there is no TLS option. The listen backlog is 5.
- The lab's directory is empty, so **no `userauth` users exist**.
- Authenticated clients can run, among ~170 listed API commands: `system`, `bg_system`, `originate`, `hupall`, `uuid_kill`,
  `reloadxml`, `fsctl`, `sched_hangup`, `shutdown`, `unload` (verified with `help` on the lab; **not executed**).

---

## 3. Official Datadog requirements, with sources

Sources (all fetched 2026-10-11):

| ID | URL | Notes |
|---|---|---|
| S1 | https://docs.datadoghq.com/extend/integrations/agent_integration/ | Partner-oriented guide ("Datadog Partner Network", "Developer Platform"). |
| S2 | https://docs.datadoghq.com/extend/ | Community vs Marketplace; submission summary. |
| S3 | https://github.com/DataDog/integrations-extras (README) | Purpose, contributing, maintainers, BSD-3-Clause license file. |
| S4 | https://github.com/DataDog/integrations-extras/blob/master/CONTRIBUTING.md | Defers to integrations-core's CONTRIBUTING. |
| S5 | https://github.com/DataDog/integrations-core/blob/master/CONTRIBUTING.md | PR rules; "For new integrations … open a pull request in the integrations-extras repo". |
| S6 | https://github.com/DataDog/integrations-core/tree/master/docs/developer (rendered at https://datadoghq.dev/integrations-core/) | `setup.md`, `testing.md`, `guidelines/{conventions,pr,style}.md`, `meta/ci/validation.md`, `meta/config-specs.md`, `base/{basics,api}.md`. |
| S7 | https://docs.datadoghq.com/developers/integrations/check_references/ | manifest / metadata.csv / service_checks.json reference. **Describes the Marketplace-flavoured manifest** (`legal_terms`, `pricing`, `vendor_id`); does not match the real extras manifests. |
| S8 | https://docs.datadoghq.com/agent/guide/use-community-integrations/ | Installing community integrations. |
| S9 | https://docs.datadoghq.com/extend/custom_checks/write_agent_check/ | Custom check basics, collection interval, subprocess/threading warning. |
| S10 | https://docs.datadoghq.com/agent/configuration/secrets-management/ | `ENC[]`, backends, caveats. |
| S11 | https://github.com/DataDog/integrations-core/tree/master/ddev/src/ddev/cli/create/templates (`check`, `check_only`) | Current scaffold templates. |
| S12 | https://github.com/DataDog/integrations-extras/tree/master/.github and `.ddev/config.toml` | Extras CI: `validate.yml`, `pr.yml`, `CODEOWNERS`, `PULL_REQUEST_TEMPLATE.md`. |
| S13 | https://github.com/DataDog/integrations-extras/tree/master/exim | A real, current extras Agent check (manifest, pyproject, tree). |
| S14 | https://pypi.org/project/datadog-checks-base/ , https://pypi.org/project/ddev/ | Versions: `datadog-checks-base` 38.4.0, `ddev` 20.0.0 (`requires-python >=3.13`), `datadog-checks-dev` 40.0.1. |
| S15 | https://github.com/DataDog/datadog-agent/releases and `omnibus/config/software/python3.rb` | Latest Agent release 7.84.2 (2026-10-07); embedded Python `default_version "3.13.15"` (main branch). |
| S16 | FreeSWITCH 1.10.12 source tarball (`src/mod/event_handlers/mod_event_socket/mod_event_socket.c`) | `userauth`, `esl-allowed-*`. **Not** from public docs: the SignalWire docs page fetched did not contain the module reference. |
| S17 | https://github.com/DataDog/integrations-extras/pull/1609 | Prior FreeSWITCH PR (closed unmerged). |

### 3.1 Requirement matrix

| # | Topic | Requirement | Level | Source |
|---|---|---|---|---|
| 1 | Repo | Public community integrations are submitted as a PR to `integrations-extras`; the author "remains the active maintainer"; Datadog gives best-effort support | MANDATORY (process) | S2, S3 |
| 2 | Repo | Maintainers must say in the PR they will maintain it; if unable they must notify Datadog | MANDATORY (process) | S3 |
| 3 | Maintainer identity | README says maintainers are "listed in `manifest.json`"; the current `exim` manifest has no maintainer field and ownership is in `.github/CODEOWNERS` | UNCONFIRMED which is authoritative; CODEOWNERS entry is enforced (row 22) | S3 vs S12/S13 |
| 4 | Partner program | S1 says to join the Datadog Partner Network and create a Developer Platform listing. S3/S4/S5 never mention either for extras | **UNCONFIRMED** whether a community contributor needs this | S1 vs S3–S5 |
| 5 | Language/API | Agent v7+ checks must be Python 3; subclass `AgentCheck` from `datadog_checks.base`; implement `check(self, instance)` (current template uses `check(self, _)`) | MANDATORY | S1, S11 |
| 6 | Naming | Package name = check name; **never name a Python file the same as the integration** | MANDATORY (convention, stated as "Never") | S1, S6 `conventions.md` |
| 7 | Config errors | Raise `ConfigurationError` for missing/invalid settings | MANDATORY (documented API behaviour) / RECOMMENDED for message style | S1 |
| 8 | State | Parse config once in `__init__`; each `AgentCheck` is one YAML instance; prefix custom attributes with `_` | RECOMMENDED | S6 `conventions.md` |
| 9 | Metrics | Types available: gauge, count, monotonic_count, rate, histogram, historate. `__NAMESPACE__` prefixes every metric | RECOMMENDED (namespace "optional addition") | S6 `base/basics.md` |
| 10 | Service checks | "New integrations generally should not submit their own service checks. Prefer metrics and metric-based monitors instead." | RECOMMENDED against | S6 `base/basics.md` |
| 11 | Init | `check_initializations` runs once before first check; config errors should surface during the run phase | RECOMMENDED | S6 `base/basics.md` |
| 12 | Subprocess/threads | For *custom checks*: don't use `subprocess` or `multithreading`; use `get_subprocess_output()` | RECOMMENDED (S9, custom-check page) | S9 |
| 13 | Collection interval | Default `min_collection_interval` is 15 s; not a guarantee; slow runs cause skipped executions | Informational | S9 |
| 14 | Tests | Tests are "required to publish"; pytest + hatch; unit (`@pytest.mark.unit`) and integration (`@pytest.mark.integration`) | MANDATORY | S1, S2 |
| 15 | Test runner | `ddev test <name>`, `ddev test -m integration <name>`, `--lint/-s`, `--fmt/-fs`, `--cov/-c`, `ddev test <name>:<env>` | Commands documented | S1, S6 `testing.md` |
| 16 | Validation | Must pass `ddev validate`; extras CI runs `ddev validate all` with: `ci`, `config`, `imports`, `integration-style`, `jmx-metrics`, `legacy-signature`, `metadata`, `models`, `package`, `readmes`, `codeowners` | MANDATORY | S2, S12 |
| 17 | Validators *not* in extras' list | `manifest`, `service-checks`, `dashboards`, `license-headers` (explicitly "not implemented for extras") are absent from `.ddev/config.toml`; their schemas are still documented | Informational / UNCONFIRMED whether reviewers check manually | S12, S6 |
| 18 | CI | Extras `pr.yml` reuses `integrations-core` `pr-test.yml` with `repo: extras`, Python 3.13, Linux runners (`ubuntu-22.04` for validate) | MANDATORY (CI constraint) | S12 |
| 19 | Config spec | `assets/configuration/spec.yaml` is the source; `conf.yaml.example` and `config_models/` are generated and must stay synced (`ddev validate config --sync`, `ddev validate models --sync`); option descriptions < 120 chars/line; options may be flagged `secret: true` | MANDATORY (synced) | S6 `validation.md`, `config-specs.md` |
| 20 | Dependencies | New third-party deps must be pinned and compatible with the Agent's embedded environment (`ddev validate dep`) | MANDATORY *if* adding deps | S6 `validation.md` |
| 21 | Imports | Import base package as `from datadog_checks.base... import` | MANDATORY (`imports` validation) | S6 `validation.md` |
| 22 | CODEOWNERS | Every extras integration needs a `.github/CODEOWNERS` entry (`ddev validate codeowners`, extras only) | MANDATORY | S6 `validation.md` |
| 23 | Changelog | Every PR must add a changelog entry to each modified integration. Format in S6 `pr.md` for extras: `CHANGELOG.md` with `## Unreleased` and `***Added***:` blocks, each first line ending in the PR link. **But** the current `ddev` template ships `changelog.d/1.added` fragments (core style) | MANDATORY to add an entry; **UNCONFIRMED** which mechanism extras currently expects | S6 `pr.md`, S11 |
| 24 | PR hygiene | One concern per PR; squash-merge only; meaningful PR title; clean history; tests included; PR template checklist | MANDATORY (process) / RECOMMENDED | S5, S6 `pr.md`, S12 |
| 25 | Versioning | Bump version when functionality changes; release-note entry in Developer Platform (partner flow) | MANDATORY (partner flow); UNCONFIRMED for pure-GitHub flow | S1 |
| 26 | Scaffolding | `ddev create` templates: `check`, `check_only`, `event`, `jmx`, `logs`, `metrics_crawler`, (+ `tile`, `marketplace`). S1 shows `ddev create -t check_only <name> --skip-manifest` (partner flow, manifest from the Developer Platform) | Command names MANDATORY-ish (tooling); which template extras expects: **UNCONFIRMED** | S1, S11 |
| 27 | Packaging | Wheel via `ddev release build <name>` or `pip wheel . --no-deps --wheel-dir dist`; hatchling backend; `pyproject.toml` name must be `datadog-<folder>` (`package` validation); BSD-3-Clause, `Private :: Do Not Upload` classifier | MANDATORY | S1, S6, S11, S13 |
| 28 | Install | `sudo -u dd-agent datadog-agent integration install -w /path/x.whl` (local wheel, Agent ≥ 6.10); for released community versions `datadog-agent integration install -t datadog-<name>==<ver>` (Agent ≥ 7.21) | Documented | S1, S8 |
| 29 | Compatibility | Python 3.13 in CI/pyproject (`requires-python >=3.12` in template, classifier 3.13); `datadog-checks-base>=` pin in template (`>=37.33.0`; latest 38.4.0); Supported OS tags in manifest | MANDATORY (CI) | S11, S12, S14 |
| 30 | Metric budget | Public integrations' metrics don't count as custom metrics, "but integrations emitting potentially unlimited metrics may still be considered custom" | RECOMMENDED: keep cardinality bounded | S2 |
| 31 | Secrets | Agent resolves `ENC[handle]` in integration configs; values are held in memory, not written to disk; `configcheck` shows passwords as `<obfuscated_password>` | Documented behaviour; use for design | S10 |
| 32 | License | Extras root license is BSD-3-Clause; template/pyproject use `BSD-3-Clause`; scaffold headers say "3-clause BSD style license" (license-header validation not enforced for extras) | MANDATORY (BSD-3-Clause assumed; contributor agreement/CLA **UNCONFIRMED**) | S3, S11, S13 |
| 33 | Security review | No public document found describing a security review or security requirements specific to community integrations | **UNCONFIRMED** | – |

### 3.2 Distinction between integration kinds

| Kind | Where | Shipped with Agent? | Install | Relevant here? |
|---|---|---|---|---|
| **Built-in/core Agent checks** | `integrations-core` (Datadog-owned) and the Agent itself | Yes | Bundled | No. `integrations-core` redirects new integrations to extras (S5). |
| **Community integration** | `integrations-extras` | **No** ("not packaged with the Agent") | `datadog-agent integration install -t datadog-<name>==<ver>` (S8) or local wheel (S1); config in `conf.d/<name>.d/conf.yaml` | **Yes: our target** |
| **Marketplace offering** | `DataDog/marketplace` (needs access from a Datadog contact) | No | Marketplace | No |
| **Custom check** | Files in `checks.d/` + `conf.d/` on one host | n/a | Copy files | Useful for a first manual run (S9). Metrics are *custom metrics*. |

---

## 4. Required vs recommended file checklist

Layout is derived from the current `ddev` `check` template (S11) and a real extras check, `exim` (S13). Items marked †
exist in `exim` but are legacy (`setup.py`, `requirements-dev.txt`, `tox.ini` appear in the 2022 PR) and are not in today's template.

```
freeswitch/                                   # integration directory in integrations-extras
├── manifest.json                    MANDATORY  (manifest_version 2.0.0; app_uuid generated by ddev; tile, author, assets)
├── metadata.csv                     MANDATORY  (header + one row per metric; `ddev validate metadata`)
├── README.md                        MANDATORY  (`ddev validate readmes`; tile references README.md#Overview/#Setup/#Support)
├── CHANGELOG.md                     MANDATORY  (tile.changelog; extras PR rule S6)
├── pyproject.toml                   MANDATORY  (hatchling; name = datadog-freeswitch; dependency datadog-checks-base)
├── hatch.toml                       MANDATORY  (test matrix; `[env.collectors.datadog-checks]`)
├── assets/
│   ├── configuration/spec.yaml      MANDATORY  (source of conf.yaml.example + config_models)
│   ├── service_checks.json          CONDITIONAL (only if service checks are shipped; `manifest.assets.integration.service_checks.metadata_path`)
│   └── dashboards/*.json            OPTIONAL   (template includes an example; exim has none)
├── datadog_checks/
│   ├── __init__.py                  MANDATORY† (namespace package shim in extras examples)
│   └── freeswitch/
│       ├── __about__.py             MANDATORY  (__version__; read by hatch)
│       ├── __init__.py              MANDATORY  (exports __version__, FreeswitchCheck)
│       ├── check.py                 MANDATORY  (not named freeswitch.py: convention row 6)
│       ├── config_models/*.py       MANDATORY  (generated: `ddev validate models --sync`)
│       └── data/conf.yaml.example   MANDATORY  (generated from spec: `ddev validate config --sync`)
├── tests/
│   ├── __init__.py, conftest.py     MANDATORY  (`dd_environment`, `instance` fixtures)
│   ├── test_unit.py                 MANDATORY  (unit tests; fake ESL server)
│   ├── test_e2e.py                  RECOMMENDED (template ships one; needs a real environment)
│   └── fixtures/ , docker/          OPTIONAL   (captured ESL transcripts; compose for a real FreeSWITCH)
├── images/.gitkeep                  RECOMMENDED (template; README images must live here)
├── changelog.d/1.added              UNCONFIRMED for extras (template ships it; extras PR rule says CHANGELOG.md)
├── setup.py                         NOT in current template (present in older extras checks)
└── LICENSE                          UNCONFIRMED (exim has none; license declared in pyproject and root repo)
```

Repo-level additions outside the integration directory (in `integrations-extras`):

| Item | Level |
|---|---|
| `.github/CODEOWNERS` entry for `/freeswitch/` | MANDATORY (`codeowners` validation) |
| CI wiring (`ddev validate ci --sync` touches CI/coverage config) | MANDATORY (`ci` validation) |
| Docs-team review of `README.md` and `metadata.csv` (CODEOWNERS routes them to `@DataDog/documentation`) | Process |

### 4.1 manifest.json (2.0.0), fields seen in the real `exim` manifest (S13)

`manifest_version`, `app_uuid` (generated by `ddev create`, never hand-written per S7), `app_id`, `owner`, `display_on_public_website`,
`tile{overview,configuration,support,changelog,description,title,media,classifier_tags}`, `author{support_email,homepage,sales_email,name}`,
`assets.integration{source_type_name,configuration.spec,events.creates_events,metrics{prefix,check,metadata_path},service_checks.metadata_path,source_type_id,auto_install}`.

- S7 lists `legal_terms`, `pricing`, `vendor_id` as mandatory: those are Marketplace/partner fields and are **absent** from the real
  extras manifest. Treat S7 as partly out of scope; the generated scaffold is authoritative.
- `tile.title` ≤ 50 characters, `tile.description` ≤ 80 (S7).
- Classifier tags to choose: a `Category::…`, `Offering::Integration`, `Supported OS::Linux` (FreeSWITCH is Linux-focused; do not claim
  macOS/Windows without testing), `Queried Data Type::…`/`Submitted Data Type::Metrics` (the extras example uses a minimal set).
- The prior PR's reviewer requested `title: "FreeSWITCH"` and a one-line description ("Monitoring Agent for FreeSWITCH PBX server") (S17).

### 4.2 metadata.csv columns (S7, S11)

`metric_name,metric_type,interval,unit_name,per_unit_name,description,orientation,integration,short_name,curated_metric,sample_tags`.
Mandatory: `metric_name`, `metric_type`, `orientation` (1/0/−1), `integration` (normalised title, e.g. `freeswitch`), `short_name`.
Every submitted metric must appear (`assert_metrics_using_metadata` in the template's tests).

---

## 5. Proposed FreeSWITCH integration architecture

### 5.1 Shape

- Integration name `freeswitch`, package `datadog-freeswitch`, module `datadog_checks.freeswitch`, class `FreeswitchCheck(AgentCheck)`,
  `__NAMESPACE__ = 'freeswitch'`, metric prefix `freeswitch.`.
- Python **stdlib only** (`socket`, `time`, `re`), plus `datadog-checks-base`. No `subprocess`, no threads, no new dependency. Reviewer
  question to confirm: direct `socket` use is acceptable for a non-HTTP protocol (not found in the docs either way).
- Layering inside the package:
  1. `esl.py`: small protocol client (connect, `auth`/`userauth`, one `api` command, close). Derived from `esl-check`/`esl_lab` logic,
     stricter body cap, explicit disconnect-notice handling, typed exceptions (`EslConnectError`, `EslTimeout`, `EslAuthError`,
     `EslProtocolError`) with fixed messages that never contain the password or raw server text.
  2. `check.py`: configuration, connection lifecycle, command allowlist, parsing, metric submission.
  3. `config_models/` generated; `data/conf.yaml.example` generated from `assets/configuration/spec.yaml`.
- One `AgentCheck` instance per configured FreeSWITCH (the Agent's model); no shared global state between instances.

### 5.2 Per-run data flow (initial version)

```
check(self, _):
  t0 = monotonic()
  ensure connection (reuse; if none: connect + authenticate)        ← at most one reconnect attempt per run
  uptime   = api "uptime s"                                         → int seconds
  channels = api "show channels count"                              → "N total."
  submit gauges; can_connect = 1
  on any error: close connection, can_connect = 0, error counter with bounded `error_type`, never raise a secret
  submit collection.duration (monotonic) in all cases
```

- **Commands (allowlist, hard-coded):** `uptime s`, `show channels count`. Optional later: `status` for `sessions max`.
  Nothing else is sendable: the client method takes only members of a constant set, and tests assert this.
- **No call control, no events:** no `originate`, `bridge`, `uuid_*`, `hupall`, `fsctl`, `bgapi`, `sched_api`, `eval`, `system`; no
  `event`/`filter`/`log`/`nolog`/`myevents`/`linger` ESL commands in v1.
- **Connection reuse:** one persistent connection per instance (preferred); it is closed after any timeout, parse error, unexpected
  message or disconnect. Alternative (connection per run) costs ≈ 3–5 ms per run measured in the lab versus ≈ 0.6 ms for a reused connection
  (reference baseline, loopback, §8.3). With a 15–30 s interval both are tiny; **decide in Milestone 3 with data**, and keep the
  "stale connection after FreeSWITCH restart" test either way.
- **Timeouts:** configurable `connect_timeout` and `read_timeout` (defaults e.g. 3 s each, bounded by validation), plus an overall run
  deadline strictly below `min_collection_interval` so a hung server cannot cause skipped runs to pile up.
- **Polling:** use the Agent default `min_collection_interval` (15 s) or higher; 2 queries per run ≈ 8 queries/min per instance.
- **Backlog:** the lab's 1.10.12 ESL listener uses a backlog of 5; many monitors or fast polling against one FreeSWITCH are not a goal.

### 5.3 Credentials and ESL user

- Config option `password` (named exactly so, so the Agent's scrubber/`configcheck` treat it as a password; S10), `secret: true` in the spec.
  Recommend `password: ENC[...]` with the Agent's secrets backends (S10 lists `file.*` backends, Agent 7.70+) so plaintext does not
  have to live in `conf.yaml`. A dedicated `password_file` option is **not** proposed for v1 (unneeded if the file backend suffices; decide with reviewers).
- No environment variables for the secret; none on the command line (the check spawns no processes).
- **Least privilege:** configure a FreeSWITCH directory user with `esl-password` and
  `esl-allowed-api="uptime,show"` (and `esl-allowed-events` empty, `esl-allowed-log=false`) and have the check use
  `userauth user@domain:password`. Verified in 1.10.12 source: `userauth` reads `esl-password`, `esl-allowed-api`,
  `esl-allowed-events`, `esl-allowed-log` from user/group/domain params, and `bgapi`/`sched_api`/`eval`/`expand`/`xml_wrap` are
  re-checked. `show` as a command name would still permit *all* `show` subcommands (read-only, but larger output); acceptable, to be tested.
  Configuration option: `username` (optional, `user@domain`); when absent, plain `auth` (legacy shared password) with a documented warning.
- **Not verified:** that `userauth` + `esl-allowed-api` works end-to-end on the lab (the lab has no directory users). This is an explicit
  Milestone 3/5 experiment. Any lab change for it is a separate, approved step.

### 5.4 Why not other approaches (short)

| Option | Verdict |
|---|---|
| Custom check using `fs_cli` via subprocess | Rejected: spawns processes, puts secrets near argv/env, discouraged by S9. |
| ESL event subscription (`event plain …`) | Deferred: unbounded stream, backpressure, per-event cost; out of scope for v1 by requirement. |
| Parse FreeSWITCH logs / CDR | Different integration type (logs); out of scope. |
| HTTP (`mod_xml_rpc`/`mod_httapi`) | Not enabled by default on most installs; weaker auth story; not worth a second transport. |

---

## 6. Security and performance requirements (acceptance criteria)

### 6.1 Security

| ID | Requirement | How it is verified |
|---|---|---|
| SEC-1 | Never log, print, or include the password in exceptions, `warning()` messages, tracebacks, `repr()` of config/client objects | Unit: capture `caplog` + exception strings for every failure path; assert the sentinel password is absent; assert exception chains do not carry the auth line |
| SEC-2 | No secret in argv or environment; check spawns no subprocesses | Unit: patch `subprocess`/`os.system`/`os.environ` access to fail; static test greps source for `subprocess`, `os.environ`, `getenv` |
| SEC-3 | Only allowlisted commands can be sent | Unit: client rejects any other string; parametrised attempt list (`originate`, `hupall`, `uuid_kill`, `system`, `bgapi …`, newline-injected strings) |
| SEC-4 | Commands containing CR/LF are rejected before sending (protocol injection) | Unit |
| SEC-5 | Works with a restricted `userauth` user and fails *closed* (reports auth/permission error, does not escalate) | Integration against real FreeSWITCH with a directory user (Milestone 5) |
| SEC-6 | Auth failure is reported distinctly, does not trigger rapid retry loops | Unit: fake server returns `-ERR invalid` → one attempt per run, `error_type:auth` |
| SEC-7 | ESL port is not required to be public; docs state: loopback/private network only, ACL recommended | README review |
| SEC-8 | `password` marked `secret: true`; `configcheck` shows it obfuscated | Real-Agent check (Milestone 5) |
| SEC-9 | Bounded input: header ≤ 8 KiB, body ≤ 64 KiB, `Content-Length` validated, bounded retries | Unit: oversized/negative/non-numeric lengths |

Residual risks to document (not solved by the check): ESL is plaintext (no TLS in 1.10.12); anything on the allowed network
that has the password can run `system`; the shared-password `auth` mode grants full control; the Agent keeps resolved secrets in memory;
whether flares scrub every resolved value is not documented (S10); FreeSWITCH does not appear to rate-limit failed auth (unverified).

### 6.2 Performance / resource

| ID | Requirement | How it is verified |
|---|---|---|
| PERF-1 | ≤ 2 ESL commands and ≤ 1 connection per instance per run in steady state | Unit counter on fake server |
| PERF-2 | Run deadline < `min_collection_interval`; no blocking beyond configured timeouts | Unit with silent/slow server |
| PERF-3 | No threads, no unbounded buffers or caches; constant memory over N runs | Unit (tracemalloc over 1k runs) |
| PERF-4 | Bounded tag cardinality: only instance-constant tags; no UUIDs, caller IDs or destinations | Unit asserts tag sets |
| PERF-5 | Incremental FreeSWITCH overhead measured, not promised (§8.3) | Milestone 6 |

Objective is **very low** overhead, not "zero". No overhead claim is made from the laptop baseline.

---

## 7. Metric definitions and proposed service checks

Command semantics verified on the lab (FreeSWITCH 1.10.12, 2026-10-11, idle and with one bounded harness call):

| Command | Idle | During 1 harness call | Format notes |
|---|---|---|---|
| `api uptime s` | `3401` | increases | single integer + newline |
| `api show channels count` | `\n0 total.\n` | `\n4 total.\n` | leading blank line; regex `(\d+) total\.` |
| `api show calls count` | `\n0 total.\n` | `\n3 total.\n` | **3 rows for 1 bridged call** (reference run: 0/3/15/30 for 0/1/5/10 calls) |
| `api status` | `UP 0 years, … ` + `N session(s) - peak P` + `S session(s) max` | `4 session(s)` | human text, brittle; includes sessions since startup/peak/max |
| `api show channels as json` | `{"row_count": 0}` (no `rows` key) | `{"row_count":4,"rows":[…]}` | rows carry UUIDs/numbers: **do not** collect (cardinality, call-sensitive) |
| unknown command | `-ERR <cmd> Command not found!` | | must be treated as failure |

### 7.1 v1 metrics (proposal)

| Metric | Type | Unit | Source | Notes |
|---|---|---|---|---|
| `freeswitch.can_connect` | gauge 1/0 | – | whole run | 1 only if connected, authenticated and both commands parsed this run. Preferred availability signal (S6 guidance). |
| `freeswitch.uptime` | gauge | second | `uptime s` | Drops on restart; monitors can alert on a decrease. |
| `freeswitch.channels.active` | gauge | – | `show channels count` | **Channels, not calls.** Includes both legs of loopback pairs and each leg of real calls (typically 2 per bridged call). Documented in README. |
| `freeswitch.collection.duration` | gauge | second | monotonic | Whole run, including connect; internal diagnostics. The Agent also reports check run time in `status`. |
| `freeswitch.collection.errors` | count | – | failures | Always submitted (0 on success so the series exists). Tag `error_type:connect\|timeout\|auth\|protocol\|parse` (5 fixed values). |

Tags: instance-constant only: `freeswitch_host:<host>`, `freeswitch_port:<port>`, plus user `tags`. No per-channel or per-call tags.

Deferred (need their own evidence and are **not** in v1): `sessions.max` / `sessions.active` (from parsing `status`), calls, per-profile
SIP/registration counts, per-second session rate, version metadata (`version short` → `set_metadata`, low risk, candidate for v1.1).
Not planned: anything derived from `show calls` row counts, unless FreeSWITCH call semantics are defined and verified first.

### 7.2 Service check

- **Open decision.** Datadog guidance discourages new service checks (§3.1 row 10). If reviewers want one: `freeswitch.can_connect`
  with `ok`/`critical`, message limited to fixed strings, `groups: ["freeswitch_host","freeswitch_port"]`, declared in
  `assets/service_checks.json`; the prior 2022 PR shipped one and reviewers only edited its description wording (S17).
- Recommendation: ship the **gauge only** in v1, ask reviewers on the PR whether a service check is wanted.

---

## 8. Testing and validation strategy

### 8.1 Unit tests (upstream-runnable, no FreeSWITCH)

A tiny threaded TCP **fake ESL server** fixture scripts server behaviour per test. Required cases (mapped from the brief):

| Area | Cases |
|---|---|
| Timeouts | connect to blackhole/closed port; server accepts but never sends greeting; hangs after `auth`; hangs after `api`; slow-drip bytes; overall deadline respected |
| Invalid credentials | `-ERR invalid` on `auth`/`userauth`; disconnect-notice after bad auth; no retry storm; `error_type:auth`; password absent from logs/exceptions |
| Unreachable / restart | server down at start; server dies mid-run; server restarts between runs (stale socket) → one reconnect, then healthy; `can_connect` 0→1 |
| Malformed responses | wrong `Content-Type`; missing/negative/huge/non-numeric `Content-Length`; truncated body; extra unsolicited frames; non-UTF-8; `-ERR` replies; `{"row_count":0}`-style edge cases (if JSON ever used); parse failures for `N total.` and uptime |
| Recovery | fail N runs then succeed; verify connection is reset after each failure kind; no leaked sockets (track open fds) |
| Multiple instances | two instances, one failing: independent state, tags, metrics, errors; different ports/credentials |
| Redaction / secrets | sentinel password never in `caplog`, exceptions, `repr`, metric tags, service check messages |
| Command safety | only allowlisted commands reach the wire (assert on server transcript); CR/LF rejection |
| Bounded resources | per-run command and connection counters; tag sets constant; memory stable |
| Metric contract | `assert_metrics_using_metadata`, `assert_all_metrics_covered`, `assert_no_duplicate_metrics` |

### 8.2 Integration / e2e

- **Local-only** integration tests against the lab's real FreeSWITCH (marker `integration`), exercising a restricted `userauth` user,
  real restarts and 1/5/10 harness calls with exact expected channel counts (4N).
- **Upstream CI caveat:** extras tests use `docker_run` + a `docker-compose.yml` from the integration's tests. There is no
  public FreeSWITCH image, and building one from source (~3 min, ~330 MB) is probably unacceptable for upstream CI. Plan on upstream
  e2e being skipped or using the fake server; confirm with reviewers (§11).
- Real Agent: `datadog-agent check freeswitch` and `datadog-agent status` against the lab (Milestone 5).

### 8.3 Overhead methodology (monitoring disabled vs enabled)

Reuse `run-baseline.sh`/`esl-baseline` with these controls:

1. **Arms** (per scenario ∈ {idle, 1, 5, 10 calls}): **A** = FreeSWITCH only (no Agent); **B** = Agent running, check disabled
   (isolates the Agent's own cost from the check's); **C** = Agent running, check enabled. Incremental check overhead = C − B (and C − A
   for the total). Agent telemetry must be disabled/pointed at a local sink; this audit sent nothing anywhere.
2. **Order/memory effect** (observed in the baseline: idle RSS 35.5 → 56.7 → 58.9 MiB across repetitions): **restart the FreeSWITCH
   container before every measured run** and run the same warm-up; interleave arms in a balanced order (e.g. ABCCBA) rather than all
   of one arm first; use ≥ 5 repetitions per cell (baseline stdev was large for 1 s peaks); analyse **paired** differences.
3. **Host load:** record `loadavg` and VM load at start/end of every run; abort/discard runs when host load exceeds a threshold
   (e.g. 1-min load > 50 % of cores), keep the host otherwise idle, and report the dispersion. The reference run's start loadavg was 2.3
   (smoke run: 6.8), so this control matters.
4. **Metrics:** FreeSWITCH process CPU (counter delta ÷ monotonic elapsed), FreeSWITCH RSS and container working set (separately),
   threads/fds, ESL connections observed, ESL latency series with the check *also* polling (it is an additional client), harness counters,
   plus the Agent's own check run time/CPU.
5. **Same everything else:** image id, config checksum, CPU/memory limits (none today; consider setting explicit limits for the
   comparison), poll interval, warm-up and window.
6. Report effect sizes with variability. No "overhead guarantee" is made from a single laptop VM.

### 8.4 Validation commands (all UNRUN in this audit; `ddev` is not installed)

```bash
# one-time (needs approval; Python 3.13 + pipx)
pipx install ddev && ddev config set repo extras          # S1
# from the extras checkout
ddev create -t check freeswitch                            # or -t check_only; see §3.1 row 26
ddev test freeswitch                                       # all tests (S1)
ddev test -m integration freeswitch                        # S1
ddev test -s freeswitch                                    # lint only (S6 testing.md)
ddev test -c freeswitch                                    # coverage (S6)
ddev validate config --sync freeswitch                     # S6 validation.md
ddev validate models --sync freeswitch
ddev validate metadata freeswitch
ddev validate readmes freeswitch
ddev validate ci --sync
ddev validate codeowners
ddev validate all changed                                  # what extras CI runs (S12)
ddev release build freeswitch                              # wheel (S1)
```

---

## 9. Installation and distribution options

| Option | Use | Source | Notes |
|---|---|---|---|
| Local wheel on a host | Development and Milestone 5 | S1 | `sudo -u dd-agent datadog-agent integration install -w <wheel>`; config at `/etc/datadog-agent/conf.d/freeswitch.d/conf.yaml`; restart Agent. |
| Custom-check copy (`checks.d/`) | Quick first run before packaging | S9 | Prefix naming rules for custom checks; metrics count as custom. Not a distribution path. |
| Public community install | Post-merge, end users | S8 | `datadog-agent integration install -t datadog-freeswitch==<version>` (Agent ≥ 7.21). |
| Containerised Agent | Docker/K8s | S8 | Build an image with `agent integration install -r -t datadog-freeswitch==<ver>`, or Helm/Operator custom tag. Persistence across Agent upgrades on hosts is **not documented** (S8). |
| Outside Datadog (own PyPI/GitHub release) | Fallback while unmerged | – | `pyproject.toml` carries `Private :: Do Not Upload`; direct wheel distribution only. |

Distribution independent of our Docker lab/CommFoundry/Ansible: the check needs only a reachable ESL endpoint, the FreeSWITCH user
configuration and the Agent. The README must say so and must not reference the lab.

---

## 10. Incremental implementation plan

Each milestone is a separate task with its own approval.

### M1: Repository audit and requirements: **this document**
- Scope: research, repo assessment, plan. Files: `docs/INTEGRATION_REQUIREMENTS_AUDIT.md`.
- Acceptance: report reviewed; open decisions answered (§12). Tests: none (no code).
- Risks: some requirements UNCONFIRMED (§3.1).

### M2: Scaffolding and metadata
- **Scope:** install `ddev` (isolated), generate the scaffold, fill manifest/metadata/spec with the §7 metrics, generate example config and
  models; no check logic.
- **Files:** new `freeswitch/` tree (§4) inside this repo (mirrors extras layout) or in an extras fork; `docs/` note of template choice.
- **Acceptance:** `ddev validate config|models|metadata|readmes|package|imports|legacy-signature` pass on the empty check;
  `ddev test` runs the template test; wheel builds.
- **Tests/commands:** §8.4. **Prerequisites:** Python 3.13, pipx, ddev (approval to install), decision D1–D5 (§12).
- **Risks:** template choice (`check` vs `check_only`), changelog mechanism, `source_type_id` assignment, maintainer/author identity.

### M3: Minimal ESL client and Agent check
- **Scope:** `esl.py` + `check.py` implementing §5 (two commands, reuse, timeouts, redaction, error taxonomy); option `username`/`password`.
- **Files:** `freeswitch/datadog_checks/freeswitch/{esl.py,check.py,__init__.py}`, spec, generated models/example.
- **Acceptance:** emits exactly the §7 metrics against the lab (counts 0/4/20/40 for 0/1/5/10 calls); never sends non-allowlisted commands.
- **Tests:** fake-server smoke tests plus manual `datadog-agent check`. **Prerequisite:** M2; lab running.
- **Risks:** direct socket use acceptable?; `userauth` experiment requires a directory user in a FreeSWITCH config (separate approved lab change).

### M4: Unit and failure-path tests
- **Scope:** full §8.1 suite, coverage, lint.
- **Files:** `freeswitch/tests/{conftest.py,test_unit.py,fixtures/*}`.
- **Acceptance:** all SEC/PERF unit criteria pass; `ddev test freeswitch` and lint green on Python 3.13; coverage report reviewed.
- **Risks:** fake server fidelity vs real FreeSWITCH (mitigate with captured transcripts from the lab).

### M5: Local installation and real-Agent verification
- **Scope:** build wheel, install into a real Agent (container or host), run `datadog-agent check`/`status`/`configcheck`, restricted ESL user,
  restart/failure drills on the lab.
- **Files:** docs/runbook only; possibly a non-default compose profile for the Agent (separate approval).
- **Acceptance:** metrics visible in check output; `password` obfuscated in `configcheck`; failure modes behave as in §8.1.
- **Prerequisites:** M3–M4; an Agent (needs an API key *or* local-only `check` runs; decide telemetry policy first).
- **Risks:** Agent API key/telemetry; Agent image arm64 vs amd64.

### M6: Controlled overhead benchmark
- **Scope:** §8.3 arms A/B/C with restart-per-run and paired analysis; extend `run-baseline.sh` minimally (container restart, arm label).
- **Files:** `scripts/run-overhead.sh` (or options on `run-baseline.sh`), `esl-baseline` report additions, `results/overhead/`.
- **Acceptance:** ≥ 5 reps/cell, host-load log, paired deltas with variability, no overhead guarantee claimed.
- **Risks:** laptop noise; Agent cost dominating; telemetry must stay local.

### M7: Documentation, packaging, submission prep
- **Scope:** README (overview/setup/metrics/FreeSWITCH-side user config/troubleshooting), CHANGELOG, optional dashboard, wheel, PR text, CODEOWNERS entry.
- **Acceptance:** `ddev validate all changed` clean in an extras checkout; fresh-host install from wheel documented and tested; only then claim readiness.
- **Risks:** reviewer feedback (docs wording, service check), Partner-program question (§3.1 row 4), upstream e2e image.

---

## 11. Risks, limitations and unresolved questions

| # | Item | Impact | Resolution path |
|---|---|---|---|
| R1 | Is Partner Network/Developer Platform enrolment required for a community extras PR? (S1 vs S3–S5) | Process blocker | Ask on a draft PR or via Datadog contact before M7 |
| R2 | Which scaffold/changelog format does extras expect today (`check` vs `check_only`; `CHANGELOG.md` vs `changelog.d`)? | Rework | Run `ddev create` inside an extras checkout in M2 and follow its output; confirm in draft PR |
| R3 | No public FreeSWITCH test image | Upstream e2e not possible | Fake-server unit tests upstream; local integration tests |
| R4 | `userauth`/`esl-allowed-api` not yet exercised on the lab | Security design unproven | M3/M5 experiment (needs approved lab config) |
| R5 | Service check vs gauge | Review churn | Gauge-first; ask reviewers |
| R6 | Direct `socket` use in a check | Review churn | Ask reviewers; fall back to documented pattern if they object |
| R7 | Overhead measured on one laptop VM | Over-claiming | Controls in §8.3; no guarantee |
| R8 | Prior PR #1609 by another author (closed, unmerged) | Naming/credit/duplicate | Reference in the PR description; the name is free on `master` today |
| R9 | `configcheck`/flare scrubbing of resolved secrets not documented (S10) | Residual secret exposure | Verify empirically in M5 |
| R10 | FreeSWITCH versions: only 1.10.12 tested; 1.11.x exists | Compatibility claims | Document tested versions; test 1.11.3 before claiming support |
| R11 | Only Linux x86-64/arm64-Docker evidence; Windows/macOS not applicable | Manifest OS tags | Declare `Supported OS::Linux` only |
| R12 | Uncommitted milestone 3–4 work | Reproducibility | Maintainer to commit (not done here) |
| R13 | ddev/Python 3.13 not installed here | No validation evidence | M2 prerequisite |
| R14 | S7 manifest reference is Marketplace-oriented | Wrong fields if followed literally | Use the generated scaffold and the `exim` example |

Not verified in this audit: any `ddev` command; Agent behaviour; `userauth` on the lab; FreeSWITCH documentation pages for ESL
(source used instead); extras CI runtime constraints beyond workflow files; whether the 2022 PR's reviewer feedback was exhaustive.

---

## 12. Exact recommended next task

**Milestone 2: Correct integration scaffolding and metadata (no check logic).**

Decisions needed from the maintainer before starting:

| ID | Decision | Recommendation |
|---|---|---|
| D1 | Approve installing Python 3.13, `pipx`, `ddev` (isolated, user-level) | Yes, via `pipx`; nothing global |
| D2 | Commit the current milestone 1–4 working tree first | Yes (reproducibility) |
| D3 | Develop in this repo under `freeswitch/` (extras layout) or in a fork of `integrations-extras` | Fork + keep this repo as the lab; copy/sync the directory |
| D4 | Availability signal: gauge only vs gauge + service check | Gauge only for v1 |
| D5 | Author/support identity in `manifest.json` (name, email) and maintainer GitHub handle for CODEOWNERS | Maintainer to provide |

Task statement for M2:

1. Install `ddev` with `pipx` under Python 3.13; `ddev config set repo extras`; clone an `integrations-extras` fork.
2. `ddev create -nt check freeswitch` (dry run) then `ddev create -t check freeswitch`; record exactly which files/templates the current
   tool produces versus §4 and fix this document's checklist accordingly.
3. Populate `manifest.json`, `metadata.csv`, `assets/configuration/spec.yaml` (options: `host`, `port`, `username`, `password` with
   `secret: true`, `connect_timeout`, `read_timeout`, `tags`, `min_collection_interval`), then `--sync` generated example/models.
4. Keep `check.py` as an inert stub submitting nothing.
5. Run and report: `ddev validate config|models|metadata|readmes|package|imports|legacy-signature|ci|codeowners`, `ddev test freeswitch`,
   `ddev test -s freeswitch`, `ddev release build freeswitch`.
6. Do not modify the lab, the Docker image or any existing baseline results.

**Stop after this audit; implementation waits for approval.**
