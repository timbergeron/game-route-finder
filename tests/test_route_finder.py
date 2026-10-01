import contextlib
import io
import socket
import subprocess
import sys
import tempfile
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

import route_finder as rf


class RouteFinderTests(unittest.TestCase):
    def test_cli_rejects_invalid_options_before_network_io(self):
        for options in (["--count", "0"], ["--count", "-2"], ["--udp", "nq:0"],
                        ["--udp", "nq:70000"], ["--udp", "unknown:26000"]):
            with self.subTest(options=options):
                result = subprocess.run(
                    [sys.executable, str(Path(rf.__file__)), "probe", "localhost"] + options,
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Target", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_udp_ignores_packets_from_another_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            server.settimeout(2)
            def send_unrelated():
                _, client = server.recvfrom(4096)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                    other.sendto(b"not the game server", client)
            worker = threading.Thread(target=send_unrelated)
            worker.start()
            try:
                result = rf.udp_probe("127.0.0.1", "qw:%s" % server.getsockname()[1], count=1, timeout=0.1)
                self.assertIsNone(result)
            finally:
                worker.join(timeout=3)

    def test_udp_accepts_server_reply(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            server.settimeout(2)
            def reply():
                _, client = server.recvfrom(4096)
                server.sendto(b"\xff\xff\xff\xffn\\test", client)
            worker = threading.Thread(target=reply)
            worker.start()
            try:
                result = rf.udp_probe("127.0.0.1", "qw:%s" % server.getsockname()[1], count=1, timeout=0.5)
                self.assertEqual(result["n"], 1)
                self.assertEqual(result["loss"], 0)
            finally:
                worker.join(timeout=3)

    def scan_fixture(self, budget=3, survey=False):
        entries = [dict(provider="Example", name="City %s" % i, host="localhost",
                        country="US", cities=["City %s" % i], asns=[12345]) for i in range(8)]
        args = SimpleNamespace(target="localhost", countries="US", hosts=None, udp=None,
                               probes=3, budget=budget, token=None, survey=survey,
                               cities="Denver,Chicago", json=None)
        return entries, args

    def test_scan_reserves_budget_before_parallel_requests(self):
        entries, args = self.scan_fixture()
        requested = []
        def measure(target, locs, token=None):
            requested.extend(locs)
            time.sleep(0.03)
            return []
        with patch.object(rf, "CATALOG", entries), patch.object(rf, "resolve", return_value="127.0.0.1"), \
             patch.object(rf, "ping", return_value={"avg": 100, "min": 90, "max": 110, "n": 10}), \
             patch.object(rf, "measure_entry", return_value={"avg": 10, "min": 9, "max": 11, "n": 5}), \
             patch.object(rf, "gp_limits", return_value=None), \
             patch.object(rf, "globalping_chunked", side_effect=measure), contextlib.redirect_stdout(io.StringIO()):
            rf.cmd_scan(args)
        self.assertEqual(sum(loc["limit"] for loc in requested), 3)

    def test_scan_does_not_modify_builtin_catalog(self):
        entries, args = self.scan_fixture(budget=0)
        with patch.object(rf, "CATALOG", entries), patch.object(rf, "resolve", return_value="127.0.0.1"), \
             patch.object(rf, "ping", return_value=None), patch.object(rf, "measure_entry", return_value=None), \
             patch.object(rf, "gp_limits", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            rf.cmd_scan(args)
        self.assertTrue(all("leg1" not in e for e in entries))

    def test_survey_shares_scan_budget(self):
        entries, args = self.scan_fixture(budget=0, survey=True)
        requested = []
        def measure(target, locs, token=None):
            requested.extend(locs)
            return []
        with patch.object(rf, "CATALOG", entries), patch.object(rf, "resolve", return_value="127.0.0.1"), \
             patch.object(rf, "ping", return_value=None), patch.object(rf, "measure_entry", return_value=None), \
             patch.object(rf, "gp_limits", return_value=None), \
             patch.object(rf, "globalping_chunked", side_effect=measure), contextlib.redirect_stdout(io.StringIO()):
            rf.cmd_scan(args)
        self.assertEqual(requested, [])

    def test_poll_network_error_is_reported_as_runtime_error(self):
        response = io.StringIO('{"id": "test-measurement"}')
        with patch.object(rf.urllib.request, "urlopen", side_effect=[response, urllib.error.URLError("offline")]), \
             patch.object(rf.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Globalping"):
                rf.globalping({"type": "ping", "target": "localhost"})

    def test_custom_hosts_reject_invalid_records_without_traceback(self):
        invalid = [{"name": "missing fields"},
                   {"provider": "Custom", "name": "Custom", "host": "localhost", "country": "US",
                    "cities": ["Denver"], "asns": [12345], "tcp": 70000}]
        for entry in invalid:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                host_file = Path(tmp) / "hosts.json"
                host_file.write_text(json.dumps([entry]))
                args = self.scan_fixture(budget=0)[1]
                args.hosts = str(host_file)
                with patch.object(rf, "resolve", return_value="127.0.0.1"), \
                     patch.object(rf, "CATALOG", []), patch.object(rf, "ping", return_value=None), \
                     patch.object(rf, "measure_entry", return_value=None), patch.object(rf, "gp_limits", return_value=None), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "host"):
                        rf.cmd_scan(args)

    def test_survey_preserves_country_for_default_cities(self):
        args = self.scan_fixture(budget=5)[1]
        args.cities = None
        requested = []
        def measure(target, locs, token=None):
            requested.extend(locs)
            return []
        with patch.object(rf, "DEFAULT_SURVEY_CITIES", {"US": ["Denver"], "CA": ["Toronto"]}), \
             patch.object(rf, "globalping_chunked", side_effect=measure), contextlib.redirect_stdout(io.StringIO()):
            rf.run_survey("127.0.0.1", {"US", "CA"}, args, budget=7)
        self.assertEqual(requested, [{"country": "CA", "city": "Toronto", "limit": 5},
                                     {"country": "US", "city": "Denver", "limit": 2}])


if __name__ == "__main__":
    unittest.main()
