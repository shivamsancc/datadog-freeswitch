# Milestone 5 report: production readiness and Linux compatibility

| | |
|---|---|
| Date | 2026-10-12 |
| Result | Fail-closed policy implemented and tested; the integration installed with the official installer into a disposable **Linux** Agent 7.80.3 and ran offline; docs rewritten for Linux operators; all executed validations pass. Submission is still blocked on identity decisions (§9). |
| Commits / pushes / PRs | None |

## 1. Git status

| Repository | Initial | Final |
|---|---|---|
| Lab (`~/Desktop/Zoos/datadog-freeswitch`, `main` @ `e67d043`) | `?? docs/MILESTONE_2_REPORT.md`, `?? docs/MILESTONE_3_REPORT.md`, `?? docs/MILESTONE_4_REPORT.md` | the same three plus `?? docs/MILESTONE_5_REPORT.md`. No tracked file changed |
| Extras checkout (`~/Desktop/Zoos/integrations-extras`, shallow clone of upstream `3084660`) | ` M .github/CODEOWNERS`, ` M .github/workflows/test-all.yml`, `?? freeswitch/` | identical entries; contents of `freeswitch/` changed as in §2 |

Existing uncommitted work was preserved.

## 2. Files changed (extras checkout, `freeswitch/`)

| File | Why |
|---|---|
| `datadog_checks/freeswitch/esl.py` | Fail-closed account policy (`_require_restricted_account`), duplicate/raw header tracking for it |
| `datadog_checks/freeswitch/check.py` | The fixed refusal reason is now part of the (rate-limited) warning |
| `tests/fake_esl.py` | Fake server can advertise `Allowed-API` values / custom `userauth` replies |
| `tests/test_unit.py` | 26 new tests (137 total) |
| `README.md` | Rewritten for Linux operators: requirements, install, FreeSWITCH-side user, config, secrets, validation, troubleshooting, limits |
| `assets/configuration/spec.yaml` and generated `data/conf.yaml.example` | `username` now states the refusal policy |

Models, metadata and manifest were not changed. Lab: this report only.

## 3. Fail-closed policy

### Evidence (FreeSWITCH v1.10.12 `mod_event_socket.c`, re-fetched this session)

- `char api_reply[512] = "Allowed-API: all\n";` (line 1769): a successful `userauth` for an account **without** `esl-allowed-api` is answered with `Allowed-API: all`.
- Lines 1903–1938: when the parameter is set, the reply carries `Allowed-API: <the configured string>` and the **same string** builds the permission hash (split on a space if the value contains one, else on commas, entries matched exactly).
- So the server *does* advertise effective API permission for `userauth` (it is the value actually enforced). M4 observed `Allowed-API: uptime,show` from the real lab for the restricted account.
- The shared-password `auth` reply is just `+OK accepted`: **no permission information exists**, so the policy cannot and does not apply to it.

### Traps found in the source, and how the policy handles them

| Trap | Handling |
|---|---|
| The advertised line is formatted into a 512-byte buffer; a very long list is truncated, and padding could hide a later entry (for example `system`) | The raw header value must be exactly one space plus the stripped value; no trailing or other padding; no empty entries |
| `esl-allowed-api` set to the literal `all` is advertised as `all` but would actually allow only a command named `all` | Refused either way (fail closed) |
| `uptime, show` (comma and space) is split on the space by the server into `uptime,` and `show`, so `uptime` would be denied | Entries are split exactly like the server and compared exactly; refused |
| Header absent or duplicated (an XML attribute can carry a newline) | Refused |
| Case: server hash is case-sensitive | Exact, case-sensitive comparison |

### Policy implemented

With `username` set, after `+OK accepted`, the check requires the advertised permissions to be **exactly the set `{uptime, show}`**. Anything else (`all`, absent, empty, a subset, extra commands, ambiguous) raises `EslAuthError` → `error_type:auth`, the connection is closed, **no command is sent** and there is no fallback to `auth`. The log gives a fixed reason ("account is not restricted to the API commands this check uses"), rate-limited as before.

Deliberate strictness: an account with extra read-only commands (for example `status`) is also refused, because the account is meant to be dedicated to this check. There is no override option in v1.

Not claimed: FreeSWITCH matches the **first word** only, so `uptime,show` still allows every `show` subcommand and every `uptime ...`. The check's own two-command allowlist is unchanged and independent. `Allowed-Events` / `Allowed-LOG` are not checked because the check never subscribes to events or logs.

### Tests (all pass)

Accepted: `uptime,show`, `show,uptime`, `uptime show`, `show uptime`, `uptime,uptime,show`. Refused with no `api` line sent and only a `userauth` line on the wire: `all`, `ALL`, header absent, empty, `uptime`, `show`, `uptime,show,status`, `uptime,show,system`, `uptime, show`, `uptime,,show`, trailing/leading comma, `Uptime,show`; trailing-padded, double-space, missing-space and duplicated headers. Plus: shared-password mode unaffected, reason logged once without secrets or username, client allowlist still enforced.

### Live-server limitation

The lab has no `userauth` users (restored in M4) and this milestone is read-only for the lab, so the **`all` and absent cases were verified from the source and the fake server, not against a live FreeSWITCH**. The restricted case (`uptime,show`) was observed live in M4.

## 4. Linux compatibility

### Environment (disposable, no production contact)

| Item | Value |
|---|---|
| Image | `datadog/agent:7.80.3` (Docker Hub, digest `sha256:a0e8aa6f…`, pulled this session, 1.18 GB) |
| OS / arch | Ubuntu 24.04.4 LTS, aarch64 |
| Agent | 7.80.3 (commit `328e400df8`), embedded Python 3.13, `datadog-checks-base 37.38.0` |
| Isolation | Agent container on a Docker `--internal` network with a relay container as its only peer; a probe to `1.1.1.1:443` from the Agent container was blocked. No API key; `dd_url` pointed at loopback; telemetry off |
| Lab access | The relay container (on the lab network, source address inside the ACL) forwarded TCP to the lab's `freeswitch:8021`. The lab container, its network and config were not modified |
| Requirements derived | `requires-python >=3.13`, `datadog-checks-base>=37.36.0`, stdlib only, Linux (manifest `Supported OS::Linux`) |

### Installation actually performed

| Step | Result |
|---|---|
| `datadog-agent integration install -w <wheel>` as `dd-agent` | Failed in this image: `site-packages` is root-owned (pip fell back to a user install, then the configuration move failed). Image quirk, not an integration defect |
| `agent integration install -r -w <wheel>` as root in the disposable container (the documented container route) | **Success**: "Successfully completed the installation of datadog-freeswitch"; `conf.yaml.example` copied into `conf.d/freeswitch.d/`; `integration show` reports 0.0.1 |
| `agent configcheck` | Exit 0; every instance shows `password: "********"`; the lab shared password, the test strings and `ENC[` do not appear |
| `agent check freeswitch --json` | Exit 0; results below |

Passwords came from `secret_backend_type: file.json` (0600 file, `ENC[handle]`); none were in argv or the image.

### Results in the Linux Agent

| Scenario | `can_connect` | errors | `error_type` | uptime / channels emitted |
|---|---|---|---|---|
| Real lab, shared password (valid) | 1 | 0 | none | real uptime, channels 0 |
| Real lab, wrong password | 0 | 1 | `auth` | none |
| Unreachable port | 0 | 1 | `connect` | none |
| Server never replies (1 s timeouts) | 0 | 1 | `timeout` | none |
| Malformed ESL | 0 | 1 | `protocol` | none |
| `userauth`, advertises `uptime,show` (fixture server) | 1 | 0 | none | fixture values |
| `userauth`, advertises `all` (fixture server) | 0 | 1 | `auth` | none |
| `userauth`, header absent (fixture server) | 0 | 1 | `auth` | none |

`freeswitch.collection.duration` was submitted in all cases. The `userauth` scenarios use fixture servers because the real lab has no restricted account in this milestone.

Egress note: the Linux Agent itself tried to reach external names (NTP pool, `process.datadoghq.com`) as default Agent behavior; they failed because the network is internal. Nothing left the container network.

Not done: Agent on a non-containerised Linux host (systemd/package install, `sudo -u dd-agent` flow) and x86-64. Those remain unverified.

## 5. Installation and configuration UX review

The README now covers all six items asked for (install, config location `/etc/datadog-agent/conf.d/freeswitch.d/conf.yaml`, host/port/auth/timeouts table, `ENC[]` with the file backend, `configcheck` / `check` / `status`, and a troubleshooting table per `error_type`). It states the DNS limitation plainly (IP addresses avoid it; the check cannot interrupt an in-progress lookup; DNS is not claimed to be bounded), the first-word permission limit, and that shared-password mode is full control. The README install line (`-t datadog-freeswitch==<VERSION>`) cannot be exercised until a version is published; only the `-w` wheel route was tested. `ddev validate readmes/config/models` pass and generated files were regenerated by `--sync`.

## 6. Code review: reliability and security

Reviewed `esl.py`, `check.py`, tests. Findings:

| Area | Assessment (code inspection plus tests) |
|---|---|
| Allowlist | Exact-match set inside `EslClient.api`; CR/LF rejected; 21 rejected strings tested; the check has no other send path |
| Frames/timeouts | Header ≤ 8 KiB, body ≤ 64 KiB, strict digit-only length, per-message deadline, run deadline; every socket operation sets a timeout |
| Cleanup/recovery | Every client error closes the socket; reused connection gets one fresh attempt; fresh failures are not retried; no loops |
| Secrets | Never in exceptions (fixed messages, `from None`), logs, tags, `repr`; sentinel tests including a server that echoes the password; policy log does not include the username |
| Cardinality | Tags: host, port, user tags, plus `error_type` from a closed set of 5 |
| Logging | Warn on first failure and on type change, one reminder per 15 min, debug for repeats, info on recovery |
| Multi-instance | Independent client/state per check instance; tested (also 8 instances in the Linux Agent) |
| Process/network | No subprocess, no threads, no `os.environ`, no external calls (source scan test); the only network operation is the configured ESL connection |
| Call control | None possible: no command outside two fixed strings; no events, no `bgapi` |

No defects found that required code changes beyond the policy. Known remaining items: hostname lookup not interruptible; refusal is strict (no opt-out); the refusal and permission-denied cases share `error_type:auth` (the log distinguishes them).

## 7. Workload and overhead assessment

**Inspection (what the code does per run, steady state):** one non-blocking `select`, two small requests (`api uptime s`, `api show channels count`), two replies of a few bytes, five metric submissions. At the default 15 s interval that is about 8 ESL commands per minute and a single persistent TCP connection. A new connection (after a restart or failure) adds one TCP handshake and one `auth`/`userauth` exchange. Worst case in a failing run is bounded by `connect_timeout + 2 × read_timeout` and 90 % of the interval, with at most two connection attempts.

**Measured in this milestone (client side only, fake server in the same process, development Mac, so it is an upper bound for the Python side and says nothing about FreeSWITCH):**

| Measurement | Value |
|---|---|
| Steady-state run, reused connection | 0.165 ms wall, 0.153 ms process CPU (includes the fake server's threads) |
| Run with a new connection each time | 0.370 ms wall |
| Python allocation peak across 2000 runs | 272 KiB (`tracemalloc`), no growth in threads or connections |

**Not measured:** FreeSWITCH-side cost of `uptime s` and `show channels count` (the latter reads the core channel table; its cost presumably grows with the number of channels, which is **inference**, not measured), Agent-side scheduling overhead, or any effect on call quality. No overhead benchmark was run and no overhead claim is made. Earlier milestone figures (ESL command latency with a reused connection ≈ 0.6 ms, new ≈ 3–5 ms on loopback) came from the Milestone 1 baseline tooling and are not repeated here.

## 8. Validation (exact statuses; final code, run from `~/Desktop/Zoos/integrations-extras`)

| Command | Result | Exit |
|---|---|---|
| `ddev -e validate config --sync freeswitch` / `models --sync` | regenerated / in sync | 0 / 0 |
| `ddev -e validate config|models|metadata|readmes|package|imports|legacy-signature freeswitch` | passed | 0 each |
| `ddev -e validate codeowners` | passed | 0 |
| `ddev -e validate all freeswitch` | 11 of 11 passed | 0 |
| `ddev -e test freeswitch` | **137 passed**, 2 warnings (generated-model Pydantic deprecation) | 0 |
| `ddev -e test -c freeswitch` | 94 % total; `esl.py` 93 % | 0 |
| `ddev -e test -fs freeswitch` (format, ruff fix) | no changes at the end | 0 |
| `ddev -e test -s freeswitch` (ruff format, ruff check, mypy) | no issues in 13 files | 0 |
| `uv build --wheel` | `datadog_freeswitch-0.0.1-py3-none-any.whl`, 13 files; SHA-256 identical to the wheel installed in the Linux Agent | 0 |
| Linux Agent install / `configcheck` / `check` | see §4 | success / 0 / 0 |

Not run: `ddev validate all changed` (shallow clone, no base), upstream CI, e2e tests (none exist), Linux host install via package manager.

## 9. Remaining blockers and limitations

| ID | Item | Type |
|---|---|---|
| B1 | Public support email (`manifest.author.support_email` is `TODO-maintainer-support-email@example.invalid`) | Maintainer decision |
| B2 | Public maintainer GitHub handle for `.github/CODEOWNERS` (entry lists only `@DataDog/ecosystems-review`) and which account | Maintainer decision |
| B3 | Fork of `DataDog/integrations-extras`; nothing can be pushed or proposed without it | Maintainer action |
| B4 | Reviewer-confirmed items: `manifest.owner`, `source_type_id`, changelog form (`## 1.0.0 / date` + PR link), service check vs gauge, `hatch.toml` `mypy-deps` workaround, whether partner enrolment is required | Upstream review |
| B5 | Linux evidence is from a containerised Agent (aarch64); package-installed Agents, x86-64 and other FreeSWITCH versions (only 1.10.12 tested) are unverified | Evidence gap |
| B6 | `userauth` refusal cases (`all`, absent) not exercised on a live FreeSWITCH | Evidence gap (requires an approved lab change) |
| B7 | First-word-only `esl-allowed-api`; shared-password mode unrestrictable; plain-text ESL | Inherent / documented |
| B8 | Hostname resolution cannot be interrupted | Documented |
| B9 | Default install route `-t datadog-freeswitch==<VERSION>` untestable until published | Release |
| B10 | A forwarding Agent (API key, flush, `count` aggregation) was not tested, by policy | Policy |

## 10. Confirmations

- **No calls generated:** no `run-call-load.sh`, SIP or synthetic load was run. The only commands the lab saw were `uptime s` and `show channels count` (and `esl-check` health calls), all read-only.
- **FreeSWITCH not restarted:** the container (`started 04:25:56Z`, from the Milestone 4 `docker start`) shows `RestartCount=0`, healthy, same start time before and after this milestone. Lab `conf/` SHA-256 hashes still match the Milestone 4 backup.
- **No production Agent modified:** `/opt/datadog-agent` (pid 282 still running) shows no changed files against a timestamp taken before the work.
- **No external telemetry sent:** no API key was configured; the Linux test ran on an internal-only Docker network (egress verified blocked); the macOS Agent copy was not used this milestone. The only external action was pulling the `datadog/agent:7.80.3` image from Docker Hub (a download, not telemetry).
- **Nothing committed, pushed, published or opened as a PR.**

Cleanup: both test containers (`m5-agent`, `m5-relay`) and the `m5-agent-net` network were removed; their secrets files went with the container. Left in place as instructed: `~/dd-test-agent`, `~/.lab-m4-backup`, `~/.config/ddev-fs-extras.toml`. Also left: the 1.18 GB `datadog/agent:7.80.3` image (remove with `docker rmi datadog/agent:7.80.3`) and scratch files under `/tmp/m5` (wheel, fixture scripts; no secrets).

## 11. Objectives

| Objective | Status |
|---|---|
| 1. Resolve the API permission limitation (detect unrestricted accounts, fail closed, honest limits) | **PASS** (live-server confirmation of the `all` case: not done, §3) |
| 2. Linux compatibility in a disposable environment, official installer, local-only | **PASS** for a containerised Agent 7.80.3 aarch64; host-package and x86-64 **not verified** |
| 3. Installation and configuration UX | **PASS** |
| 4. Reliability, security and overhead review | **PASS** (overhead: inference plus client-side measurement only; no benchmark) |
| 5. Packaging and submission readiness assessment | **PASS** as an assessment; submission itself **BLOCKED** by B1–B4 |
| 6. Cleanup and no production changes | **PASS** |

Stopped here; nothing further will be started without approval.
