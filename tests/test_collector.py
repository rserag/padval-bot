import datetime as dt
import unittest

from padval_bot.collector import format_duration, render_report


class CollectorTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(90), "1m")
        self.assertEqual(format_duration(3720), "1h 2m")
        self.assertEqual(format_duration(90000), "1d 1h")

    def test_render_healthy_report(self):
        snapshot = {
            "generated_at": dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc),
            "network": [{"name": "gateway", "healthy": True}],
            "routeros": None,
            "hosts": [{
                "name": "vm",
                "address": "192.0.2.10",
                "reachable": True,
                "uptime": 90000,
                "load": 0.1,
                "memory_pct": 20,
                "filesystems": {"root": 30},
                "services": {"nginx": True},
                "containers": ["web|running|Up 1 hour (healthy)"],
                "compose_projects": [],
                "process_memory": {},
                "notes": [],
            }],
            "http": [{"name": "web", "status": 200, "healthy": True}],
        }
        report = render_report(snapshot, "TEST")
        self.assertIn("All monitored systems healthy", report)
        self.assertNotIn("RAID", report)
        self.assertLessEqual(len(report), 4000)

    def test_render_reports_failures(self):
        snapshot = {
            "generated_at": "now",
            "network": [{"name": "router", "healthy": False}],
            "routeros": {"healthy": False},
            "hosts": [{"name": "storage", "address": "192.0.2.20", "reachable": False}],
            "http": [{"name": "site", "status": 502, "healthy": False}],
        }
        report = render_report(snapshot)
        self.assertIn("4 issues detected", report)
        self.assertIn("storage unreachable", report)
        self.assertIn("site returned 502", report)

    def test_render_escapes_dynamic_html(self):
        snapshot = {
            "generated_at": "now",
            "network": [{"name": "router <unsafe>", "healthy": True}],
            "routeros": None,
            "hosts": [{"name": "vm & host", "address": "192.0.2.10", "reachable": False}],
            "http": [],
        }
        report = render_report(snapshot, "STATUS <TEST>")
        self.assertIn("STATUS &lt;TEST&gt;", report)
        self.assertIn("router &lt;unsafe&gt;", report)
        self.assertIn("VM &amp; HOST", report)
        self.assertNotIn("<unsafe>", report)
