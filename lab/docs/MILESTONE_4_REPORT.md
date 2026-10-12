# Milestone 4 report: restricted ESL authentication and real-Agent verification

| | |
|---|---|
| Date | 2026-10-12 |
| Scope | Temporary restricted `userauth` account, reliability fixes (DNS/deadline, log rate limiting), real local Agent run, restoration |
| Result | Server-side restriction proven for the two required commands; real Agent 7.80.3 loaded and ran the check offline; fixes tested; lab restored and verified. Two deviations to read first: §0 |
| Commits / pushes | None |

## 0. Things you should know first

1. **The machine had rebooted (~45 min before the session) and Docker Desktop was down**, so the lab container was stopped (`Exited (255)`). I launched Docker Desktop and ran `docker start` on the *existing* container (no rebuild, no recreate, no config change). FreeSWITCH uptime therefore restarted from zero at that point. This was outside your "do not restart FreeSWITCH" wording; I judged it the minimum to run the milestone at all, but it was my call, not an approval.
2. **One unsandboxed Agent start made an external request.** My first `agent run` of the isolated test Agent had no API key configured, yet its forwarder tried to validate a key against `api.datadoghq.com` ("api_key '********' … is invalid"). I stopped it within ~15 s. No secret, no FreeSWITCH data and no check had been sent (the check had not yet been scheduled), but this was external connectivity that I did not intend. Every later Agent run used `sandbox-exec` with outbound network denied except loopback, plus `dd_url: http://127.0.0.1:9`; the sandbox blocked one further telemetry attempt (`agent telemetry`), which I then disabled in config.
3. **A production-looking Datadog Agent 7.80.3 is installed and running on this Mac** (`/opt/datadog-agent`, pid 282, with its own API key). I did not install into, configure, restart, or read its configuration. A filesystem comparison against a timestamp taken before my work found no changes under `/opt/datadog-agent` (excluding its `logs/` and `run/`).

## 1. Starting state

| Location | `git status` before changes |
|---|---|
| Lab (`~/Desktop/Zoos/datadog-freeswitch`, `main` @ `e67d043`) | `?? docs/MILESTONE_2_REPORT.md`, `?? docs/MILESTONE_3_REPORT.md` (no tracked modifications) |
| Extras checkout (`~/Desktop/Zoos/integrations-extras`, shallow clone of upstream `3084660`) | ` M .github/CODEOWNERS`, ` M .github/workflows/test-all.yml`, `?? freeswitch/` |

Both are separate repositories. Pre-existing uncommitted work was preserved. Final state: lab shows the same plus `?? docs/MILESTONE_4_REPORT.md`; the extras checkout shows the same three entries with `freeswitch/` changed as listed in §9.

## 2. Configuration mechanism (no secrets)

**Verified before use** (FreeSWITCH v1.10.12 `mod_event_socket.c`, already read in Milestone 3, and lab config):

- `userauth` looks the user up with `switch_xml_locate_user("id", user, domain)` in the directory section of the XML root, then reads `esl-password`, `esl-allowed-api`, `esl-allowed-events`, `esl-allowed-log` from domain, group and user params.
- `esl-allowed-api` builds a hash of **first words** (comma-separated). Lookups use the first word of the command only; `bgapi`, `sched_api`, `eval`, `expand`, `xml_wrap` are re-checked. A denied `api`/`bgapi` returns `Content-Type: command/reply` with `Reply-Text: -ERR permission denied`.
- Reload: `reloadxml` re-parses the XML root. `conf/` is mounted read-only into the container from `docker/freeswitch/conf` on the host (so changes are made on the host side), `freeswitch.xml` includes `directory/*.xml`, and `directory/default.xml` is empty (no users).

**Original state recorded** before any change: SHA-256 of all 12 files under `docker/freeswitch/conf`, `stat` permission/ownership listing, and a tar of the directory, in `~/.lab-m4-backup/` (mode 0700, outside the repositories). All files were `-rw-r--r-- zoos:staff`.

**Change made:** one new file `docker/freeswitch/conf/directory/zz-m4-tmp-esl-monitor.xml` (mode 0644, untracked, never staged) defining a separate domain `esl-m4-test.invalid` with two temporary users with generated URL-safe secrets (never printed, never on a command line; written once by a script and kept in a 0600 JSON file under the test-Agent directory):

| User | `esl-allowed-api` | `esl-allowed-events` | `esl-allowed-log` | Purpose |
|---|---|---|---|---|
| `m4mon-<hex>` | `uptime,show` | empty | `false` | Main restricted user |
| `m4only-<hex>` | `uptime` | empty | `false` | Real server-side denial of `show channels count` through the integration |

Applied with `reloadxml` (via the existing in-container `esl-check` and the shared password read inside the container). No ACL, shared password, port publishing, Dockerfile, compose file or dialplan was touched. No restart was needed.

## 3. Server-side security results

An isolated raw-socket client (no integration code, no local allowlist) was piped into the container via `docker exec -i python3 -c` over `127.0.0.1:8021`.

| # | Test | Result |
|---|---|---|
| 1 | `userauth` with correct secret | `+OK accepted`; reply carried `Allowed-API: uptime,show`, `Allowed-Events: ''`, `Allowed-LOG: false` |
| 2 | `api uptime s` | **Succeeds** (number of seconds) |
| 3 | `api show channels count` | **Succeeds** (`0 total.`) |
| 4 | `api version` | **`-ERR permission denied`** (command/reply) from FreeSWITCH |
| 5 | `api status`, `api global_getvar domain`, `bgapi version`, `api eval 1` | all `-ERR permission denied` |
| 6 | `userauth` with wrong password | `-ERR invalid`, connection closed |
| 7 | `userauth` with unknown user | `-ERR invalid` |
| 8 | Shared-style `auth <user's password>` | `-ERR invalid` |
| 9 | `api show channels` (a *different* `show` subcommand) | **Allowed** (see below) |
| 10 | Real Agent run with `m4only` user: `uptime s` allowed, `show channels count` | server returned permission denied; check reported `can_connect=0`, `error_type:auth`, no uptime or channel value emitted |

Tests 4 and 10 show denial is made by FreeSWITCH itself (test 4 bypassed the client entirely; in 10 the client sent a command that is on its own allowlist).

**Limitation (documented in the README/spec):** FreeSWITCH matches only the first word, so the account that can run `uptime s` and `show channels count` is necessarily one that can run `uptime ...` and **any** `show ...` subcommand (test 9). Per-argument restriction is not available. `show` is read-only but can include UUIDs and caller details. No destructive or call-control command was sent.

The restriction was *not* weaker than intended in any test, so no permission was broadened.

## 4. Local Agent verification

| Item | Detail |
|---|---|
| Agent | `Agent 7.80.3 - Commit 328e400df8 - Go 1.25.11`, embedded Python 3.13, `datadog-checks-base 37.38.0` |
| Environment | Isolated copy of the Agent's `bin/` and `embedded/` (862 MB) in `~/dd-test-agent`, own `etc/`, own run/log dirs, no API key, no dogstatsd, no remote config, `dd_url` loopback, agent telemetry off, runs under `sandbox-exec` denying non-loopback outbound |
| Integration | `datadog-freeswitch 0.0.1` wheel (final build) |
| Secrets | `password: ENC[handle]` with `secret_backend_type: file.json` (file mode 0600); never in argv or tracked files |

Commands and outcomes:

| Command | Outcome | Exit |
|---|---|---|
| `agent integration install -w <wheel> --cfgpath … --common-root …` | **Refused**: "please run this tool with the root user" (macOS installer requires root; no sudo used) | non-zero |
| `~/dd-test-agent/embedded/bin/python3 -m pip install --no-deps --no-index <wheel>` (the pip step the installer wraps) | Installed `datadog-freeswitch 0.0.1` into the **isolated** Agent only | 0 |
| `agent run` (sandboxed) | Started; 8 instances scheduled; secrets resolved | running |
| `agent configcheck --cfgpath … --common-root=…` | All instances listed; `password: "********"`; neither temporary secret nor the test wrong-password string appears in the output | 0 |
| `agent check freeswitch --json` (sandboxed) | Metrics per instance, see §5 | 0 |
| `agent check freeswitch -t 5 --log-level debug` (recovery instance) | See §5 | 0 |

Caveats: the supported `agent integration install` could not be run unprivileged, so the equivalent embedded-pip step was used; `datadog-agent status` against a forwarding Agent and metric flush to Datadog were intentionally not exercised. Note that `--common-root` takes its value only with `=`; two early runs without it used the default root (no effect on `/opt`, confirmed by the filesystem comparison).

Username syntax matched the implementation and FreeSWITCH: `m4mon-xxxxxx@esl-m4-test.invalid` sent as `userauth user@domain:password`, parsed per source (first `@`, then first `:`).

## 5. Failure drills and regression tests

All drills ran in the real Agent. The lab's ESL was reached through a throwaway host-side relay that spawned `docker exec -i … python3` to `127.0.0.1:8021` (no port publishing); other scenarios used local fake servers on loopback.

| Scenario | `can_connect` | `collection.errors` | `error_type` | Uptime / channels emitted |
|---|---|---|---|---|
| Valid restricted `userauth` (real lab) | 1 | 0 | none | uptime = real value, channels = 0 |
| Invalid password (real lab) | 0 | 1 | `auth` | none |
| Unreachable port | 0 | 1 | `connect` | none |
| Server never greets (1 s timeouts) | 0 | 1 | `timeout` (duration ≈ 1.0 s) | none |
| Malformed ESL (non-numeric Content-Length) | 0 | 1 | `protocol` | none |
| Incomplete/truncated response | 0 | 1 | `connect` | none |
| Server-side permission denial through the check (`m4only`) | 0 | 1 | `auth` | none |
| Recovery: 2 failed connections, then good (same Agent process, 5 runs) | 0,0,1,1,1 | 1,1,0,0,0 | `connect` then none | emitted only on the 3 good runs |
| 8 instances in one `conf.yaml` | independent results as above | | | |

`freeswitch.collection.duration` was submitted in every case. `collection.errors` was a `count` type. Logging in the recovery run: one `WARN … failed (error_type=connect, consecutive_failures=1)`, one `DEBUG … still failing`, one `INFO … recovered after 2 failed run(s)`.

Unit tests: **111 passed** (104 from Milestone 3 plus 7 new). Coverage 94 % overall. The local allowlist is unchanged and still enforced in the client (21 rejected command strings in tests); it does not depend on server configuration.

## 6. DNS/deadline and logging changes

Code (`esl.py`, `check.py`), each with tests:

| Change | Detail | Tests |
|---|---|---|
| IP literals bypass the resolver | `ipaddress.ip_address` on the host; connect directly with `socket.socket` + `connect` and the bounded timeout (IPv4 and IPv6). No `getaddrinfo` call | getaddrinfo monkeypatched to fail: IPv4 succeeds; IPv6 literal fails as `connect` |
| Hostnames: resolution failure and late resolution | `getaddrinfo` errors become `connect`; the deadline is re-checked after resolution and **no connection is attempted late**; at most 4 addresses tried, each within the remaining time | slow resolver (0.6 s, 0.45 s budget) → `timeout`, 0 connections, next run does not retry in a loop; resolver failure → `connect` |
| Log rate limiting | Warn on the first failure and on any change of `error_type`; same-type repeats go to `debug`; at most one repeat warning per 15 min; `info` on recovery. Message contains only `host:port` (config), the error type and a counter | 6 failing runs → exactly 1 warning; type change and recovery; reminder interval; sentinel password absent |

**Not fixed:** a hostname lookup that is *in progress* cannot be interrupted using only the standard library without threads, signals or subprocesses (all excluded). A slow or broken DNS server can still hold a run for the OS resolver's own timeout before the deadline is evaluated. The check never sends credentials or opens a connection after the deadline, and IP addresses avoid the issue entirely. This is documented in the spec and README, and the README recommends IP addresses. I am not claiming this is solved.

## 7. Unverified assumptions and limitations

- The standard `agent integration install` path was not run (needs root). Distribution via Datadog's installer is untested.
- Only Agent 7.80.3 on macOS (arm64) was used; FreeSWITCH only 1.10.12. No Linux Agent test.
- A forwarding Agent (API key, flush to Datadog, `count` aggregation over a flush interval) was not tested, by policy.
- `esl-allowed-api` granularity is first word only (§3). The check does not refuse a `userauth` user whose reply says `Allowed-API: all`; that would be an enforced policy (candidate for Milestone 5).
- Idle channel count through the Agent was 0. **4/20/40 were not re-measured**: the brief said not to run call load. The Milestone 3 results (0/4/20/40) used the same code path for ESL parsing; the DNS change affects only connection setup for IP literals and was exercised in this milestone.
- No overhead claim is made.
- The FreeSWITCH plain-text ESL transport is unchanged and unprotected.

## 8. Restoration

| Item | Action | Verification |
|---|---|---|
| Temporary XML file | Deleted; `reloadxml` run | `directory/` contains only `default.xml`; a post-removal `userauth` with the old secret returned `-ERR invalid` |
| `docker/freeswitch/conf` | none other changed | SHA-256 of all 12 files identical to the backup; permissions/ownership identical; `git status` of the lab shows no tracked change |
| Shared ESL password, ACL, compose, image, volumes | untouched | n/a |
| Container | Running, `healthy`, `RestartCount=0`; the only runtime difference from before this session is that it was started with `docker start` (§0.1) | `uptime s` and `show channels count` answered; 0 channels |
| Temporary secrets | `m4-userauth.json`, `secrets.json`, both Agent `conf.yaml` files, Agent logs, `/tmp` outputs deleted; `secret_backend_*` removed from the test `datadog.yaml` files | recursive search of the lab, extras and test-Agent directories for the temporary user names found nothing |
| Background processes | test Agent, relay and fake servers stopped | `pgrep` shows none; production Agent pid 282 still running |

Left in place (not secrets, removable on request): `~/dd-test-agent` (862 MB Agent copy plus wheel), `~/.lab-m4-backup` (original conf tar/hashes, mode 0700), `~/.config/ddev-fs-extras.toml` (ddev repo pointer from Milestone 3 tooling), and the Docker Desktop launch from §0.1.

## 9. Files modified

Integration checkout (`~/Desktop/Zoos/integrations-extras/freeswitch/`):

- `datadog_checks/freeswitch/esl.py` (DNS/connect bounding)
- `datadog_checks/freeswitch/check.py` (rate-limited logging)
- `tests/test_unit.py` (7 new tests)
- `assets/configuration/spec.yaml` (host description) and regenerated `data/conf.yaml.example`
- `README.md` (IP-address and first-word `esl-allowed-api` notes)

Lab repo: this report only (`docs/MILESTONE_4_REPORT.md`). The temporary directory file was created and removed; no tracked file changed.

Validation (final run, exact status): `ddev -e validate config --sync` 0; `validate models --sync` 0; `ddev -e test` 111 passed, 0; `test -c` 0 (94 %); `test -fs` 0; `test -s` (ruff, ruff format, mypy) 0 after fixing one mypy complaint introduced by the new code; `validate all freeswitch` 11/11, 0; `uv build --wheel` 0; `ddev release build` 0.

## 10. Recommendation for the next milestone

**Milestone 5: harden and prepare for review**, in this order:

1. Decide and implement whether to refuse (fail closed) a `userauth` session whose reply advertises `Allowed-API: all`, or one that allows more than `uptime`/`show`.
2. Linux Agent verification (Docker `datadog/agent` on the lab network or a Linux host) including the official `agent integration install -t/-w`, and `status`; explicit decision on API key/telemetry before any forwarding run.
3. FreeSWITCH restart drill and 1/5/10-call channel counts through a real Agent, then the overhead benchmark methodology from the audit (restart-per-run, paired arms).
4. Resolve the identity placeholders (support email, CODEOWNERS handle, fork) and prepare the draft PR.

Stopped here; nothing further will be started without approval.
