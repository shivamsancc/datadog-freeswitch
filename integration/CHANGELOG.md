# CHANGELOG - FreeSWITCH

## Unreleased

***Added***:

* Add the FreeSWITCH check: availability, uptime, active channel count and collection diagnostics over the Event Socket.
* Add `freeswitch.sessions.limit` and `freeswitch.sessions.created` from `show status` (tested on FreeSWITCH 1.10.12). A failure of that query does not suppress the other metrics.
* Add `freeswitch.sessions.active`, `freeswitch.sessions.peak` and `freeswitch.sessions.rate.limit` (from `show status`), `freeswitch.registrations` (`show registrations count`) and `freeswitch.calls.bridged` (`show bridged_calls count`, opt-in with `collect_bridged_calls`, off by default). Each optional query fails independently.

***Changed***:

* Submit `freeswitch.sessions.created` as a monotonic count of the cumulative value instead of a gauge, and submit a `0` baseline sample after a detected FreeSWITCH restart, so that the new process is counted in full.
