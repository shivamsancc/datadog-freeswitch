"""Unit tests for the measurement math/parsers in esl-baseline.
Run inside the container (needs no ESL/FreeSWITCH):  lab/tests/run-unit-tests.sh
"""
import importlib.machinery
import importlib.util
import json
import sys
import unittest

sys.path.insert(0, "/usr/local/bin")
_l = importlib.machinery.SourceFileLoader("esl_baseline", "/usr/local/bin/esl-baseline")
_s = importlib.util.spec_from_loader("esl_baseline", _l)
B = importlib.util.module_from_spec(_s)
_l.exec_module(B)


class Percentiles(unittest.TestCase):
    def test_median_and_interpolation(self):
        self.assertEqual(B.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(B.percentile([10], 95), 10)
        self.assertAlmostEqual(B.percentile(list(range(1, 101)), 95), 95.05)
        self.assertIsNone(B.percentile([], 50))

    def test_describe_p95_needs_enough_samples(self):
        self.assertIsNone(B.describe([1.0] * 19)["p95"])
        self.assertIsNotNone(B.describe([1.0] * 20)["p95"])
        d = B.describe([3.0, 1.0, 2.0])
        self.assertEqual((d["n"], d["min"], d["max"], d["median"], d["mean"]), (3, 1.0, 3.0, 2.0, 2.0))
        self.assertEqual(B.describe([])["n"], 0)


class CpuMath(unittest.TestCase):
    def test_container_cpu_from_cumulative_usec(self):
        # 500 000 us of CPU in 2 s of wall time = 25 % of one core
        self.assertAlmostEqual(B.cpu_pct_from_usec(500_000, 2.0), 25.0)
        # one full core for 1 s
        self.assertAlmostEqual(B.cpu_pct_from_usec(1_000_000, 1.0), 100.0)
        # more than one core is representable
        self.assertAlmostEqual(B.cpu_pct_from_usec(3_000_000, 1.0), 300.0)

    def test_process_cpu_from_ticks(self):
        self.assertAlmostEqual(B.cpu_pct_from_ticks(50, 100, 5.0), 10.0)

    def test_rejects_bad_intervals_and_counter_resets(self):
        self.assertIsNone(B.cpu_pct_from_usec(10, 0))
        self.assertIsNone(B.cpu_pct_from_usec(-5, 1.0))
        self.assertIsNone(B.cpu_pct_from_ticks(1, 0, 1.0))

    def test_cumulative_counter_is_not_a_percentage(self):
        # a cumulative 8.8 s counter value must only ever be used as a delta
        self.assertAlmostEqual(B.cpu_pct_from_usec(8_831_218 - 8_831_218, 30.0), 0.0)


class Memory(unittest.TestCase):
    def test_working_set(self):
        self.assertEqual(B.working_set_bytes(100, 30), 70)
        self.assertEqual(B.working_set_bytes(100, None), 100)
        self.assertEqual(B.working_set_bytes(10, 50), 0)
        self.assertIsNone(B.working_set_bytes(None, 5))


class Parsers(unittest.TestCase):
    STATUS = ("UP 0 years, 0 days, 0 hours, 6 minutes, 47 seconds, 150 milliseconds, 808 microseconds\n"
              "FreeSWITCH (Version 1.10.12 -release 64bit) is ready\n"
              "82 session(s) since startup\n"
              "4 session(s) - peak 40, last 5min 0 \n"
              "0 session(s) per Sec out of max 30, peak 4, last 5min 4\n"
              "200 session(s) max\n")

    def test_status(self):
        s = B.parse_status(self.STATUS)
        self.assertEqual((s["sessions_current"], s["sessions_peak"], s["sessions_since_startup"],
                          s["sessions_max"], s["uptime_s"]), (4, 40, 82, 200, 407))

    def test_uptime_with_days(self):
        t = "UP 0 years, 2 days, 3 hours, 4 minutes, 5 seconds, 1 milliseconds, 2 microseconds\n"
        self.assertEqual(B.parse_status(t)["uptime_s"], (2 * 24 + 3) * 3600 + 4 * 60 + 5)

    def test_values(self):
        self.assertEqual(B.parse_value("uptime_s", "407\n"), 407)
        self.assertEqual(B.parse_value("show_channels_count", "\n40 total.\n"), 40)
        self.assertEqual(B.parse_value("show_calls_count", "\n0 total.\n"), 0)
        self.assertEqual(B.parse_value("status", self.STATUS), 4)
        self.assertIsNone(B.parse_value("uptime_s", "-ERR nope"))
        self.assertIsNone(B.parse_value("show_calls_count", "garbage"))


class Aggregation(unittest.TestCase):
    def test_agg(self):
        g = B.agg([1.0, 2.0, 3.0, None])
        self.assertEqual((g["n"], g["mean"], g["min"], g["max"]), (3, 2.0, 1.0, 3.0))
        self.assertAlmostEqual(g["stdev"], 1.0)
        self.assertIsNone(B.agg([None]))
        self.assertIsNone(B.agg([5.0])["stdev"])

    def test_report_renders_without_data(self):
        out = B.render_report({"env": {}, "scenarios": {}, "anomalies": [], "_runs": []}, {})
        self.assertIn("# FreeSWITCH resource and ESL baseline", out)

    def test_summary_flags_anomalies(self):
        phase = {"kind": "resources", "scenario": "one", "rep": 1, "calls": 1, "esl_snapshots": [],
                 "validation": {"counts_as_expected_in_all_snapshots": False},
                 "cpu_pct_of_one_core": {"container_window_avg": 1.0, "container_peak_interval": 2.0,
                                         "freeswitch_window_avg": 1.0, "freeswitch_peak_interval": 2.0},
                 "memory_mib": {k: {"mean": 1.0, "peak": 1.0} for k in
                                ("container_working_set", "container_usage_memory_current", "freeswitch_process_rss")},
                 "freeswitch_process": {"threads": {"peak": 10}, "open_fds": {"peak": 20}}}
        s = B.build_summary([{"kind": "env"}, {"phase": phase, "load": None, "settle": {"returned_to_baseline": False}}])
        self.assertEqual(len(s["anomalies"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
