# FreeSWITCH integration: metric verification report

Three local sessions, all 2026-10-12: the metric implementation and first verification (sections 1 to 2 keep that history),
the final verification and reconciliation (sections 3 to 5, 7 to 9), and a workspace audit with RTP event research
(section 6 and section 11, which supersede earlier RTP statements). Local work only: nothing was committed, pushed or
published; no remote, credential, SSH or global Git configuration was touched; the lab container
(`datadog-freeswitch-freeswitch-1`) was only listed with `docker ps` and stayed `Up (healthy)`.

## Verification levels used in this report

| Level | Meaning |
|---|---|
| **Unit / fixture** | Deterministic tests with the in-process fake Event Socket server and captured output. Shows parsing and emission logic only. It does **not** show that FreeSWITCH offers the data |
| **Runtime (disposable)** | A real FreeSWITCH 1.10.12 container created for the run (`integration/tests/runtime/testbed.py`), real SIP and RTP over its own loopback, ground truth read over a separate connection |
| **Agent** | The stock `datadog/agent:7.80.3` image running this check on its own schedule; its intake was a local sink container, so nothing left the machine and no real API key was used |
| **Not verified** | Not observed at any level above |

## 1. State at the start of the final session

| | |
|---|---|
| Branch / HEAD | `main` / `58e55cbd96fce8db8fac71f4ddfa64576924f7ed` (unchanged at the end) |
| Existing report | `docs/evidence/METRIC_VERIFICATION_REPORT.md` (this file, previous version replaced; its facts are carried below) |
| Tests | `integration/tests/{test_unit,test_campaign,test_metrics}.py`, `integration/tests/runtime/` |
| Metric documentation | `integration/metadata.csv`, `integration/README.md`, `docs/ROADMAP.md` (sections 2.1, 6.2), `docs/ARCHITECTURE.md` |
| Pre-existing local changes | all preserved; none discarded |
| `integrations-extras` | `3fefc5d` on `add-freeswitch-integration`; `freeswitch/` holds an uncommitted local copy of the same files so `ddev` could run (not pushed) |

## 2. History kept from the first session (facts, not status)

- Disposable testbed built; registration, gateway, real SIP calls, restricted-account behavior and event captures were
  obtained on it. Startup lesson: FreeSWITCH's XML pre-processor is line-based (an include on the same line as other
  markup silently fails); registrations need `from-domain` when the domain is not the proxy address.
- `peak` is an all-time high-water mark (held at 6 for 391 s across two 5-minute boundaries and a later smaller call).
  `last 5min` resets on fixed 5-minute boundaries (6 to 0 at t about 300 s).
- `show channels count`, `show registrations count` and `show bridged_calls count` trail call setup by about a second
  (core table written asynchronously); `status` counters do not.
- `show calls count` equals channels minus bridged pairs. It is not a call count.
- The account limited to `uptime,show` is denied `status`, `sofia ...`, `version`, `hupall`, `originate`.
- Hangup events carry `rtp_audio_*` variables; in the test calls they were 0 or 1 packets and MOS `nan`.

## 3. `sessions.created` across a FreeSWITCH restart

### Question
The metric was changed from a gauge to `monotonic_count`. Does that survive a restart, as a real Agent processes it?

### Method
`integration/tests/runtime/restart_experiment.py`: disposable FreeSWITCH plus the real Agent image on a private Docker
network; the check runs every 5 s; a timeline of calls and `docker restart` of only the disposable container; raw
`status` values recorded with timestamps; what the Agent serialized (`/api/v1/series`) captured by the sink.
Raw data: [data/sessions_created_restart_before_fix.json](data/sessions_created_restart_before_fix.json),
[data/sessions_created_restart_after_fix.json](data/sessions_created_restart_after_fix.json).

### Observations (same timeline both runs)

| Time (s) | Event | Raw `since startup` |
|---|---|---|
| 0 | idle, Agent already sampling | 0 |
| 16 | 2 calls (4 sessions) | 4 |
| 43 | **restart #1** (new process) | 0 |
| 64 | 1 call (2 sessions) | 2 |
| 85 | 3 calls (6 sessions) | 8 |
| 102 | **restart #2**, then 12 failed originates before the next Agent sample | 12 |

Agent-reported non-zero increments:

| | Before the change (plain `monotonic_count`) | After (reset sample on detected restart) |
|---|---|---|
| 2 calls | +4 | +4 |
| after restart #1, 1 call | +2 | +2 |
| 3 calls | +6 | +6 |
| after restart #2 (raw 12) | **+4** (12 minus the old process's 8) | **+12** |
| Sum | 16 | **24** (true total 4+2+6+12) |

### Conclusions
1. The raw FreeSWITCH counter resets to 0 on restart (observed: 4 to 0, 8 to 0).
2. A real Agent ignores a lower sample **and takes it as the new baseline**: after restart #1 the next increments (+2, +6)
   were exact. Valid post-restart increments are not suppressed by the drop itself.
3. It is **not** correct in general: when the new process passes the old count before the first sample, the Agent reports
   only `new - old`. Restart #2 lost 8 of 12 sessions. Sessions created between a restart and the first sample are also
   lost when the new value stays below the old one. The earlier statement that "ignoring a lower sample handles resets"
   was incomplete and is withdrawn.
4. Fix, in `check.py`: the check remembers the last uptime and counter; when uptime went down or the counter went down it
   submits a `0` sample before the real value (`_submit_created`), which moves the Agent's baseline to 0. A restart seen
   in a run whose `show status` failed is applied to the next value. The re-run showed all 24 sessions.
5. Remaining uncertainty: a restart that happens while the Agent or check is not running cannot be detected, and sessions
   created by the old process before the Agent's first sample are not counted. The Agent behaviour above was observed with
   Agent 7.80.3 only. The server-side (Datadog backend) handling of the `count` after the Agent was not observable here.
6. `sessions.limit` stays a gauge: it is a configured constant, not a cumulative total (unchanged by this evidence).

### Tests added
`test_metrics.py`: startup (raw value, no reset sample), repeated and growing samples (no injected zero), restart with a
lower counter, restart where the new count already exceeds the old one, restart seen while `show status` fails, two
restarts with a failed run between, per-instance state. They apply a small model of the observed Agent rule
(`agent_increase`); that model is labelled as such and is not the Agent. Runtime:
`test_sessions_created_across_a_real_restart_submits_the_baseline_reset` (real restart, same check object).

## 4. Cost of `calls.bridged`

### Measurements
`integration/tests/runtime/measure_bridged.py`; disposable FreeSWITCH, real SIP bridges (4 channels each) over its
loopback, 15 samples per level, one connection reused, restricted account, the check's own client. Single host (Docker
Desktop on macOS), idle FreeSWITCH otherwise. Raw: [data/bridged_calls_cost.json](data/bridged_calls_cost.json).

| Bridged calls | Channels | Response bytes | `show bridged_calls count`, median / max ms | Complete run (5 commands), median / max ms |
|---|---|---|---|---|
| 0 | 0 | 10 | 2.0 / 2.9 | 3.8 / 6.7 |
| 1 | 4 | 879 | 2.5 / 5.5 | 4.5 / 7.6 |
| 5 | 20 | 2,459 | 2.4 / 3.8 | 3.9 / 6.5 |
| 10 | 40 | 4,435 | 2.9 / 4.4 | 4.1 / 6.6 |
| 25 | 100 | 10,360 | 2.9 / 4.5 | 4.6 / 9.9 |
| 50 | 200 | 20,235 | 3.4 / 6.1 | 5.0 / 7.5 |
| 100 | 400 | 39,986 | 2.9 / 4.8 | 3.7 / 7.1 |
| 150 | 600 | 59,736 | 6.6 / 9.7 | 7.4 / 11.6 |

Every level parsed to the exact bridge count. About 395 to 400 bytes per bridged call in this test (the first report's
"about 600" was an estimate and is corrected). Transfer at the default 15 s interval (240 runs per hour): 100 bridged
calls give about 40 KB per run, 9.6 MB per hour, 230 MB per day; 150 give 14.3 MB per hour, 344 MB per day, per
instance. Real rows with longer names and caller IDs will be larger.

### Limits of this sample
One host, loopback, 15 samples per level, at most 150 bridges, idle CPU only (`min idle cpu` 97.4 percent at the top
level), FreeSWITCH-side query cost on a large production database not measured, no concurrency with other ESL clients,
no network latency. These numbers do not predict production behaviour.

### Handling of bad output (unit-tested, fake server)
Truncated listing (no trailing total), a total that is not the last line, a non-empty count without the `uuid,` header,
negative or non-numeric totals, UTF-8 errors, `-ERR` replies and over-long numbers are `parse` or `protocol` errors that
leave every other metric intact. Output above 2 MiB is a `protocol` error (the connection is closed, the next run
reconnects). A fake total inside a row cannot change the result (only the last line is read). Row content never reaches
a tag, name, log or exception.

### Decision: keep it, **disabled by default** (`collect_bridged_calls: false`)
| Option | Assessment |
|---|---|
| Enabled by default | Rejected. Latency is small (3 to 10 ms), so cost is not the deciding factor; the listing carries caller IDs and extensions and crosses the unencrypted Event Socket every 15 s, to produce one integer. The other four queries carry no caller data. Transfer grows linearly with calls (hundreds of MB per day per instance at 100 calls) |
| **Opt-in, off by default** | **Chosen.** Same security boundary (the `show` permission already exists), no new command, users who want connected-call counts accept the transfer explicitly |
| Defer entirely | Not chosen. The metric is runtime-verified and exact; no cheaper source exists (`show calls count` is not a call count, `show bridged_calls count` is not count-only) |

Implemented: new instance option `collect_bridged_calls` (spec, generated models, `conf.yaml.example`), the command is
not sent and the metric is not emitted unless true, the run budget counts four commands (five when enabled). The Event
Socket permission boundary is unchanged. Tests: default sends no bridged command (unit and real-server relay), per-instance
enablement, boolean validation, budget.

## 5. Reconciled metric matrix

Permissions: every command below is under the existing `uptime,show` account (`uptime` first word for `uptime s`, `show`
for the others). Tags: base only (`freeswitch_host`, `freeswitch_port`, configured tags) unless stated.

| Metric / area | Status | Semantics | Type, unit | Source | Tests | Known limitations |
|---|---|---|---|---|---|---|
| `freeswitch.can_connect` | **Implemented, runtime-verified, Agent-observed** | 1 if connected, authenticated and both required queries valid | gauge | whole run | unit; runtime; emitted by the real Agent | optional-query failures do not change it |
| `freeswitch.uptime` | **Implemented, runtime-verified, Agent-observed** | seconds since process start | gauge, second | `uptime s` | unit; runtime | resets on restart |
| `freeswitch.collection.errors` | **Implemented, runtime-verified** | failed runs, one sample per distinct `error_type` (`connect, timeout, auth, protocol, parse`) | count, error | check | unit; runtime (auth, refused, paused container, refused connection) | optional failures reported with the same bounded values; which query failed is only in the log |
| `freeswitch.collection.duration` | **Implemented, unit-tested** (emitted under the Agent) | run time | gauge, second | check | unit | |
| `freeswitch.channels.active` | **Implemented, runtime-verified** | channels existing now | gauge | `show channels count` | unit; runtime (0, 4, 6, 12, 14) | channels not calls; trails call setup by about 1 s |
| `freeswitch.sessions.active` | **Implemented, runtime-verified, Agent-observed** | current sessions (core counter) | gauge, session | `show status` line `N session(s) - peak P` | unit; runtime | equals channels on every sample; line optional on older output |
| `freeswitch.sessions.peak` | **Implemented, runtime-verified** | all-time high-water mark since process start | gauge, session | same line, `peak P` | unit; runtime; 391 s timeline | not a recent peak; resets only on restart |
| `freeswitch.sessions.created` | **Implemented, runtime-verified, restart behaviour Agent-verified** | cumulative sessions since process start; failed originates count | monotonic_count, session | `show status` `N session(s) since startup` | unit (7 regression tests); runtime incl. real restart; Agent run (24 of 24) | uncountable: before the Agent's first sample, and a restart missed while the check or Agent was down; Agent 7.80.3 only |
| `freeswitch.sessions.limit` | **Implemented, runtime-verified** | configured `max-sessions` | gauge, session | `N session(s) max` | unit; runtime | tested on 1.10.12 only |
| `freeswitch.sessions.rate.limit` | **Implemented, runtime-verified** | configured `sessions-per-second` | gauge, session | rate line `out of max M` | unit; runtime | optional line |
| Current sessions-per-second, `last 5min` values | **Investigated, deliberately deferred** | | | rate line | timeline evidence | the 5-minute figures reset on fixed boundaries; current rate is a one-second sample every 15 s |
| `freeswitch.registrations` | **Implemented, runtime-verified** | current registrations, all profiles | gauge | `show registrations count` | unit (real 0 and 1 captures); runtime (1, 0 after killgw, 1) | no per-profile or per-user tag; count only |
| `freeswitch.calls.bridged` | **Implemented, runtime-verified (when enabled); off by default** | channel pairs currently bridged | gauge | `show bridged_calls count` (full listing) | unit; runtime 0, 1, 3, up to 150 | caller data on the wire; about 400 B per call; 2 MiB cap; opt-in |
| Logical call counts (`calls.active`) | **Blocked** (no reliable source) | | | `show calls count`, `show bridged_calls`, sessions, channels | evidence only | `show calls count` = channels minus bridged pairs; calls need originate/answer events or CDR |
| Gateway and profile health | **Blocked by security constraint** | | | `sofia status [gateway]` only; no `show` subcommand lists them | output captured; denial of `sofia` confirmed for the restricted account | the `sofia` first word cannot be narrowed and would also permit stopping or restarting profiles. Observed states: `REGED`, `NOREG` (with answered ping `Status UP`), `FAIL_WAIT`, `FAILED`; `REGED` can still be DOWN for originate. Not enabled; no permission was broadened |
| RTP packet counts, MOS, quality percentage | **Investigated; available only in `CHANNEL_HANGUP_COMPLETE`; not implemented** | per leg, at hangup | event variables `rtp_audio_*` | event-only account works (section 11) | consistent in the disposable environment (section 11) | needs a separate event listener; no metric registered |
| RTP jitter and packet-loss values | **Investigated; not suitable as emitted** | | same event | | noisy / not a loss ratio (section 11) | do not publish as jitter or loss |
| Version metadata | **Investigated, deliberately deferred** | | `show status` banner | none | | `version` is outside `uptime,show` |

## 6. RTP quality from hangup events (research; no collector implemented)

Full evidence and the decision are in section 11. Summary: the earlier statement that RTP values "were not meaningful"
was caused by the test configuration (the sofia profiles had no `rtp-timer-name`, so FreeSWITCH paced no RTP: about
one datagram per second was measured), not by FreeSWITCH. With `rtp-timer-name=soft` the values are meaningful in the
disposable environment. No RTP metric is implemented or registered.

## 7. Commands executed and results

All from `integration/` unless noted. Results are from this session's final run.

| Command | Result |
|---|---|
| `pytest tests/test_metrics.py -q` (focused) | 73 passed |
| `pytest tests -q --cov=datadog_checks.freeswitch` | 316 passed, 15 skipped (runtime); coverage 95% (check.py 97%, esl.py 94%) (second session; final counts in section 11) |
| `FS_RUNTIME_TESTS=1 pytest tests -q` | 331 passed (15 runtime tests on a disposable FreeSWITCH) |
| `python -m tests.runtime.restart_experiment <dir>` (three runs; the last two are in `data/`) | completed; results in section 3 |
| `python -m tests.runtime.measure_bridged <file>` | completed; results in section 4 |
| `ddev -x test freeswitch` (in `integrations-extras`) | 316 passed, 15 skipped |
| `ddev -x test --fmt freeswitch`, then `ddev -x test -s freeswitch` (ruff format, ruff check, mypy) | passed: 14 files formatted, all checks passed, mypy no issues in 21 files |
| `ddev -x validate metadata freeswitch` | first run failed (a description over the 400-character limit, my defect); shortened; re-run passed |
| `ddev -x validate config / models / readmes / package / eula freeswitch` | all passed (`config --sync` and `models --sync` were run after adding the option) |
| `ddev -x validate codeowners` | passed |
| `git diff --check` | no output, exit 0 |
| `ddev -x validate ci`, `labeler`, `licenses`, `manifest`, `dep` (previous session) | not applicable or not available: `ci` unknown repository `local`; `labeler` and `licenses` only for integrations-core; no `manifest` command in ddev 20.0.0; `dep` fails on other integrations' requirements |

**Not run / blocked:** validators that need an `integrations-core` checkout; an end-to-end run against a Datadog backend
(the Agent run used a local sink); more than one Agent version; Agent behaviour of the `count` series after leaving the
Agent. A passing local suite is not official Datadog validation.

## 8. Blocked or deferred work

1. **Gateway/profile health:** blocked by the `sofia` first-word permission. Needs an explicit decision, a new allowlist
   entry and account policy set, and the state-mapping table; not attempted.
2. **Logical call counts:** no reliable polled source; needs events or CDR (a different collection path).
3. **RTP jitter, loss, MOS:** section 6.
4. **`calls.bridged` default:** decided off; revisit only if a count-only source appears.
5. **Not covered:** other FreeSWITCH versions, real Agent with a real intake, soak testing, TLS or remote hosts.

## 9. Files changed in the final session and why

| File | Change |
|---|---|
| `integration/datadog_checks/freeswitch/check.py` | restart-aware `sessions.created` submission; opt-in `collect_bridged_calls`; four-command default budget |
| `integration/assets/configuration/spec.yaml`, `config_models/{defaults,instance}.py`, `data/conf.yaml.example` | new `collect_bridged_calls` option (generated files produced by `ddev validate ... --sync`) |
| `integration/metadata.csv` | descriptions of `sessions.created` and `calls.bridged` (400-character limit) |
| `integration/tests/test_metrics.py`, `test_unit.py`, `runtime/test_runtime.py` | restart regression tests, opt-in tests, default-command expectations, runtime restart and relay tests |
| `integration/tests/runtime/{testbed,restart_experiment,measure_bridged}.py` | restart and network support, the two experiments |
| `integration/README.md`, `CHANGELOG.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` | reconciled with the code: command counts, opt-in, restart behaviour, roadmap sections 2.1, 2.2, 2.3, 6.2 status cells and DEC-7 |
| `docs/evidence/data/*.json` | raw experiment data (metric values and timestamps only; no credentials, hosts or numbers) |

A stray `datadog-freeswitch/integrations-extras/` directory created by one of my mis-pathed `rsync` calls (one copied
file) was removed. Unrelated pre-existing edits (for example the `manifest.json` support e-mail) were preserved and not
reviewed here.

## 10. Final Git state

Branch `main`, HEAD `58e55cbd96fce8db8fac71f4ddfa64576924f7ed`, nothing staged or committed.

Modified: `README.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `integration/CHANGELOG.md`, `integration/README.md`,
`integration/assets/configuration/spec.yaml`, `integration/datadog_checks/freeswitch/{check.py,esl.py}`,
`integration/datadog_checks/freeswitch/config_models/{defaults.py,instance.py}`,
`integration/datadog_checks/freeswitch/data/conf.yaml.example`, `integration/manifest.json`, `integration/metadata.csv`,
`integration/tests/{fake_esl.py,test_campaign.py,test_unit.py}`.

Untracked: `docs/evidence/` (this report, `RM1_T1_T2_EVIDENCE.md`, `data/`), `integration/tests/runtime/`,
`integration/tests/test_metrics.py`.

`integrations-extras` (separate repository, branch `add-freeswitch-integration`, HEAD `3fefc5d` unchanged): local
uncommitted copy of the same changes under `freeswitch/`; not pushed.

## 11. Third session: workspace audit and RTP event research

### 11.1 Confirmed repositories (read-only inspection, nothing changed)

| | `datadog-freeswitch` (private lab/research workspace) | `integrations-extras` |
|---|---|---|
| Path / top level | `/Users/zoos/Desktop/Zoos/datadog-freeswitch` | `/Users/zoos/Desktop/Zoos/integrations-extras` |
| Branch / HEAD | `main` / `58e55cbd96fce8db8fac71f4ddfa64576924f7ed` ("roadmap") | `add-freeswitch-integration` / `3fefc5d26ea51297a97b1ef7dc9bdd235ba1f38e` ("Add FreeSWITCH integration") |
| Remotes | `origin` = `github.com/shivamsancc/datadog-freeswitch` (fetch and push) | `fork` = `github.com/shivamsancc/integrations-extras`, `origin` = `github.com/DataDog/integrations-extras` |
| Working tree | 16 modified, untracked `docs/evidence/`, `integration/tests/runtime/`, `integration/tests/test_metrics.py`; nothing staged | 12 modified, untracked `freeswitch/tests/runtime/`, `freeswitch/tests/test_metrics.py`; nothing staged |

HEAD `58e55cb` belongs to the intended repository: it is the tip of `main` of the private lab workspace (history:
"roadmap", "Build README with reusable design kit components", ...), and the documented changes are present in the working
tree (`check.py` `_submit_created`, `collect_bridged_calls` in `spec.yaml`, `config_models`, `metadata.csv`, tests). The
`integrations-extras` copy is an uncommitted mirror kept only so `ddev` can run. Remotes were inspected, not changed.

### 11.2 Audit of the latest changes

| Check | Finding |
|---|---|
| Restart detection | `check.py` sets `_restart_pending` when uptime decreased or the counter decreased, from state kept across runs. Present and tested |
| Reset before value | `_submit_created` emits `monotonic_count(0)` and then the raw value in the same run. Order covered by unit tests and observed with the real Agent (24 of 24) |
| Normal growth, repeats, failed status, per-instance state | covered by the seven tests of section 3; an added test shows a restart that happens during runs that fail before emitting is detected on recovery (state is kept) |
| Limitation text | Corrected: a restart is **not** lost just because the check was unavailable (uptime is compared on recovery); it is missed when the Agent and check were restarted together (memory lost) or when the new process already has a higher uptime and counter than the last sample. Documented in `integration/README.md` and covered by a test of the undetected case |
| `agent_increase` vs real Agent | `agent_increase` is a model in `test_metrics.py` and says so. The real Agent evidence is the experiment (`restart_experiment.py`, `data/sessions_created_restart_*.json`) and the runtime restart test, not those unit tests. No backend-level claim is made and exact totals are not guaranteed for every restart scenario |
| `collect_bridged_calls` | exists in `spec.yaml`, `defaults.py` (None), `instance.py`, `conf.yaml.example` (`false`), default off; `check.py` skips the query when off (`_collect_bridged`) |
| Query not sent when off | unit test asserts no `show bridged_calls count` line on the wire; the real-server relay test asserts the four-command set |
| Enabled paths | parsing, malformed, truncated, `-ERR`, permission denied, timeout, oversize (2 MiB) covered in `test_metrics.py` |
| Caller IDs / credentials | no tag, name or log carries listing content (test with caller-like strings in rows and replies); log messages are fixed text |
| Permissions | `esl.py` allowlist is five fixed commands, all `show`/`uptime`; `REQUIRED_API_PERMISSIONS` is still `{uptime, show}`; no `status`, `sofia`, `event` |

No genuine defect was found in the restart fix or in `collect_bridged_calls`. One documentation inaccuracy (the
limitation above) was corrected, and two tests were added.

### 11.3 RTP experiments (disposable FreeSWITCH only)

**Setup.** `integration/tests/runtime/rtp_events_experiment.py` on a new container (the lab was not touched), two sofia
profiles over the container loopback, a listener using the testbed's own full-control password, `tc netem` on the
container loopback for impairment (`iproute2` was installed inside the disposable container from its package
repositories; the container was destroyed afterwards). Raw sanitized output:
[data/rtp_hangup_events.json](data/rtp_hangup_events.json) (no UUIDs, addresses or SDP; fixed test extensions only).

**Why earlier tests showed no media.** Without `rtp-timer-name` in the sofia profiles FreeSWITCH sent about 1 datagram
per second; with `<param name="rtp-timer-name" value="soft"/>` it sent about 50 per second per direction. The testbed now
sets it. This was a test-configuration defect, not a FreeSWITCH limitation.

**Experiment A: fields in `CHANNEL_HANGUP_COMPLETE`** (one event per leg; plain format; about 9 KB per event)

| Field (`variable_rtp_audio_...`) | Meaning observed | Behaviour |
|---|---|---|
| `in_packet_count`, `in_media_packet_count`, `in_raw_bytes`, `in_media_bytes` | RTP received from the remote end of this leg | about 50 per second of call; equal in both |
| `out_packet_count`, `out_media_packet_count`, `out_raw_bytes`, `out_media_bytes` | RTP sent to the remote end of this leg | about 50 per second |
| `in_mos` | FreeSWITCH estimate; `4.50` on clean media | `nan` when no media or very short call; absent when the leg never had media (486) |
| `in_quality_percentage` | 100 on clean media, 63 to 68 at 10% impairment, 4 to 41 at 30% | `nan` without media |
| `in_flaw_total` | 0 clean; 116 to 158 at 10%; 182 to 408 at 30% | 0 without media |
| `in_jitter_loss_rate` | 0.00 clean; 0.11 to 0.17 at 10% loss; **0.74 to 0.82 with delay and jitter and no loss configured** | **not a packet-loss ratio**; reflects jitter-buffer discards |
| `in_jitter_min_variance`, `in_jitter_max_variance` | unitless in the event; clean calls ranged 0.1 to 800 (a 20 s clean call: 131 to 800) | **noisy, units undocumented** |
| `in_skip_packet_count` | 3 to 6 on clean tone calls; 254 to 257 in a 5 s `park` call; 18 to 21 in a 0.3 s call | not a loss counter; depends on whether FreeSWITCH sends |
| `rtcp_packet_count` | 0 for short calls, about 40 per second of call with RTCP interval 1000 ms | RTCP-dependent |
| `Hangup-Cause`, `Call-Direction`, `duration`, `billsec`, `read_codec` | bounded or numeric | `Hangup-Cause` values seen: `NORMAL_CLEARING`, `USER_BUSY` |

Both directions and both legs are reported; values per leg are what FreeSWITCH saw on that leg only. Meaningful values
need real bidirectional (or at least inbound) RTP; echo or park with no audio source gives zeros and `nan`.

**Experiment B: scenarios** (counts are events; each call yields two, one per leg; sanitized in the JSON)

| Scenario | Calls | Result |
|---|---|---|
| Tone 3 s | 5 | in about 147 to 152, out about 152 to 155; MOS 4.50, quality 100 on all 10 events |
| Tone 10 s | 5 | in about 500, out about 503 to 505; MOS 4.50 on all |
| Tone 20 s | 3 | in about 997, out about 1003; MOS 4.50 on all |
| `park` 5 s (control, no audio) | 5 | in 0, out 0, MOS `nan`, quality `nan` on all 10 |
| Answer and hang up after 0.3 s | 5 | counters 0, MOS `nan`, duration 0 |
| 10 concurrent tone calls, 8 s | 10 | 20 events, all delivered, all MOS 4.50 |
| Unanswered (486 busy) | 1 | events delivered with `USER_BUSY` and **no `rtp_audio_*` variables** |
| netem loss 10% | 3 | MOS 3.25 to 3.64, quality 63 to 68 |
| netem loss 30% | 3 | MOS 0.99 to 3.69 (spread), quality 4 to 41 |
| netem delay 60 ms jitter 40 ms | 3 | MOS 1.00 to 2.63; shows MOS reacts to jitter-buffer loss |

Duplicates: none seen (one event per leg). Event delivery lag (event timestamp to receipt) 7 to 56 ms in all scenarios.
Out-of-order: not observed and not provoked. Not tested: more than 10 concurrent calls, video, SRTP, transcoding, bypass
media, long calls beyond 20 s.

**Experiment C: collection reliability**

| Concern | Evidence |
|---|---|
| Permissions | A directory account with `esl-allowed-events=CHANNEL_HANGUP_COMPLETE` and `esl-allowed-api=none` authenticated, subscribed to `CHANNEL_HANGUP_COMPLETE`, and was denied `CHANNEL_CREATE`, `ALL`, `api status`, `api uptime s` and `api show channels count` (`-ERR permission denied`). Without `esl-allowed-events` the same server advertised `Allowed-Events: all` |
| Missed events | A subscriber that disconnected while a call was up and reconnected after the hangup received **no replay** (0 events). FreeSWITCH keeps no queue: data is lost during any collector outage or restart. Mitigation would have to come from CDR files or a database, which are not enabled here |
| Calls ended before statistics exist | Short and unanswered calls produce events with zero counters, `nan`, or no variables: a collector must skip those |
| Non-finite values | `nan` appears as text for MOS and quality; must be dropped, never emitted as 0 |
| Concurrency | 10 simultaneous calls: 20 of 20 events |
| Tags | Usable bounded values exist: `Call-Direction` (2), `rtp_use_codec_name` (few), `Hangup-Cause` (bounded but large). Extensions, caller IDs, UUIDs and IPs appear in the same event and must never be tags or logged |
| Architecture | The existing check polls with `uptime,show` and refuses any account whose `Allowed-API` differs; it keeps one request/response connection. An event consumer is a long-lived streaming connection with a different account, a different failure model (no replay) and about 9 KB per leg of caller-bearing data. It should be a separate listener (own process or Agent check class), not a change to this check's transport or permission boundary |

Not tested: listener behaviour when FreeSWITCH drops a slow subscriber, very high event rates, exact duplicate
conditions, or whether `nan` can appear for other fields on other versions.

### 11.4 Candidate metrics

All would be per ended call leg, emitted by a separate listener, with tags limited to a bounded `direction`
(`Call-Direction`) and optionally `codec`; never extension, caller ID or UUID. "Verified" below means
disposable-environment evidence for FreeSWITCH 1.10.12 with the stated scenarios; nothing was verified through an Agent.

| Candidate | Event field | Proposed type, unit | Emit when | Evidence | Classification |
|---|---|---|---|---|---|
| `freeswitch.rtp.mos` | `rtp_audio_in_mos` | histogram (per Agent) or distribution via DogStatsD, score 1 to 5 | value finite and `in_packet_count` above a minimum (for example 50) | 4.50 clean, falls with impairment, `nan` without media | **Verified and suitable for implementation**, as a FreeSWITCH estimate, not ITU P.800 measured |
| `freeswitch.rtp.quality` | `rtp_audio_in_quality_percentage` | histogram, percent | same as MOS | 100 clean, 63 to 68 at 10%, falls at 30% | **Verified and suitable for implementation** |
| `freeswitch.rtp.packets.in` / `.out` | `in_packet_count`, `out_packet_count` | histogram or count, packets | leg had media | about 50 per second, consistent across 46 clean legs | **Available but not meaningful enough alone** (volume figure; useful to qualify MOS) |
| `freeswitch.rtp.flaws` | `in_flaw_total` | histogram | leg had media | 0 clean, grows with impairment | **Available but not meaningful enough in current tests** (definition of "flaw" not established) |
| `freeswitch.rtp.jitter` | `in_jitter_min/max_variance` | n/a | n/a | 0.1 to 800 on clean calls, units unknown | **Inconsistent or unreliable** |
| `freeswitch.rtp.packet_loss` | `in_jitter_loss_rate`, `in_skip_packet_count` | n/a | n/a | 0.8 reported with no configured loss; skip 250+ with no media | **Inconsistent or unreliable** as loss; a true loss ratio would need RTCP receiver reports, not tested |
| Any RTP metric via the current check | n/a | n/a | n/a | values not returned by any polled command | **Blocked by architecture** (event listener required) |

### 11.5 Recommendation

1. **Do not add RTP to the existing check.** Its poll-and-parse model, `uptime,show` account policy and request
   framing do not fit a streaming, no-replay event feed.
2. **If RTP quality matters, build a separate event listener**, in a later task, limited to MOS and quality percentage
   (plus a packet-count qualifier), using an account restricted exactly as in the permission experiment. Accept that
   events are lost while the listener is down (document it, or add CDR as a second source).
3. **Before that, test more:** longer and higher-concurrency runs (100 or more concurrent legs), a codec other than PCMU,
   transcoding and SRTP, a real Agent consuming histogram or distribution values, and a decision on distribution via
   DogStatsD versus per-Agent histograms. Do not publish jitter or packet loss as emitted.
4. **Defer** if call-quality monitoring is not a requirement: nothing in the current metric set depends on it.

### 11.6 Commands executed and results (this session)

| Command | Result |
|---|---|
| `pwd`, `git rev-parse --show-toplevel`, `git branch --show-current`, `git rev-parse HEAD`, `git status --short`, `git remote -v` in both repositories | as in 11.1 |
| `pytest tests -q -p no:cacheprovider` (in `integration/`) | 318 passed, 15 skipped |
| `FS_RUNTIME_TESTS=1 pytest tests -q -p no:cacheprovider` | 333 passed |
| `python -m tests.runtime.rtp_events_experiment <file>` | completed (section 11.3) |
| `ddev -x test freeswitch` (`integrations-extras`) | 318 passed, 15 skipped |
| `ddev -x test --fmt freeswitch`, `ddev -x test -s freeswitch` | 15 files formatted, all checks passed, mypy: no issues in 22 source files |
| `ddev -x validate metadata / config / models / readmes / package / eula freeswitch`, `validate codeowners` | all passed |
| `git diff --check` | exit 0, no output |
| Validators needing an `integrations-core` checkout; any real Datadog backend or intake | **not run / blocked** |

### 11.7 Files changed in this session

`integration/datadog_checks/freeswitch/check.py` was not changed in this session. Changed: `integration/README.md`
(restart limitation wording), `integration/tests/test_metrics.py` (two audit tests),
`integration/tests/runtime/testbed.py` (`rtp-timer-name`, `edge_extra`/`common_extra`, `cap_net_admin`, event-only
research account, tone extension), new `integration/tests/runtime/rtp_events_experiment.py`,
`docs/evidence/data/rtp_hangup_events.json`, `docs/ROADMAP.md` (E1 to E3 status cells), this report. The git state is
unchanged apart from these files: branch `main`, HEAD `58e55cb`, nothing staged or committed.

## 12. Fourth session: RTP event collector feasibility and stress testing

Research only. No listener, metric or check change was made. All runs used new disposable FreeSWITCH containers
(`fs-metrics-testbed-*`) from the lab's already-built image, real SIP and RTP over the container loopback, with local
test accounts only. The existing lab container stayed `Up (healthy)`; no carrier, external call or production
credential was involved.

### 12.1 State and environment

| | |
|---|---|
| Repository / branch | `/Users/zoos/Desktop/Zoos/datadog-freeswitch`, `main` |
| HEAD | **`4d28a2d` ("mnk")**, committed by the user at 23:20 during this session, on top of `58e55cb`. It already contains the earlier sections of this report and the experiment scripts as they were at that moment. I made no commit and did not touch Git history; the files below are changed on top of that commit |
| Host | 8 CPUs, 6.2 GB available to Docker (arm64, native image) |
| Earlier RTP evidence | section 11: 10 concurrent calls, `rtp-timer-name=soft` required, MOS 4.50 clean |

### 12.2 Event delivery under concurrency (PCMU, real bidirectional RTP, 10 s calls)

Command: `python -m tests.runtime.rtp_load_experiment <out.json> load`. Testbed: `max_sessions=3000`,
`sessions_per_second=1000`. One call = two legs (outbound leg and inbound leg); each leg yields one
`CHANNEL_HANGUP_COMPLETE`. Test ids (`X-Test-Id` SIP header) stayed in the experiment process.

| Calls | Concurrent sessions at peak (legs) | Events expected | Received | Missing | Duplicates | MOS (min / median / max) | Lag ms (min / median / max) | Container CPU / memory during calls | Datagrams per second (container) |
|---|---|---|---|---|---|---|---|---|---|
| 13 | 26 | 26 | 26 | 0 | 0 | 4.5 / 4.5 / 4.5 | 23 / 25 / 27 | 17% / 240 MiB | 1,159 |
| 25 | 50 | 50 | 50 | 0 | 0 | 4.5 / 4.5 / 4.5 | 16 / 20 / 42 | 25% / 456 MiB | 2,409 |
| 50 | 100 | 100 | 100 | 0 | 0 | 4.5 / 4.5 / 4.5 | 20 / 37 / 47 | 18% / 937 MiB | 4,686 |
| 75 | 150 | 150 | 150 | 0 | 0 | 4.5 / 4.5 / 4.5 | 26 / 38 / 48 | 19% / 1.39 GiB | 6,903 |

The 100-leg level and a 150-leg level ran successfully on this host; nothing above 150 was attempted for media calls.
Quality percentage was 100 in every event. Every event was 9.5 to 10.1 KB. Memory growth was about 9 MiB per leg in the
FreeSWITCH container (media and session buffers, not the listener). The observer's own memory could not be isolated
(`ru_maxrss` did not move); it only parsed and discarded events.

### 12.3 Media configurations (5 calls of about 12 s each, 10 events per scenario)

Command: `... rtp_load_experiment <out.json> codecs`. Profiles offered `PCMU,PCMA,speex@8000h@20i,speex@16000h@20i,L16`.
Datagrams per second measured with `/proc/net/snmp` inside the container (about 540: two directions x 5 calls x 50 pps
plus RTCP), confirming bidirectional flow.

| Scenario | Codec / rate seen | In packets per leg | MOS | Quality | `nan` / missing |
|---|---|---|---|---|---|
| PCMU baseline | PCMU 8000 | 607 to 617 | 4.50 (10 of 10) | 100 | none |
| PCMA | PCMA 8000 | 606 to 616 | 4.50 | 100 | none |
| Speex wideband | SPEEX 16000 | 606 to 615 | 4.50 | 100 | none |
| L16 | L16 8000 | 608 to 617 | 4.50 | 100 | none |
| SRTP (SDES, `rtp_secure_media=true`) PCMU | PCMU, `AEAD_AES_256_GCM_8` | 607 to 616 | 4.50 | 100 | none |
| SRTP PCMA | PCMA, `AEAD_AES_256_GCM_8` | 610 to 622 | 4.50 | 100 | none |
| Transcoding PCMU to PCMA (bridge, 3 calls) | per leg PCMU or PCMA | 489 to 498 | 4.50 on legs with media | | `nan` on the 6 legs that never received audio (the far end only sleeps or only receives) |

Only codecs built into the image were available (no Opus, G.722, G.729). Counts matched duration x 50 within about 2
percent in every case. The `nan` cases are explained by topology (a leg that receives no audio has no MOS), not by a
codec or SRTP limitation. Earlier findings on `in_jitter_loss_rate`, jitter variance and `in_skip_packet_count` were not
revisited and stand.

### 12.4 Listener resilience

Commands: `... rtp_load_experiment <out.json> resilience` and `python -m tests.runtime.rtp_stress_extra <out.json>`.
Raw results: [data/rtp_load_resilience.json](data/rtp_load_resilience.json).

| Test | Result |
|---|---|
| Disconnect while 10 calls (20 legs) were up; all hung up during the gap; reconnect | **0 of 20 events delivered** after reconnect. FreeSWITCH keeps no queue and replays nothing |
| Calls ended after the reconnect | 10 of 10 delivered |
| Loss indicator: polled `sessions.created` delta versus hangup events observed, with a 4-call (8-leg) gap | 30 sessions created, 22 events observed, difference **8 = the true number of unobserved legs**. Controlled case where every session produces one hangup event; a failed originate also produces one, so the difference is an estimate, not an exact count |
| Duplicate delivery: two simultaneous subscriptions | each received all 6 events (one copy per subscription); no duplicates on a single subscription in any run. Dedupe by event `Unique-ID` is needed only when two subscriptions overlap |
| Burst: 150 legs hung up in one command | 150 of 150 received; first at 0.04 s, last at 0.09 s; 1.46 MB |
| Slow consumer, 200 legs unread then read | 200 of 200 |
| Slow consumer, 2,400 legs (21.7 MB) unread for the whole generation (about 32 s) then read | 2,400 of 2,400, 2,400 unique, not disconnected by the server; FreeSWITCH container 432 MiB at that moment |
| FreeSWITCH graceful restart (`docker restart -t 2`) with 5 calls up | the old subscription received **10 of 10** hangup events during shutdown, then disconnected; a new subscription after restart received 6 of 6 |
| FreeSWITCH hard kill (`SIGKILL`) with 10 calls up | **0 events**; subscription disconnected. In-flight call measurements are lost entirely |
| Wrong password | `-ERR invalid` (no events, connection refused) |
| Event-only account subscribing to `CHANNEL_CREATE` | `-ERR permission denied`; the permitted event delivered 3 of 3 legs |
| Missing or `nan` fields | seen in real events: `nan` MOS and quality (no inbound media), absent `rtp_audio_*` variables on unanswered calls. A consumer must treat these as "no measurement". Synthetic malformed events were not generated |

Not tested: server-side drop of a very slow subscriber beyond 21.7 MB, event rates above a few hundred per second,
exact duplicate or out-of-order conditions (none occurred), time skew between FreeSWITCH and the listener host.

### 12.5 Privacy and data minimization

A hangup event had 229 fields and 9.5 KB; 28 fields (about 12 percent of the bytes) are RTP statistics. The rest
carries caller and callee numbers, names, call and SIP identifiers, SDP, media addresses and user variables. No
server-side projection of fields was found for `event plain`; filtering has to be done in the consumer.

Minimal design (requirements, not an implementation):
1. **Fields kept per event:** `variable_rtp_audio_in_mos`, `variable_rtp_audio_in_quality_percentage`,
   `variable_rtp_audio_in_packet_count`, `Call-Direction`, and optionally `variable_rtp_use_codec_name`. Nothing else.
2. **Never retained or forwarded:** any identity, number, name, address, SDP, UUID or `X-` header; no raw event is stored
   or logged; error logs contain counters and fixed text only. `Unique-ID` is used in memory only for short-window dedupe.
3. **Tags:** `direction` (2 values) and optionally `codec` (small, bounded; unknown values map to `other`). No
   `hangup_cause` tag until bounded by an allowlist; no extension, caller, UUID or IP.
4. **Account:** the event-only account verified in 12.4 (`esl-allowed-events=CHANNEL_HANGUP_COMPLETE`,
   `esl-allowed-api=none`), separate from the polling check's `uptime,show` account.
5. **Bounded memory:** parse each event as it arrives, extract the five values, discard the rest; a fixed-size queue
   between reader and emitter; when full, drop the newest and count the drop. Bounded read size per event.

### 12.6 Decision

| Criterion | Evidence | Assessment |
|---|---|---|
| Reliability under load | 150 of 150 and 2,400 of 2,400 events, zero duplicates, 7 to 56 ms lag | good in this environment |
| Meaningful across media | MOS and quality consistent over PCMU, PCMA, Speex 16 kHz, L16 and SRTP; correct fall under impairment (section 11) | good for FreeSWITCH's own estimate |
| Detect incomplete collection | no replay; hard kill loses everything; polled `sessions.created` minus events observed approximates the loss in a controlled run | needs an explicit loss indicator; workable |
| Operational complexity | a long-lived streaming process or a non-polling Agent check class, reconnect logic, its own account | moderate; cannot share the polling check |
| Privacy | event carries caller data; account can be limited to one event and no API; fields can be dropped immediately | acceptable with the minimal design above |
| Real-time vs per-call | values exist only after hangup; a 30-minute call reports nothing until it ends | a post-call quality distribution, not live monitoring |

**Candidate metrics (recommendations only; none registered or emitted):**

| Metric | Field | Type, unit | Aggregation | Tags | Emit when |
|---|---|---|---|---|---|
| `freeswitch.rtp.mos` | `rtp_audio_in_mos` | distribution (preferred, via DogStatsD) or histogram, score | per call leg at hangup; percentiles meaningful only across hosts with a distribution | `direction`, `codec` | value finite and `in_packet_count` at least 50 |
| `freeswitch.rtp.quality` | `rtp_audio_in_quality_percentage` | distribution or histogram, percent | as above | same | same |
| `freeswitch.rtp.events.received` | listener counter | count | per interval | none | always |
| `freeswitch.rtp.events.dropped` | listener counter (queue full, parse failure) | count | per interval | `reason` (bounded) | always |
| `freeswitch.rtp.events.skipped` | no media, `nan` or missing | count | per interval | none | when skipped |
| `freeswitch.rtp.listener.connected` | listener state | gauge 1/0 | per run | none | always |
| `freeswitch.rtp.loss_estimate` (derived in Datadog) | `sessions.created` increase minus `events.received` | formula | per interval | none | dashboard only |

Not recommended as metrics: jitter variance, `in_jitter_loss_rate`, `in_skip_packet_count`, `in_flaw_total`.

### 12.7 Known gaps (not tested)

No Datadog Agent or backend consumed any of this (the Agent was not involved in section 12). No remote or real SIP
endpoint, only FreeSWITCH-to-FreeSWITCH loopback. No soak longer than a few minutes, calls longer than 20 s were only
tested earlier, no more than 150 simultaneous legs, no Opus or G.722, no video, no conference, no media bypass, no
packet-loss ratios from RTCP receiver reports. Impairment was only `tc netem` on the container loopback (section 11).
Event schemas of other FreeSWITCH versions were not examined. A passing local run here is not Datadog validation.

### 12.8 Recommendation

**Conduct one more focused experiment, then implement a separate listener.** The collection path is dependable enough
(no loss or duplicates up to 150 legs and 2,400 buffered events, consistent MOS across codecs and SRTP), but two points
that decide the design are unproven: how a real Agent aggregates per-call values (distribution through DogStatsD versus
histogram in a check), and whether the listener stays correct over a soak of at least one hour with mixed call lengths and
a restart. Do not extend the polling check. Defer RTP entirely only if call-quality distributions are not a requirement.

### 12.9 Commands, results and files (this session)

| Command | Result |
|---|---|
| `python -m tests.runtime.rtp_load_experiment <out> load` | completed: levels in 12.2 |
| `python -m tests.runtime.rtp_load_experiment <out> codecs` | completed: 12.3 |
| `python -m tests.runtime.rtp_load_experiment <out> resilience` | completed: 12.4 |
| `python -m tests.runtime.rtp_stress_extra <out>` | completed: slow consumer and hard kill |
| `pytest tests -q -p no:cacheprovider` (`integration/`) | 318 passed, 15 skipped |
| `FS_RUNTIME_TESTS=1 pytest tests -q -p no:cacheprovider` | 333 passed |
| `ddev -x test freeswitch` (`integrations-extras`) | 318 passed, 15 skipped |
| `ddev -x test --fmt freeswitch`, `ddev -x test -s freeswitch` | 17 files formatted, ruff clean, mypy no issues in 24 source files (a mypy complaint in my new script was fixed) |
| `git diff --check` | exit 0 |
| `integrations-core` validators, Agent, backend | not run |

Files added or changed on top of `4d28a2d`: `integration/tests/runtime/rtp_load_experiment.py` (the file in the commit is
an earlier draft; this is the final, lint-clean version; the testbed `codecs` option was already in the commit), new `integration/tests/runtime/rtp_stress_extra.py`, new
`docs/evidence/data/rtp_load_resilience.json`, this report. `check.py`, `esl.py`, metadata and configuration were not
touched.

## 13. Fifth session: Datadog Agent path and one-hour soak

Research only. No production metric was implemented or registered; every metric below uses the throw-away prefix
`experiment.rtp.` and went to a local sink. No listener was built: `rtp_soak_experiment.py` is a temporary harness.

### 13.1 State

| | |
|---|---|
| Repository / branch | `/Users/zoos/Desktop/Zoos/datadog-freeswitch` (`origin` = `github.com/shivamsancc/datadog-freeswitch`), `main` |
| Commit `4d28a2d` ("mnk") | belongs to this repository (author `shivamzoos`, 2026-10-12 23:20 +0530); it contains the report up to section 11 and the earlier runtime scripts. Not amended |
| HEAD at the end | `7ca0196` ("Fix HTML encoding in README.md"): three README-only commits made by the user between my runs. I made no commit, reset, rebase or checkout |
| Working tree at the end | modified `docs/evidence/METRIC_VERIFICATION_REPORT.md`, `integration/tests/runtime/rtp_load_experiment.py`; untracked `docs/evidence/data/{rtp_dogstatsd_types,rtp_load_resilience,rtp_soak_1h}.json`, `integration/tests/runtime/{rtp_agent_lib,rtp_dogstatsd_types,rtp_soak_experiment,rtp_stress_extra}.py` |
| Lab | `datadog-freeswitch-freeswitch-1` untouched (`Up (healthy)`); all runs used `fs-metrics-testbed-*`, `fs-exp-agent-*`, `fs-exp-sink-*` containers on private Docker networks, all removed afterwards |

### 13.2 Proposed measurement contract (candidate, not registered)

| Measurement | Source field (hangup event) | Meaning | Unit and accepted range | Emit when | Otherwise |
|---|---|---|---|---|---|
| MOS | `variable_rtp_audio_in_mos` | FreeSWITCH's own estimate of what this leg **received** | score; finite, `0 < x <= 5` (observed 0.99 to 4.50) | field present, finite, in range, and `in_packet_count >= 50` (at least about one second of media) | count the leg under an `outcome` tag, emit no value |
| Quality | `variable_rtp_audio_in_quality_percentage` | FreeSWITCH's own percentage derived from packet flaws | percent; finite, `0 <= x <= 100` | same conditions as MOS | same |
| Packet count | `variable_rtp_audio_in_packet_count` | how much media backs the sample | packets | **qualifier only**, used for the gate above; not a score | not emitted as a quality value |

Distinguishing a bad call from no data: a legitimately bad call has a finite MOS (observed 0.99 to 1.0 at 30% loss)
**and** packets above the gate; "no data" is `nan`, absent, or fewer than 50 packets. `nan` is never turned into 0. The
listener therefore also counts outcomes (`valid`, `no_media` including 0 packets, `short`, `missing`, `invalid`).
Allowed tags: `direction` (`inbound`, `outbound`, else `other`) and a normalized `codec` (`pcmu`, `pcma`, `speex`, `l16`,
else `other`); in the soak exactly 6 combinations appeared. UUIDs, caller IDs, extensions, SIP identities and raw
headers are never tags.

### 13.3 DogStatsD metric types with a real Agent

`python -m tests.runtime.rtp_dogstatsd_types <out.json>`; Agent `datadog/agent:7.80.3` (DogStatsD on, non-local
traffic on, `use_v2_api.series=false`, serializer compression `zlib`), intake and sketch endpoints pointed at a local
sink container; the sink's received payloads (`/api/v1/series`, `/api/beta/sketches`) were decoded locally.
30 real samples (6 clean calls from this run plus 24 sanitized impaired-call events from section 11; MOS 0.99 to 4.50, sum
93.63), sent as gauge, histogram and distribution, 15 per direction, in one burst. Raw:
[data/rtp_dogstatsd_types.json](data/rtp_dogstatsd_types.json).

| Type (wire format) | What the Agent delivered | Samples visible afterwards | Consequence |
|---|---|---|---|
| Gauge `name:4.5\|g\|#direction:outbound` | one point per tag set per 10 s interval: the last value (1.31 and 1.49) | 1 of 15 | **Unusable for per-call data**: 14 of 15 calls vanish |
| Histogram `...\|h\|#...` | `.avg`, `.median`, `.max`, `.95percentile` as gauges and `.count` as a rate (1.5 per second over 10 s = 15), per host and tag set | 15 counted; avg 3.275 and 2.967, max 4.5 | percentiles are computed in the Agent and **cannot be merged across hosts or listeners**; 5 series per tag set |
| Distribution `...\|d\|#...` | an Agent-side sketch per tag set per interval with count, min, max, avg, sum (15, 1.0 to 4.5, avg 3.275, sum 49.13 for outbound; 15, 0.99 to 4.5, avg 2.967, sum 44.5 for inbound; sums add to 93.63) | 15 counted | merges across listeners; percentiles are computed by the backend |

Observed behaviour:
- **Acceptance** is visible only through the Agent's own counters (`agent status`, section DogStatsD: Metric Packets,
  Metric Parse Errors, Udp Packets). UDP sending itself proves nothing.
- **Invalid input:** 11 deliberately bad lines gave 6 parse errors (`abc`, empty value, `4.5.5`, `1e999`, unknown type `x`,
  no type). `nan`, `inf` and `-inf` were **accepted as packets and silently not aggregated** in histogram and distribution
  (a mix of 4, nan, 4, inf, 2 produced count 3, avg 3.33); a gauge kept the last finite value (2); a series made only of
  `nan` produced nothing. A negative value (-1) **was** aggregated: the Agent does not range-check. So the listener must
  validate and count rejected samples itself; the Agent will not report them.
- **Flush boundary:** samples sent 12 s apart landed in separate 10 s intervals (timestamps 10 s apart); a gauge in
  the later interval was independent of the earlier one.
- **Tags:** each distinct tag combination is its own series (direction x codec gave 6 distribution series, one sample each).
  Unbounded tag values would create series without limit; none were used.
- **Not verified:** backend percentiles (p50, p95, p99) of a distribution, backend merging across Agents, retention and
  billing of distributions (they require a Datadog backend and organization; none consumed these samples). Locally only
  count, min, max, avg and sum of the Agent sketch were decoded. The sketch bins were not turned into quantiles here.

**Recommendation by use case**

| Use case | Type | Why |
|---|---|---|
| Individual post-call measurement | none as a metric | one series per call is unbounded cardinality; per-call detail belongs in logs or events, which would carry caller data and are out of scope |
| Fleet-wide distribution of call quality | **distribution** | one tag set per direction and codec; percentiles are correct across hosts and listeners; count is the number of samples |
| Per-listener trend and operational summary | histogram (acceptable) or distribution | histogram gives `.avg/.median/.max/.95percentile/.count` immediately with no backend percentile feature, but is per Agent only and costs 5 series per tag set |
| Never | gauge | loses all but one call per interval |

### 13.4 One-hour soak

Command: `python -m tests.runtime.rtp_soak_experiment <out.json> 3600`
(`Testbed(max_sessions=3000, sessions_per_second=1000, rtp_end=30000, codecs='PCMU,PCMA,speex@8000h@20i,speex@16000h@20i,L16')`).
Raw: [data/rtp_soak_1h.json](data/rtp_soak_1h.json) (timeline every 30 s, call categories and times, no identifiers).

**Load profile:** Poisson arrivals at 0.8 calls per second, at most 25 concurrent calls (peak 38 legs observed); mix:
70% media calls (tone, 4 to 25 s; PCMU 50%, PCMA 20%, Speex 16 kHz 10%, SRTP PCMU 10%, SRTP PCMA 10%), 15% `park` with
no audio (5 s), 8% answered then hung up after 0.3 s, 7% unanswered (486). Real SIP and RTP on the container loopback.
**Duration 3,682 s** (3,600 s generation plus 82 s drain and flush). Not interrupted; no test failure.

**Planned interruptions (all executed):**

| At | Interruption | Observed |
|---|---|---|
| 1,188 s | listener disconnect, held 60 s | gap 56.7 s measured by the listener |
| 1,800 s | Agent container restart | DogStatsD unreachable for the Agent boot period; listener rebuilt its UDP socket on the new published port |
| 2,413 s | FreeSWITCH graceful restart (calls paused 12 s before) | listener reconnected after 7.1 s gap; polling check and event account recovered |
| 2,992 s | listener "process" restart (state reset), held 15 s | gap 15.1 s; dedupe memory cleared |

**Reconciliation**

| Quantity | Value |
|---|---|
| Calls started (ground truth) | 2,793 (media 1,929; park 429; short 235; busy 200); 0 failed originates |
| Hangup events expected (2 per call) | 5,586 |
| Events received by the listener | 5,471 (missing 115, 2.06%) |
| Missing explained by the three listener gaps (calls whose hangup fell in a gap) | 122 predicted vs 115 observed; by category: valid-media legs 77, no-media legs 30, unanswered legs 8 (sum 115). The 7-event difference is boundary timing at gap edges |
| Duplicates (same `Unique-ID`) | 0 |
| Queue | max depth 4 of 2,000; 0 dropped; 0 parse errors; 0 authentication failures; 3 connect failures during restarts, 4 reconnects counted |
| Valid quality samples expected / submitted by the listener | 3,858 / 3,781 (77 lost in gaps, as above) |
| Valid samples observed by the Agent (sketch counts) | **3,771** (10 fewer than submitted) |
| Outcome counters submitted / observed by the Agent | 5,471 / 5,455 (16 fewer); valid 3,781 / 3,771, no media 1,298 / 1,294, missing 392 / 390 |
| Omitted measurements | `no_media` 1,298 (park and 0.3 s calls, all 0 packets), `missing` 392 (unanswered calls: no `rtp_audio_*` variables), `invalid` 0, `short` 0 |
| Tag sets seen at the Agent | 6 (`direction` x `codec` of PCMU, PCMA, Speex), stable for the whole run; no other values |
| Event processing latency (event timestamp to DogStatsD send) | median 34.5 ms, p95 71.8 ms, max 198 ms |
| Resources | listener process RSS 10.7 to 15.9 MB over the hour; Agent memory 80 to 110 MiB, CPU about 1%; FreeSWITCH container 243 MiB at start, up to 947 MiB, ending near 750 MiB (it oscillated, no monotonic growth; peak concurrency 38 legs), CPU 5 to 35% of one core-equivalent |
| Polling check (same Agent) | `sessions.created` increase seen by the Agent: 5,554 against 5,586 sessions created (the Agent restart removes its in-memory baseline and one interval) |

**Findings**
1. The 10 and 16 missing Agent samples coincide with the Agent restart: UDP is fire-and-forget; the listener's
   `send_errors` stayed 0 while samples were lost. **Samples lost between listener and Agent are invisible to the
   listener** and can only be inferred (submitted counters versus the Agent). Loss of 0.26% of valid samples in this run
   was entirely attributable to the planned Agent restart; no loss was seen otherwise.
2. The Agent-visible `listener.connected` gauge (sent every 10 s) was 0 at 1,189 to 1,239 s (6 samples), at 2,419 s and
   at 2,989 and 2,999 s. A gap shorter than the gauge interval can be missed entirely (the 7 s FreeSWITCH restart gap was
   caught by only one sample).
3. Every sample from an interrupted period is absent; nothing is recoverable. The interruption must be reported, not
   hidden.

### 13.5 Loss-detection strategy

| Mechanism | Evidence | Authoritative? | Verdict |
|---|---|---|---|
| `listener.connected` gauge (1/0) | seen at the Agent for every gap of 15 s or more; one 7 s gap seen by luck | for state only; coarse | **use**, plus the two below |
| `listener.reconnects` count and `listener.gap_seconds` (disconnected time in the interval) | harness counted 4 reconnects and gap durations 56.7, 7.1, 15.1 s exactly; **not sent to the Agent in this run** (only kept in the harness) | yes: measured by the listener; survives short gaps | **use** (needs a small follow-up test that they reach the Agent) |
| Events expected versus received | population is not knowable from FreeSWITCH; here known only because the generator was the source | no | cannot be done in production |
| Polled `sessions.created` increase minus events received | 5,554 minus 5,471 = 83 against 115 true lost events (and 32 sessions the check itself missed around the Agent restart); includes failed originates | **no** | **dashboard hint only, never labelled as loss** |
| Bounded dropped-event counter | 0 in the soak; 17,950 of 20,000 dropped under a deliberate overload test (13.6), conservation held | yes for local queue overflow | **use** |
| Explicit `unknown quality` outcomes (`no_media`, `missing`, `short`, `invalid`) | counted per outcome, 1,690 in the soak | yes for received events | **use**; keeps "no data" distinct from "bad" |

An interrupted period must be flagged (gauge 0, gap seconds above 0) so that charts of "calls with MOS below 3" are not
read as complete across it. No extra permission, no raw caller data and no second event source are needed for these.

### 13.6 Queue, privacy and size assumptions (isolated bench)

Synthetic event of the observed shape: 229 fields, 11.1 KB (larger than the 9.5 KB observed), 28 RTP fields, the 7
wanted fields set. Same `WANTED` filter and bounded queue as the harness; run while the soak was in progress (so
throughput is a lower bound):

| Check | Result |
|---|---|
| Parse only the wanted fields | 20,000 events in 4.0 s (about 5,000 per second); traced Python allocation peak 33.9 KiB |
| Overload (producer 20,000 events at once, consumer 200 per second, queue 2,000, drop-newest) | consumed 1,691, dropped 17,950, still queued 359; consumed + dropped + queued = 20,000; queue never above 2,000 |
| Oversized event | `_read` rejects a body above 256 KiB (`event too large`) and the reader reconnects (not exercised with a real FreeSWITCH) |
| Raw events | never stored, logged or forwarded; the harness keeps 7 fields per event for the duration of one queue slot |
| Secrets in diagnostics | harness log lines contain counters and sizes only; a text search of the soak log for "password" or "secret" returned 0 matches; credentials are only in memory and in the disposable container's temporary config, removed at teardown |
| Reconnect | exponential backoff 1 to 5 s; authentication failure is counted, not retried at full speed |

### 13.7 What is verified locally, and what is not

| Verified locally (disposable FreeSWITCH, real Agent 7.80.3, local sink) | Still depends on a real backend or more work |
|---|---|
| per-call values reach the Agent over DogStatsD; types behave as in 13.3 | backend percentiles of distributions (p50, p95, p99), merging across Agents, billing |
| `nan`, `inf` and empty values are not aggregated; the Agent does not range-check | UDP loss under real load on a busy host (here only the planned Agent restart) |
| 3,600 s soak: 5,471 of 5,586 events, 0 duplicates, 0 drops, gaps fully accounted | multi-hour or multi-day behaviour, FreeSWITCH memory pattern beyond one hour |
| restart of listener, Agent and FreeSWITCH; recovery without manual action | other FreeSWITCH versions; remote (non-loopback) endpoints, real carriers, Opus or G.722 |
| tag set stayed at 6 combinations | `listener.reconnects` and `listener.gap_seconds` reaching the Agent (counted in the harness only) |

### 13.8 Commands, results and files

| Command | Result |
|---|---|
| `git rev-parse`, `git status --short`, `git show -s 4d28a2d` | state in 13.1 |
| `python -m tests.runtime.rtp_dogstatsd_types <out>` | completed (13.3) |
| `python <scratch>/nan.py` (Agent only: nan and inf in g, h, d) | completed (13.3) |
| `python -m tests.runtime.rtp_soak_experiment <scratch>/soak_dry.json 300` (dry run, not a substitute) | completed; found and fixed a greeting-handling defect in the harness before the real run |
| `python -m tests.runtime.rtp_soak_experiment <scratch>/soak_1h.json 3600` | **completed, 3,682 s** |
| `python -m tests.runtime.rtp_queue_bench` (same bench, kept in the repository; the numbers in 13.6 came from the scratch copy while the soak was running) | numbers in 13.6 |
| `pytest tests -q` (via `ddev -x test freeswitch`) | 318 passed, 15 skipped |
| `FS_RUNTIME_TESTS=1 pytest tests -q -p no:cacheprovider` | 333 passed |
| `ddev -x test --fmt freeswitch`, `ddev -x test -s freeswitch` | 20 files formatted; ruff clean; mypy no issues in 27 source files (earlier findings in the new scripts were fixed) |
| `ddev -x validate metadata / config / models / readmes / package / eula freeswitch`, `validate codeowners` | all passed |
| `git diff --check` | exit 0 |
| `integrations-core` validators, any Datadog backend | not run |

`tc netem` could not be reinstalled in this session (the package mirror answered 403), so no new packet-loss
impairment was generated; impaired values came from the sanitized events of section 11. The soak ran clean media only,
so it shows delivery and accounting, not MOS under impairment.

Files added in this session: `integration/tests/runtime/{rtp_agent_lib,rtp_dogstatsd_types,rtp_soak_experiment}.py`,
`docs/evidence/data/{rtp_dogstatsd_types,rtp_soak_1h}.json`; this report. `check.py`, `esl.py` and the shipped metrics
were not touched.

### 13.9 Decision

**1. Ready to begin a separately scoped listener implementation**, with these conditions written into its scope:
- metric type **distribution** (via DogStatsD) for MOS and quality, `direction` and normalized `codec` as the only tags,
  gated at 50 packets, with outcome counters for everything not emitted;
- the loss indicators of 13.5 (connected gauge, reconnect counter, gap seconds, drop counter) as part of the first
  version, and the polled-check difference only as a labelled hint;
- the restricted event-only account verified in section 11, no raw event retention, bounded queue with drop-newest;
- acceptance includes the two items not provable locally: backend percentile verification in a real (non-production)
  Datadog organization, and a check that the gap counters reach the Agent. Those are the user's decision to run.

## 14. Sixth session: the separate RTP listener prototype

A local prototype in `rtp-listener/` (new, untracked). It is **not** part of the polling integration, registers nothing
in `integration/metadata.csv`, and is not a production release.

### 14.1 Workspace state

| | |
|---|---|
| Research repository | `/Users/zoos/Desktop/Zoos/datadog-freeswitch`, `main`, HEAD `7ca0196` (unchanged; user commits `4d28a2d` and the three README commits are untouched) |
| Integration mirror | `/Users/zoos/Desktop/Zoos/integrations-extras`, `add-freeswitch-integration`, HEAD `3fefc5d`, 14 uncommitted entries (the polling integration mirror, unchanged in behaviour) |
| Existing integration | `integration/datadog_checks/**` and `metadata.csv`: **no diff** from this session; command allowlist, permissions and metrics unchanged |
| Pre-existing uncommitted work | preserved: modified report and `rtp_load_experiment.py`, experiment scripts, evidence JSON |
| Discrepancy | none relative to the earlier baseline; HEAD had already advanced by user commits before this session |

### 14.2 Design and component boundary

```
FreeSWITCH --ESL (event-only account)--> EventClient --> reader thread --> bounded queue (drop-newest)
                                                                   |
                         emitter thread: classify -> StatsdClient --UDP--> local Datadog Agent
                                           \-> health report every N s (counts + gauges + cumulative totals)
```

| Module | Responsibility | Independent test |
|---|---|---|
| `config.py` | validated settings from `FS_RTP_*`; password never in repr or errors; shared-password mode needs an explicit opt-in | `test_config_statsd.py` |
| `esl.py` | receive-only ESL client: `userauth`, `event plain CHANNEL_HANGUP_COMPLETE`, reads; strict permission check; fixed-message errors with bounded `reason`; header 8 KiB and event 256 KiB limits | `test_esl_client.py` (simulated server) |
| `events.py` | keep 7 fields of an event, URL-decode, ignore everything else; never raises | `test_events_quality.py` |
| `quality.py` | validation, outcome classification, tag normalization (pure) | `test_events_quality.py` |
| `statsd.py` | DogStatsD distribution/gauge/count, batching to 1,400 bytes, socket replaced after an error with one retry | `test_config_statsd.py` |
| `listener.py` | threads, bounded queue, reconnect with bounded exponential backoff and jitter, gap accounting, dedupe (5,000 ids in memory), health telemetry, clean shutdown | `test_listener.py` |

Separation from the polling check: separate package and process, its own account (`esl-allowed-api=none`,
`esl-allowed-events=CHANNEL_HANGUP_COMPLETE`), no import of `datadog_checks`, no shared lifecycle. It reuses no ESL code
from the check (the check's client is a request/response design with a different permission policy); the runtime tests reuse
only the disposable test environment helpers in `integration/tests/runtime`.

### 14.3 Metric contract (prototype names)

| Metric | Type | Unit | Tags | Source / emitted when |
|---|---|---|---|---|
| `freeswitch.rtp.mos` | distribution | score | `direction`, `codec` | `variable_rtp_audio_in_mos`, outcome `valid` |
| `freeswitch.rtp.quality_percentage` | distribution | percent | `direction`, `codec` | `variable_rtp_audio_in_quality_percentage`, outcome `valid` |
| `freeswitch.rtp.listener.connected` / `.queue_depth` | gauge | 1/0, events | static tags | each health interval |
| `.listener.events`, `.duplicates`, `.parse_errors`, `.dropped`, `.reconnects`, `.connect_errors`, `.send_errors` | count | events | static | deltas per interval |
| `.listener.gap_seconds` | count | second | static | disconnected time in the interval, including a gap that is still open |
| `.listener.samples` | count | events | `outcome` (7 values) | deltas per interval |
| `.listener.total` | gauge | varies | `counter` (17 names) | cumulative since process start; survives lost datagrams |

Validation (documented in `rtp-listener/README.md`): packets decide first (`no_media` 0, `short` below `min_packets`=50),
then MOS and quality must be present, numeric, finite and in range (MOS 0.5 to 5, quality 0 to 100); otherwise one of
`missing_fields`, `invalid_value`, `non_finite`, `out_of_range`. Nothing is substituted with 0. Packet count is a
qualifier, not a metric. Tags: `direction` in `inbound|outbound|other`; `codec` in an allowlist plus `other` and `unknown`,
so at most 3 x 12 series per distribution; no UUID, SIP identity, caller ID, extension or event field is ever a tag.

### 14.4 Queue, reconnect and shutdown behaviour

- Queue: bounded (default 2,000), drop-newest, `dropped` counted; exercised in unit tests (queue 5, 30 events: exactly 24
  dropped) and with real events (queue 3, slow emitter; events = processed + dropped, no silent loss).
- Reconnect: delay in `[d/2, d]`, `d = min(max, min x 2^(n-1))`; unit-tested at both jitter extremes; authentication
  failure is retried at the same bounded rate and logged at most once per minute per reason, fixed text only.
- Gap: time disconnected (including before the first connection) accumulates; `gap_seconds` is reported while a gap is
  open; events during a gap are not replayed (unit test and real FreeSWITCH test).
- Shutdown: `stop()` ends both threads, drains the queue, sends a final health report and closes sockets; tested that no
  `fs-rtp-*` thread remains and no `api`, `bgapi`, `sendmsg` or `filter` line was ever sent.

### 14.5 Tests and local evidence

| Layer | What it is | Result |
|---|---|---|
| Unit (`rtp-listener/tests`, 112 tests) | SIMULATED ESL server and SIMULATED Agent (UDP sink) | 112 passed (repeated runs stable) |
| Runtime (`rtp-listener/tests/runtime`, 5 tests, `FS_RTP_RUNTIME_TESTS=1`) | real disposable FreeSWITCH 1.10.12, installed `datadog/agent:7.80.3` image, local sink | 5 passed |

Runtime evidence (real FreeSWITCH and real Agent):

| Test | Observation |
|---|---|
| Real path, 12 concurrent media calls over PCMU, PCMA, Speex 16 kHz and SRTP, 3 park, 3 hung up right after answer, 2 unanswered | listener outcomes exactly `valid` 24, `no_media` 12, `missing_fields` 4; submitted 24 MOS and 24 quality; **Agent sketches counted 24 and 24**; tag sets all `direction` x `codec` within `{pcmu, pcma, speex}`; MOS 4.0 to 4.5; no password, SIP name or origination text in logs |
| Disconnect/reconnect | forced disconnect, call hung up inside the gap: the two legs were **not** delivered, events counter unchanged, reconnects +1, gap seconds grew; the Agent saw `total{counter:reconnects}` and `gap_seconds` |
| FreeSWITCH restart | listener reconnected on its own, the next call produced 2 valid samples |
| Agent restart | submitted 38 samples in total, Agent observed 30: **8 lost** between listener and Agent (the hangups that arrived while DogStatsD was down); listener `send_errors` stayed **0**; the cumulative gauge `total{counter:submitted_mos}` seen at the Agent equalled the listener's submitted count, so the loss is computable afterwards |
| Queue overflow with real events | queue 3 and a slow emitter: `events == processed + dropped`, `dropped > 0`, max depth 3 (SIMULATED sink as the statsd target for this test) |

Defects found by the runtime tests and fixed: (1) the FreeSWITCH container's published ESL port changed after a restart;
the testbed now accepts a fixed host port (a deployment concern, not a listener defect); (2) after an Agent restart a
connected UDP socket reports the earlier "port unreachable" on its next send, so the first sample was refused although the
Agent was back; the client now replaces the socket and retries once, counting `send_errors` only if the retry fails.

### 14.6 Remaining limitations

- **Missed events are gone.** The listener reports interruptions, not how many events were missed. There is no exact count of
  lost RTP events, and `sessions.created` minus events is only a rough hint (failed originates also create hangup events).
- **UDP blind spot.** Samples lost between listener and Agent (Agent restart, host overload) are invisible to the listener:
  `send_errors` caught 0 of the 8 lost. Practical mitigation: compare `increase(total{counter:submitted_mos})` with the
  distribution count over a window; this breaks when the listener restarts (its totals reset) and is an estimate.
  Do not call the pipeline lossless.
- **Locally observed aggregation only.** The Agent sketch was decoded for count, min, max, avg and sum; quantiles were not
  computed locally.
- **Backend not verified:** p50, p95 and p99, merging across hosts, query semantics and billing of distributions need a
  non-production Datadog organization; the reproducible procedure and a helper that sends a known distribution and prints
  the exact expected percentiles are in `rtp-listener/README.md` and `rtp-listener/tools/known_distribution.py`. No
  backend result exists and none is claimed.
- Clean loopback media only (impairment was covered in sections 11 and 12); FreeSWITCH 1.10.12 only; no multi-hour run of
  this implementation (the one-hour soak of section 13 used the earlier harness, not this code); the listener runs as a
  plain process with no packaging, service manager, TLS or privilege separation.
- Not production ready: no packaging, no configuration management, no security review beyond the points above.

### 14.7 Commands and results

| Command | Result |
|---|---|
| `git rev-parse --show-toplevel`, `git branch --show-current`, `git rev-parse HEAD`, `git status --short` in both repositories | state in 14.1 |
| `pytest tests -q -p no:cacheprovider` (in `rtp-listener/`, repeated 4 times) | 112 passed, 5 skipped (runtime) each time |
| `FS_RTP_RUNTIME_TESTS=1 pytest tests/runtime -q -s` (four runs) | run 1: 3 passed, 2 failed (ESL port changed after the FreeSWITCH restart, and its knock-on effect); run 2 after the fixed-port fix: 5 passed; run 3: 1 failed (the first sample after the Agent restart was refused, a defect fixed afterwards); run 4 after the socket-retry fix: **5 passed** |
| `ruff format` / `ruff check --config integrations-extras/pyproject.toml` (in `rtp-listener/`) | 16 files unchanged, all checks passed |
| `mypy --config-file=integrations-extras/pyproject.toml --explicit-package-bases` | no issues in 18 source files |
| `FS_RUNTIME_TESTS=1 pytest tests -q` (in `integration/`, after the testbed changes) | 333 passed |
| `ddev -x test freeswitch`, `ddev -x test -s freeswitch` | 318 passed, 15 skipped; 21 files formatted, ruff clean, mypy no issues in 28 source files |
| `ddev -x validate metadata / config / models / readmes / package / eula freeswitch`, `validate codeowners` | all passed |
| `git diff --check` | exit 0 |
| `integrations-core` validators; any Datadog backend | not run |

### 14.8 Files

New: `rtp-listener/` (`fs_rtp_listener/{__init__,__main__,config,esl,events,quality,statsd,listener}.py`,
`tests/{conftest,test_events_quality,test_config_statsd,test_esl_client,test_listener}.py`,
`tests/runtime/{conftest,test_real_path}.py`, `tools/known_distribution.py`, `pyproject.toml`, `README.md`).
Changed test helpers: `integration/tests/runtime/testbed.py` (`esl_host_port`, `codecs`), `rtp_agent_lib.py`
(`statsd_host_port`, `refresh_port`). Not changed: the polling check, its metadata, its allowlist or permissions.

### 14.9 Final Git state

Branch `main`, HEAD `7ca0196`, nothing staged or committed. Untracked: `rtp-listener/`, `docs/evidence/data/*.json` (3),
`integration/tests/runtime/{rtp_agent_lib,rtp_dogstatsd_types,rtp_queue_bench,rtp_soak_experiment,rtp_stress_extra}.py`.
Modified: `docs/evidence/METRIC_VERIFICATION_REPORT.md`, `integration/tests/runtime/{rtp_load_experiment,testbed}.py`.
`integrations-extras` is unchanged in HEAD (`3fefc5d`) with the same uncommitted mirror.
