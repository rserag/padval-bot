import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from padval_bot.jellyfin import JellyfinError, JellyfinService


class JellyfinServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key_file = Path(self.directory.name) / "key"
        self.key_file.write_text("a" * 32 + "\n", encoding="ascii")
        self.service = JellyfinService(
            {
                "base_url": "http://192.0.2.20:8096",
                "api_key_file": str(self.key_file),
                "timeout_seconds": 7,
            }
        )

    @patch("padval_bot.jellyfin.urllib.request.urlopen")
    def test_refresh_uses_header_and_fixed_endpoint(self, urlopen):
        response = MagicMock()
        response.read.return_value = b""
        urlopen.return_value.__enter__.return_value = response

        self.service.refresh_library()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.0.2.20:8096/Library/Refresh")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("X-emby-token"), "a" * 32)
        self.assertNotIn("api_key", request.full_url)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)

    @patch("padval_bot.jellyfin.urllib.request.urlopen")
    def test_preflight_requires_valid_system_info(self, urlopen):
        response = MagicMock()
        response.read.side_effect = [
            b'{"Version":"10.11.11"}',
            b'[{"Key":"RefreshLibrary","State":"Idle"}]',
        ]
        urlopen.return_value.__enter__.return_value = response

        self.service.preflight()

        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(requests[0].full_url, "http://192.0.2.20:8096/System/Info")
        self.assertEqual(requests[1].full_url, "http://192.0.2.20:8096/ScheduledTasks")
        self.assertTrue(all(request.method == "GET" for request in requests))

    @patch("padval_bot.jellyfin.urllib.request.urlopen")
    def test_scan_status_reads_progress_and_last_result(self, urlopen):
        response = MagicMock()
        response.read.return_value = b"""[
          {
            "Name":"Scan Media Library",
            "State":"Running",
            "CurrentProgressPercentage":37.25,
            "LastExecutionResult":{
              "Status":"Completed",
              "EndTimeUtc":"2026-09-02T11:32:11.2130536Z"
            }
          }
        ]"""
        urlopen.return_value.__enter__.return_value = response

        status = self.service.scan_status()

        self.assertTrue(status.active)
        self.assertEqual(status.progress, 37.25)
        self.assertEqual(status.last_execution_status, "Completed")
        self.assertEqual(status.last_execution_end, "2026-09-02T11:32:11.2130536Z")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.0.2.20:8096/ScheduledTasks")
        self.assertEqual(request.method, "GET")

    @patch("padval_bot.jellyfin.urllib.request.urlopen")
    def test_scan_status_rejects_invalid_progress(self, urlopen):
        response = MagicMock()
        response.read.return_value = (
            b'[{"Name":"Scan Media Library","State":"Running",'
            b'"CurrentProgressPercentage":101}]'
        )
        urlopen.return_value.__enter__.return_value = response

        with self.assertRaises(JellyfinError):
            self.service.scan_status()

    def test_rejects_invalid_key(self):
        self.key_file.write_text("not-a-key\n", encoding="ascii")
        with self.assertRaises(JellyfinError):
            JellyfinService(
                {
                    "base_url": "http://192.0.2.20:8096",
                    "api_key_file": str(self.key_file),
                }
            )


if __name__ == "__main__":
    unittest.main()
