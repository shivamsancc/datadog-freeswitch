# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Per-metric tests for the session, registration and bridged-call metrics, and their failure isolation.

Fixtures marked REAL are output captured from a disposable FreeSWITCH 1.10.12 test environment
(``tests/runtime``) with SIP calls over its own loopback; hostnames were replaced. They hold only synthetic
test extensions and random UUIDs. Everything else is SYNTHETIC and says so.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

import pytest

from datadog_checks.base.stubs.aggregator import AggregatorStub
from datadog_checks.dev.utils import get_metadata_metrics
from datadog_checks.freeswitch import FreeswitchCheck, esl

from .conftest import SENTINEL_PASSWORD
from .fake_esl import FakeEslServer, api_response, command_reply, make_handler

MakeServer = Callable[..., FakeEslServer]
MakeCheck = Callable[..., FreeswitchCheck]
Run = Callable[..., None]

# REAL: three concurrent loopback SIP calls (6 sessions) one second after they were established.
STATUS_LOADED = (
    b'UP 0 years, 0 days, 0 hours, 0 minutes, 15 seconds, 995 milliseconds, 950 microseconds\n'
    b'FreeSWITCH (Version 1.10.12 -release 64bit) is ready\n'
    b'6 session(s) since startup\n'
    b'6 session(s) - peak 6, last 5min 6 \n'
    b'5 session(s) per Sec out of max 10, peak 5, last 5min 5 \n'
    b'50 session(s) max\n'
    b'min idle cpu 0.00/99.57\n'
    b'Current Stack Size/Max 240K/8192K\n'
)
# REAL: ``show bridged_calls count`` with one bridged call. FreeSWITCH returns the listing, then the total.
BRIDGED_HEADER = (
    b'uuid,direction,created,created_epoch,name,state,cid_name,cid_num,ip_addr,dest,presence_id,presence_data,'
    b'accountcode,callstate,callee_name,callee_num,callee_direction,call_uuid,hostname,sent_callee_name,'
    b'sent_callee_num,b_uuid,b_direction,b_created,b_created_epoch,b_name,b_state,b_cid_name,b_cid_num,b_ip_addr,'
    b'b_dest,b_presence_id,b_presence_data,b_accountcode,b_callstate,b_callee_name,b_callee_num,b_callee_direction,'
    b'b_sent_callee_name,b_sent_callee_num,call_created_epoch\n'
)
BRIDGED_ROW = (
    b'd93e2ba1-bb8f-4f60-90ad-95c50be0e34d,outbound,2026-10-12 16:06:10,1791821170,sofia/internal/5001,'
    b'CS_EXECUTE,5001,5001,,5001,,,,ACTIVE,5000,5000,SEND,d93e2ba1-bb8f-4f60-90ad-95c50be0e34d,testhost,5000,5000,'
    b'db60ce07-4ea5-4e6e-bdd0-8fe389ea3571,outbound,2026-10-12 16:06:10,1791821170,sofia/internal/5000,'
    b'CS_EXCHANGE_MEDIA,5001,5001,127.0.0.1,5000,,,,ACTIVE,5000,5000,SEND,5001,5001,1791821170\n'
)
BRIDGED_ONE = BRIDGED_HEADER + BRIDGED_ROW + b'\n1 total.\n'


def tags_for(server: FakeEslServer) -> list[str]:
    return ['freeswitch_host:127.0.0.1', 'freeswitch_port:{}'.format(server.port), 'tier:test']


# ---------------------------------------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------------------------------------


def test_parse_status_real_loaded_capture() -> None:
    status = esl.parse_status(STATUS_LOADED)
    assert status == esl.StatusFields(created=6, limit=50, active=6, peak=6, peak_5min=6, rate_limit=10, rate_peak=5)


def test_parse_status_idle_after_calls_real_capture() -> None:
    # REAL (fake_esl.STATUS_1_10_12): zero current sessions, a peak of 4, nothing in the last five minutes.
    from .fake_esl import STATUS_1_10_12

    status = esl.parse_status(STATUS_1_10_12)
    assert (status.active, status.peak, status.peak_5min) == (0, 4, 0)
    assert (status.rate_limit, status.rate_peak) == (30, 4)


def test_parse_status_older_output_without_the_peak_and_rate_lines() -> None:
    # SYNTHETIC: a version that prints only the two lines the first release relied on.
    status = esl.parse_status(b'4 session(s) since startup\n200 session(s) max\n')
    assert status == esl.StatusFields(4, 200, None, None, None, None, None)


@pytest.mark.parametrize(
    'extra',
    [
        b'many session(s) - peak 4, last 5min 0\n',
        b'0 session(s) - peak four, last 5min 0\n',
        b'0 session(s) - peak 4\n',
        b'0 session(s) - peak 4, last 5min 0\n0 session(s) - peak 4, last 5min 0\n',  # repeated
        b'0 session(s) - peak 4, last 5min 0\n1 session(s) - peak 5, last 5min 1\n',  # conflicting
        b'-1 session(s) - peak 4, last 5min 0\n',
        b'0 session(s) per Sec out of max x, peak 4, last 5min 4\n',
        b'0 session(s) per Sec out of max 30, peak 4\n',
        b'0 session(s) per Sec out of max 30, peak 4, last 5min 4\n'
        b'0 session(s) per Sec out of max 30, peak 4, last 5min 4\n',
        b'0 session(s) - peak ' + b'9' * 40 + b', last 5min 0\n',
    ],
)
def test_parse_status_rejects_malformed_optional_lines(extra: bytes) -> None:
    # SYNTHETIC. A present-but-invalid line fails the whole status query; nothing partial is emitted.
    with pytest.raises(esl.EslParseError):
        esl.parse_status(b'4 session(s) since startup\n' + extra + b'200 session(s) max\n')


@pytest.mark.parametrize(
    'body, expected',
    [(b'\n0 total.\n', 0), (b'\n1 total.\n', 1), (b'\n250 total.\n', 250), (b'7 total.', 7)],
)
def test_parse_registrations(body: bytes, expected: int) -> None:
    # The first two are REAL (``show registrations count``, 0 and 1 registrations).
    assert esl.parse_registrations(body) == expected


@pytest.mark.parametrize(
    'body',
    [
        b'',
        b'\n',
        b'total.',
        b'-1 total.',
        b'1.5 total.',
        b'x total.',
        b'1 total',
        b'0 total.\n0 total.',  # two lines
        b'reg_user,realm\n1000,test.local\n\n1 total.\n',  # a listing is not the count-only reply
        b'-ERR show registrations count Command not found!\n',
        b'\xff total.',
        b'\xd9\xa1 total.',
        b'9' * 40 + b' total.',
    ],
)
def test_parse_registrations_rejects_malformed(body: bytes) -> None:
    with pytest.raises(esl.EslParseError):
        esl.parse_registrations(body)


def test_parse_bridged_calls() -> None:
    assert esl.parse_bridged_calls(b'\n0 total.\n') == 0  # REAL
    assert esl.parse_bridged_calls(BRIDGED_ONE) == 1  # REAL
    many = BRIDGED_HEADER + BRIDGED_ROW * 300 + b'\n300 total.\n'  # SYNTHETIC repetition of the real row
    assert esl.parse_bridged_calls(many) == 300


@pytest.mark.parametrize(
    'body',
    [
        b'',
        b'\n\n',
        b'garbage\n',
        b'1 total.\n' + BRIDGED_ROW,  # the total is not the last line
        b'3 total.\nthis is not a listing\n',
        b'not,a,listing\n1 total.\n',  # non-empty count but no uuid header
        b'-1 total.\n',
        b'-ERR show bridged_calls Command not found!\n',
        BRIDGED_HEADER + BRIDGED_ROW,  # truncated: no total line
    ],
)
def test_parse_bridged_calls_rejects_malformed(body: bytes) -> None:
    with pytest.raises(esl.EslParseError):
        esl.parse_bridged_calls(body)


def test_bridged_parser_ignores_a_fake_total_inside_a_row() -> None:
    # SYNTHETIC: caller-controlled text in a row must not influence the value; only the last line counts.
    row = BRIDGED_ROW.replace(b',5001,5001,', b',99 total.,5001,', 1)
    assert esl.parse_bridged_calls(BRIDGED_HEADER + row + b'\n1 total.\n') == 1


# ---------------------------------------------------------------------------------------------------------
# Emission through the check
# ---------------------------------------------------------------------------------------------------------


def test_loaded_state_emits_every_metric_with_documented_type_and_tags(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    server.responses.update(
        {
            'show channels count': b'\n6 total.\n',
            'show status': STATUS_LOADED,
            'show registrations count': b'\n1 total.\n',
            'show bridged_calls count': BRIDGED_ONE,
        }
    )
    dd_run_check(make_check(server, collect_bridged_calls=True))
    tags = tags_for(server)
    expected = {
        'freeswitch.channels.active': (6, aggregator.GAUGE),
        'freeswitch.sessions.active': (6, aggregator.GAUGE),
        'freeswitch.sessions.peak': (6, aggregator.GAUGE),
        'freeswitch.sessions.created': (6, aggregator.MONOTONIC_COUNT),
        'freeswitch.sessions.limit': (50, aggregator.GAUGE),
        'freeswitch.sessions.rate.limit': (10, aggregator.GAUGE),
        'freeswitch.registrations': (1, aggregator.GAUGE),
        'freeswitch.calls.bridged': (1, aggregator.GAUGE),
    }
    for name, (value, kind) in expected.items():
        aggregator.assert_metric(name, value, tags=tags, count=1, metric_type=kind)
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())


def test_zero_values_are_emitted_not_omitted(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    server.responses.update(
        {
            'show channels count': b'\n0 total.\n',
            'show status': (
                b'0 session(s) since startup\n0 session(s) - peak 0, last 5min 0 \n'
                b'0 session(s) per Sec out of max 30, peak 0, last 5min 0\n200 session(s) max\n'
            ),
            'show registrations count': b'\n0 total.\n',
            'show bridged_calls count': b'\n0 total.\n',
        }
    )
    dd_run_check(make_check(server, collect_bridged_calls=True))
    for name in (
        'freeswitch.sessions.active',
        'freeswitch.sessions.peak',
        'freeswitch.sessions.created',
        'freeswitch.registrations',
        'freeswitch.calls.bridged',
    ):
        aggregator.assert_metric(name, 0, count=1)


def test_peak_is_not_derived_from_current_and_follows_the_reported_value(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server, collect_bridged_calls=True)
    for current, peak in ((2, 2), (0, 2), (1, 2), (0, 0)):  # (0, 0): FreeSWITCH restarted
        server.responses['show status'] = (
            '9 session(s) since startup\n{} session(s) - peak {}, last 5min {}\n50 session(s) max\n'.format(
                current, peak, peak
            ).encode()
        )
        dd_run_check(check)
    assert [m.value for m in aggregator.metrics('freeswitch.sessions.active')] == [2, 0, 1, 0]
    assert [m.value for m in aggregator.metrics('freeswitch.sessions.peak')] == [2, 2, 2, 0]


def test_unsupported_older_version_keeps_created_and_limit_and_omits_the_rest(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    server.responses['show status'] = b'4 session(s) since startup\n200 session(s) max\n'
    dd_run_check(make_check(server, collect_bridged_calls=True))
    aggregator.assert_metric('freeswitch.sessions.created', 4, count=1)
    aggregator.assert_metric('freeswitch.sessions.limit', 200, count=1)
    for name in ('freeswitch.sessions.active', 'freeswitch.sessions.peak', 'freeswitch.sessions.rate.limit'):
        aggregator.assert_metric(name, count=0)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=1)  # absence of an optional line is not an error


def test_no_unbounded_tags_whatever_the_server_returns(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    server.responses['show bridged_calls count'] = BRIDGED_ONE
    server.responses['show registrations count'] = b'\n1 total.\n'
    dd_run_check(make_check(server, collect_bridged_calls=True))
    allowed = {'freeswitch_host:127.0.0.1', 'freeswitch_port:{}'.format(server.port), 'tier:test'}
    for stubs in aggregator._metrics.values():
        for stub in stubs:
            assert set(stub.tags) <= allowed | {'error_type:' + t for t in esl.ERROR_TYPES}
    # nothing from the listing (uuids, extensions, host names) reaches any tag or name
    text = ' '.join(stub.name + ' ' + ' '.join(stub.tags) for stubs in aggregator._metrics.values() for stub in stubs)
    for needle in ('d93e2ba1', '5001', 'testhost', 'CS_EXECUTE'):
        assert needle not in text.replace(str(server.port), '')


# ---------------------------------------------------------------------------------------------------------
# Partial failures
# ---------------------------------------------------------------------------------------------------------

ANSWERS = {
    'parse': api_response(b'this is not valid\n'),
    'command_error': api_response(b'-ERR no such command\n'),
    'permission_denied': command_reply('-ERR permission denied'),
}
EXPECTED_ERROR = {'parse': 'parse', 'command_error': 'protocol', 'permission_denied': 'auth'}
OPTIONAL = {
    'show status': ('freeswitch.sessions.created', 'freeswitch.sessions.limit', 'freeswitch.sessions.peak'),
    'show registrations count': ('freeswitch.registrations',),
    'show bridged_calls count': ('freeswitch.calls.bridged',),
}


@pytest.mark.parametrize('answer', list(ANSWERS))
@pytest.mark.parametrize('failing', list(OPTIONAL))
def test_one_failing_optional_query_keeps_every_other_metric(
    failing: str,
    answer: str,
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Run,
    aggregator: AggregatorStub,
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        conn.send(ANSWERS[answer] if cmd == failing else api_response(srv.responses[cmd]))
        return True

    server = make_server(make_handler(on_api=on_api))
    dd_run_check(make_check(server, collect_bridged_calls=True))
    tags = tags_for(server)
    aggregator.assert_metric('freeswitch.can_connect', 1, tags=tags, count=1)
    aggregator.assert_metric('freeswitch.uptime', count=1)
    aggregator.assert_metric('freeswitch.channels.active', count=1)
    for query, names in OPTIONAL.items():
        for name in names:
            aggregator.assert_metric(name, count=0 if query == failing else 1)
    aggregator.assert_metric(
        'freeswitch.collection.errors',
        1,
        tags=tags + ['error_type:' + EXPECTED_ERROR[answer]],
        count=1,
        metric_type=aggregator.COUNT,
    )
    assert [m.value for m in aggregator.metrics('freeswitch.collection.errors')] == [1]  # no separate zero sample
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())
    # all later queries ran on the same connection: one connection, all five commands
    assert server.connections == 1
    assert len(server.api_lines) == 5


def test_all_optional_queries_denied_keeps_required_metrics_and_reports_one_error_type(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        if cmd in OPTIONAL:
            conn.send(command_reply('-ERR permission denied'))
        else:
            conn.send(api_response(srv.responses[cmd]))
        return True

    server = make_server(make_handler(on_api=on_api))
    dd_run_check(make_check(server, collect_bridged_calls=True))
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)
    aggregator.assert_metric('freeswitch.uptime', count=1)
    aggregator.assert_metric('freeswitch.channels.active', count=1)
    errors = aggregator.metrics('freeswitch.collection.errors')
    assert [(m.value, tuple(sorted(m.tags))) for m in errors] == [
        (1.0, tuple(sorted(tags_for(server) + ['error_type:auth'])))
    ]
    for names in OPTIONAL.values():
        for name in names:
            aggregator.assert_metric(name, count=0)


def test_two_failures_with_different_types_report_both(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        if cmd == 'show registrations count':
            conn.send(command_reply('-ERR permission denied'))
        elif cmd == 'show bridged_calls count':
            conn.send(api_response(b'nonsense'))
        else:
            conn.send(api_response(srv.responses[cmd]))
        return True

    server = make_server(make_handler(on_api=on_api))
    dd_run_check(make_check(server, collect_bridged_calls=True))
    got = sorted(
        tag for m in aggregator.metrics('freeswitch.collection.errors') for tag in m.tags if 'error_type' in tag
    )
    assert got == ['error_type:auth', 'error_type:parse']
    aggregator.assert_metric('freeswitch.sessions.created', count=1)


def test_transport_failure_in_an_optional_query_ends_the_optional_queries_but_keeps_earlier_metrics(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    def on_api(conn: Any, srv: FakeEslServer, cmd: str) -> bool:
        if cmd == 'show registrations count':
            return False  # close without answering
        conn.send(api_response(srv.responses[cmd]))
        return True

    server = make_server(make_handler(on_api=on_api))
    check = make_check(server, collect_bridged_calls=True)
    dd_run_check(check)
    aggregator.assert_metric('freeswitch.can_connect', 1, count=1)
    aggregator.assert_metric('freeswitch.sessions.created', count=1)  # queried before the failure
    aggregator.assert_metric('freeswitch.registrations', count=0)
    aggregator.assert_metric('freeswitch.calls.bridged', count=0)  # not attempted on a dead connection
    aggregator.assert_metric('freeswitch.collection.errors', 1, count=1)
    assert server.api_lines.count('api show bridged_calls count') == 0


def test_bridged_listing_larger_than_the_normal_limit_is_accepted_but_not_unbounded(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    rows = BRIDGED_ROW * 200  # about 100 KiB: above the 64 KiB limit that applies to the other commands
    assert len(rows) > esl.MAX_BODY_BYTES
    server.responses['show bridged_calls count'] = BRIDGED_HEADER + rows + b'\n200 total.\n'
    dd_run_check(make_check(server, collect_bridged_calls=True))
    aggregator.assert_metric('freeswitch.calls.bridged', 200, count=1)

    # above the listing limit it is a bounded protocol error, and the other metrics survive
    server.responses['show bridged_calls count'] = b'x' * (esl.MAX_LISTING_BYTES + 1)
    aggregator.reset()
    dd_run_check(make_check(server, collect_bridged_calls=True))
    aggregator.assert_metric('freeswitch.calls.bridged', count=0)
    aggregator.assert_metric('freeswitch.uptime', count=1)
    aggregator.assert_metric('freeswitch.collection.errors', 1, count=1)


def test_other_commands_keep_the_small_body_limit() -> None:
    assert esl.MAX_BODY_BYTES == 64 * 1024
    assert esl.MAX_LISTING_BYTES > esl.MAX_BODY_BYTES


def test_optional_failures_never_leak_server_text_or_secrets(
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Run,
    aggregator: AggregatorStub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    server = make_server()
    server.responses['show registrations count'] = ('LEAK ' + SENTINEL_PASSWORD + ' 1000@test.local\n').encode()
    server.responses['show bridged_calls count'] = b'uuid,x\nLEAK-ROW-5001\n\nnot a total\n'
    dd_run_check(make_check(server, collect_bridged_calls=True))
    assert SENTINEL_PASSWORD not in caplog.text
    assert 'LEAK' not in caplog.text
    assert '5001' not in caplog.text
    assert 'registrations query failed' in caplog.text


def test_recovery_after_optional_failure_emits_again(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    check = make_check(server, collect_bridged_calls=True)
    dd_run_check(check)
    server.responses['show registrations count'] = b'junk\n'
    dd_run_check(check)
    server.responses['show registrations count'] = b'\n3 total.\n'
    dd_run_check(check)
    assert [m.value for m in aggregator.metrics('freeswitch.registrations')] == [1, 3]
    assert [m.value for m in aggregator.metrics('freeswitch.collection.errors')] == [0, 1, 0]


# ---------------------------------------------------------------------------------------------------------
# sessions.created across normal growth, repeated samples and FreeSWITCH restarts
# ---------------------------------------------------------------------------------------------------------


def _status_body(created: int) -> bytes:
    return (
        '{} session(s) since startup\n0 session(s) max\n'.format(created)
        .replace('0 session(s) max', '50 session(s) max')
        .encode()
    )


def _samples(
    steps: Sequence[tuple[int, int | None]],
    make_server: MakeServer,
    make_check: MakeCheck,
    dd_run_check: Run,
    aggregator: AggregatorStub,
) -> list[float]:
    """Run the check once per ``(uptime, created)`` step (created None: the status query fails) and return the
    ``sessions.created`` values that were submitted, in order."""
    server = make_server()
    check = make_check(server, collect_bridged_calls=True)
    for uptime, created in steps:
        server.responses['uptime s'] = '{}\n'.format(uptime).encode()
        server.responses['show status'] = b'garbage\n' if created is None else _status_body(created)
        dd_run_check(check)
    assert all(m.type == aggregator.MONOTONIC_COUNT for m in aggregator.metrics('freeswitch.sessions.created'))
    return [m.value for m in aggregator.metrics('freeswitch.sessions.created')]


def agent_increase(samples: list[float]) -> float:
    """Total increase a Datadog Agent reports for these monotonic_count samples.

    MODEL of behavior observed with a real Agent 7.80.3 (docs/evidence/METRIC_VERIFICATION_REPORT.md section 3): the
    increase between consecutive samples is reported; a sample lower than the previous one is ignored but becomes the
    new baseline. This is not the Agent.
    """
    total = 0.0
    for previous, current in zip(samples, samples[1:]):
        if current >= previous:
            total += current - previous
    return total


def test_created_startup_submits_the_raw_value_without_a_reset_sample(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    assert _samples([(100, 40)], make_server, make_check, dd_run_check, aggregator) == [40]


def test_created_repeated_and_growing_samples_are_submitted_unchanged(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    steps = [(100, 4), (115, 4), (130, 4), (145, 10), (160, 11)]
    samples = _samples(steps, make_server, make_check, dd_run_check, aggregator)
    assert samples == [4, 4, 4, 10, 11]  # no injected zero without a restart
    assert agent_increase(samples) == 7


def test_created_restart_with_a_lower_counter_injects_one_baseline_reset(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    steps = [(100, 4), (115, 10), (3, 0), (18, 2), (33, 8)]  # restart between the 2nd and 3rd run
    samples = _samples(steps, make_server, make_check, dd_run_check, aggregator)
    assert samples == [4, 10, 0, 0, 2, 8]
    assert agent_increase(samples) == 6 + 2 + 6  # 4 to 10, then 0 to 2 to 8: nothing after the restart is lost


def test_created_restart_where_the_new_process_already_passed_the_old_count(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    # Observed with a real Agent: without the reset only 12 - 8 = 4 of the 12 new sessions were reported.
    steps = [(100, 8), (115, 8), (5, 12), (20, 13)]
    samples = _samples(steps, make_server, make_check, dd_run_check, aggregator)
    assert samples == [8, 8, 0, 12, 13]
    assert agent_increase(samples) == 13  # all 12 sessions of the new process plus the one that followed


def test_created_restart_seen_while_the_status_query_fails_is_applied_to_the_next_value(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    steps = [(100, 8), (5, None), (20, 9)]  # uptime shows the restart; the counter is unavailable that run
    samples = _samples(steps, make_server, make_check, dd_run_check, aggregator)
    assert samples == [8, 0, 9]
    assert agent_increase(samples) == 9


def test_created_two_restarts_and_a_failed_run_in_between(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    steps = [(10, 1), (25, 5), (5, 2), (20, None), (35, 6), (8, 0), (23, 3)]
    samples = _samples(steps, make_server, make_check, dd_run_check, aggregator)
    assert samples == [1, 5, 0, 2, 6, 0, 0, 3]
    assert agent_increase(samples) == 4 + 2 + 4 + 3


def test_created_state_is_per_check_instance(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    first, second = make_server(), make_server()
    a, b = make_check(first), make_check(second)
    first.responses['uptime s'], first.responses['show status'] = b'100\n', _status_body(9)
    second.responses['uptime s'], second.responses['show status'] = b'100\n', _status_body(9)
    dd_run_check(a)
    dd_run_check(b)
    first.responses['uptime s'] = b'3\n'  # only the first server restarts
    dd_run_check(a)
    dd_run_check(b)
    by_port = {
        port: [
            m.value for m in aggregator.metrics('freeswitch.sessions.created') if 'freeswitch_port:%d' % port in m.tags
        ]
        for port in (first.port, second.port)
    }
    assert by_port[first.port] == [9, 0, 9]
    assert by_port[second.port] == [9, 9]


# ---------------------------------------------------------------------------------------------------------
# calls.bridged is opt-in
# ---------------------------------------------------------------------------------------------------------


def test_bridged_calls_is_disabled_by_default_and_the_listing_is_never_requested(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    server = make_server()
    server.responses['show bridged_calls count'] = BRIDGED_ONE
    for _ in range(3):
        dd_run_check(make_check(server))
    assert 'api show bridged_calls count' not in server.api_lines
    assert 'show bridged_calls count' not in ' '.join(server.lines)
    aggregator.assert_metric('freeswitch.calls.bridged', count=0)
    aggregator.assert_metric('freeswitch.collection.errors', 0, count=3)


def test_bridged_calls_can_be_enabled_per_instance(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    on, off = make_server(), make_server()
    for server in (on, off):
        server.responses['show bridged_calls count'] = BRIDGED_ONE
    dd_run_check(make_check(on, collect_bridged_calls=True))
    dd_run_check(make_check(off, collect_bridged_calls=False))
    aggregator.assert_metric('freeswitch.calls.bridged', 1, tags=tags_for(on), count=1)
    assert 'api show bridged_calls count' in on.api_lines
    assert 'api show bridged_calls count' not in off.api_lines
    aggregator.assert_metric('freeswitch.calls.bridged', count=1)  # only the enabled instance


def test_bridged_calls_option_must_be_a_boolean(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run
) -> None:
    server = make_server()
    check = make_check(server, collect_bridged_calls='sometimes')
    with pytest.raises(Exception):  # noqa: B017 - configuration validation error type is internal to the base package
        dd_run_check(check)


def test_run_budget_counts_the_bridged_command_only_when_enabled(
    make_server: MakeServer, make_check: MakeCheck
) -> None:
    for enabled, expected in ((False, 1.7), (True, 2.0)):
        check = make_check(
            make_server(),
            connect_timeout=0.5,
            read_timeout=0.3,
            min_collection_interval=15,
            collect_bridged_calls=enabled,
        )
        check.load_configuration_models()
        check._configure()
        assert check._run_budget == pytest.approx(expected)


def test_created_restart_during_a_failed_required_query_is_detected_on_recovery(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    """Audit: runs that fail before any metric is emitted must not lose the restart evidence."""
    server = make_server()
    check = make_check(server)
    server.responses['uptime s'], server.responses['show status'] = b'100\n', _status_body(8)
    dd_run_check(check)
    server.responses['uptime s'] = b'garbage\n'  # the required query fails: nothing is emitted, state is kept
    dd_run_check(check)
    server.responses['uptime s'], server.responses['show status'] = b'12\n', _status_body(11)  # restarted meanwhile
    dd_run_check(check)
    samples = [m.value for m in aggregator.metrics('freeswitch.sessions.created')]
    assert samples == [8, 0, 11]


def test_created_restart_that_looks_like_normal_growth_is_not_detected(
    make_server: MakeServer, make_check: MakeCheck, dd_run_check: Run, aggregator: AggregatorStub
) -> None:
    """Documented limitation: uptime and the counter both went up (the old process lived under one interval)."""
    steps = [(5, 2), (20, 9)]  # restarted in between, but both values are higher than before
    samples = _samples(steps, make_server, make_check, dd_run_check, aggregator)
    assert samples == [2, 9]
