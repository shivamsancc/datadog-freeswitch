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
