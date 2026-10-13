# FreeSWITCH RTP quality listener (research prototype)

A small, separate process that turns the post-call statistics FreeSWITCH puts in `CHANNEL_HANGUP_COMPLETE` events into
fleet-level quality distributions. **Not a Datadog integration, not registered anywhere, not a production release.** It
is independent of the polling check in `../integration`: separate package, separate account, separate lifecycle.

## What it does

- Connects to the FreeSWITCH Event Socket, authenticates with `userauth`, subscribes **only** to
  `event plain CHANNEL_HANGUP_COMPLETE`, then only reads. It never sends `api`, `bgapi`, `sendmsg` or `filter`.
- Keeps seven fields of each event and discards the rest while parsing (no raw event is stored, logged or forwarded).
- Validates, then sends DogStatsD **distributions** to a local Agent. Listener health goes out as counts and gauges.

## Requirements

- Python 3.10 or newer, standard library only.
- A reachable Event Socket and a local Datadog Agent with DogStatsD enabled (UDP, default port 8125).
- A dedicated FreeSWITCH directory user (see below). The polling check's `uptime,show` account is not reused.

## FreeSWITCH account (least privilege)

```xml
<user id="rtp_listener">
  <params>
    <param name="esl-password" value="..."/>
    <param name="esl-allowed-api" value="none"/>
    <param name="esl-allowed-events" value="CHANNEL_HANGUP_COMPLETE"/>
  </params>
</user>
```

Verified on 1.10.12: this account authenticates, may subscribe to the one event, and is denied every API command and
every other event (`-ERR permission denied`). By default (`strict_permissions`) the listener **refuses** an account whose
advertised `Allowed-Events` is not exactly that event or whose `Allowed-API` is not `none`, before subscribing. Without a
`username` the shared password would be used, which gives full control; that needs
`FS_RTP_ALLOW_SHARED_PASSWORD=1` and is not recommended.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `FS_RTP_HOST`, `FS_RTP_PORT` | `127.0.0.1`, `8021` | Event Socket endpoint |
| `FS_RTP_USERNAME` | none (required) | `user@domain` of the event-only account |
| `FS_RTP_PASSWORD` or `FS_RTP_PASSWORD_FILE` | none (required) | secret; prefer the file; never printed |
| `FS_RTP_STATSD_HOST`, `FS_RTP_STATSD_PORT` | `127.0.0.1`, `8125` | local Agent DogStatsD |
| `FS_RTP_MIN_PACKETS` | `50` | inbound packets needed for a measurement to count (about one second) |
| `FS_RTP_QUEUE_SIZE` | `2000` | bounded queue; drop-newest when full |
| `FS_RTP_HEALTH_INTERVAL` | `10` | seconds between health reports |
| `FS_RTP_BACKOFF_MIN`, `FS_RTP_BACKOFF_MAX` | `1`, `30` | reconnect backoff bounds (seconds) |
| `FS_RTP_CODECS` | `pcmu,pcma,g722,g729,opus,speex,l16,ilbc,amr,amr-wb` | codec tag allowlist |
| `FS_RTP_TAGS` | none | static `key:value` tags added to every metric (not `direction`, `codec`, `outcome`, `counter`, `metric`) |
| `FS_RTP_METRIC_PREFIX` | `freeswitch.rtp` | name prefix |

## Run and stop

```shell
cd rtp-listener
export FS_RTP_USERNAME='rtp_listener@example.local' FS_RTP_PASSWORD_FILE=/path/to/secret
python -m fs_rtp_listener
```

`SIGTERM` or `SIGINT` stops it: the reader stops, queued samples are processed, a final health report is sent, the
connection and UDP socket are closed, and the process exits 0 if all threads ended within 10 s. A subscription lives
only as long as its connection, so nothing is left behind in FreeSWITCH.

## Metrics (prototype names; nothing is registered)

| Metric | Type | Unit | Tags | When |
|---|---|---|---|---|
| `freeswitch.rtp.mos` | distribution | score | `direction`, `codec` | outcome `valid` |
| `freeswitch.rtp.quality_percentage` | distribution | percent | `direction`, `codec` | outcome `valid` |
| `freeswitch.rtp.listener.connected` | gauge | 1/0 | static | every health interval |
| `freeswitch.rtp.listener.queue_depth` | gauge | event | static | every health interval |
| `freeswitch.rtp.listener.events`, `.duplicates`, `.parse_errors`, `.dropped`, `.reconnects`, `.connect_errors`, `.send_errors` | count | event / connection | static | per interval (deltas) |
| `freeswitch.rtp.listener.gap_seconds` | count | second | static | seconds disconnected in the interval, reported while the gap is still open |
| `freeswitch.rtp.listener.samples` | count | event | `outcome` | per interval |
| `freeswitch.rtp.listener.total` | gauge | varies | `counter` | cumulative since the process started |

**Outcomes** (`outcome`, 7 values): `valid`, `no_media` (0 inbound packets), `short` (below `min_packets`),
`missing_fields`, `invalid_value` (not a number), `non_finite` (`nan` or infinity), `out_of_range`.
**Validation:** MOS finite and `0.5 <= x <= 5`, quality finite and `0 <= x <= 100`, only with enough packets. Nothing is
ever replaced by 0. A genuinely bad call (MOS 1.0) is valid; no data is an outcome, not a bad score.

**Tag policy and cardinality.** Only `direction` (`inbound`, `outbound`, `other`) and `codec` (the allowlist, `other`,
`unknown`) are used on quality metrics: at most 3 x 12 = 36 series per metric (6 seen in the soak). `outcome` has 7 values;
`counter` in `.total` has 17. UUIDs, SIP identities, caller IDs, extensions and any other event value are never tags.
Static tags are operator-supplied and checked against a strict pattern.

## Loss and delivery: what it can and cannot tell you

- **No replay.** FreeSWITCH delivers an event only to subscribers connected at that moment. Hangups during a disconnect
  are gone. The listener reports the interruption (`connected`, `reconnects`, `gap_seconds`), not the lost count.
- **Queue.** Full queue drops the newest event and increments `dropped`; overload cannot grow memory.
- **UDP.** DogStatsD has no acknowledgement. `send_errors` counts only errors the operating system reports. A datagram
  lost on the way to the Agent (for example while the Agent restarts) is **not** detected by the listener.
- **Mitigation.** `freeswitch.rtp.listener.total{counter:submitted_mos}` is a cumulative gauge: a later datagram carries
  it even if earlier ones were lost, so `increase(total) - count(distribution)` over a window estimates what was lost
  between listener and Agent. In the local test the gauge matched the listener exactly while the Agent had observed 8
  fewer samples after a restart (see the report). It is an estimate for operators, not a guarantee, and resets with the
  listener process.
- **Not a count of lost RTP events.** Do not derive one from `sessions.created` minus events: failed originates also
  create hangup events.

## Tests

```shell
python -m pytest tests -q                                  # unit tests: simulated ESL server and simulated Agent
FS_RTP_RUNTIME_TESTS=1 python -m pytest tests/runtime -q   # disposable FreeSWITCH + installed Agent image + local sink
```

The unit tests use a SIMULATED Event Socket server and a SIMULATED Agent (a UDP socket). The runtime tests use a real
FreeSWITCH container, the `datadog/agent` image (7.80.3 here) with its intake pointed at a local sink, and need Docker
(the runtime helpers live in `../integration/tests/runtime`). Nothing is sent outside the machine.

## Backend verification (not done, follow-up)

Local tests cannot show what the Datadog backend does with distributions. In a **non-production** organization, with a key
you are authorized to use (never a production key):

1. Run two Agents with different hostnames (two containers, or two hosts) against the same non-production organization.
2. On each, send a known distribution under the throw-away name:
   `python tools/known_distribution.py --port <statsd port> --seed 1 --count 2000 --tag host_role:a` (and `--seed 2 ...
   --tag host_role:b` on the other).
3. Print the expected values of the union: `python tools/known_distribution.py --expected --seeds 1,2 --count 2000`.
4. In Metrics Explorer or the metrics query API, after enabling percentile aggregations for the metric, query
   `p50:`, `p95:`, `p99:` of `experiment.rtp_backend_check.mos{*}` over the window and with `{host_role:a}`,
   `{host_role:b}`; compare with the expected union values (a sketch is approximate: record the relative error).
5. Confirm count, and that the merged result differs from averaging per-host percentiles.
6. Record the cost implication shown in the organization's usage page for the tag combinations used.

Until that is done, backend percentiles, merging across hosts and billing are **unverified**.
