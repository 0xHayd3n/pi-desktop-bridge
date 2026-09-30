"""Exercise lease-free health diagnostics and their readiness exit status."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from pi_desktop_bridge import cli
from pi_desktop_bridge.transport import TransportError


def healthy():
    return {
        "protocol_version": cli.PROTOCOL_VERSION, "agent_version": cli.__version__,
        "agent_sha256": hashlib.sha256(Path(cli.__file__).with_name("pi_agent.py").read_bytes()).hexdigest(),
        "desktop_ready": True, "session_active": False,
        "checks": [{"name": "wayland", "ok": True, "message": "Desktop socket found"}],
    }


class DoctorTests(unittest.TestCase):
    def run_doctor(self, health):
        transport = mock.MagicMock()
        transport.__enter__.return_value = transport
        transport.request.return_value = health
        with mock.patch.object(cli, "SSHTransport", return_value=transport):
            result = cli.doctor("pi-desktop")
        transport.request.assert_called_once_with("health")
        transport.__exit__.assert_called_once()
        return result

    def test_matching_agent_is_ready_without_status_capture_or_input(self):
        result = self.run_doctor(healthy())
        self.assertTrue(result["ready"])
        self.assertTrue(result["agent_matches_client"])
        self.assertEqual(result["next_steps"], [])

    def test_stale_agent_is_not_ready_and_explains_deploy_reconnect(self):
        data = healthy()
        data["agent_sha256"] = "0" * 64
        result = self.run_doctor(data)
        self.assertFalse(result["ready"])
        self.assertFalse(result["agent_matches_client"])
        self.assertIn("deploy --host pi-desktop", result["next_steps"][0])

    def test_missing_prerequisite_is_reported_without_trying_to_install(self):
        data = healthy()
        data["desktop_ready"] = False
        data["checks"].append({"name": "wtype", "ok": False, "message": "Install wtype"})
        result = self.run_doctor(data)
        self.assertFalse(result["ready"])
        self.assertIn("wtype", [check["name"] for check in result["checks"] if not check["ok"]])

    def test_malformed_diagnostics_are_not_passed_to_output(self):
        data = healthy()
        data["checks"] = [{"name": "bad", "ok": "yes", "message": "private arbitrary output"}]
        result = self.run_doctor(data)
        self.assertFalse(result["ready"])
        self.assertNotIn("private arbitrary output", json.dumps(result))
        for malformed in (None, [], True):
            with self.subTest(malformed=malformed):
                self.assertFalse(self.run_doctor(malformed)["ready"])

    def test_ssh_failure_has_safe_next_steps(self):
        with mock.patch.object(cli, "SSHTransport", side_effect=TransportError("Agent protocol mismatch")):
            result = cli.doctor("pi-desktop")
        self.assertFalse(result["ready"])
        self.assertIn("protocol mismatch", result["checks"][0]["message"])
        self.assertIn("trusted host key", result["next_steps"][0])

    def test_doctor_exit_status_matches_readiness_and_stdout_is_json(self):
        for ready in (True, False):
            with self.subTest(ready=ready), mock.patch.object(cli, "doctor", return_value={"ready": ready}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["doctor", "--host", "pi-desktop"])
            self.assertEqual(code, 0 if ready else 1)
            self.assertEqual(json.loads(output.getvalue()), {"ready": ready})


class ServeTests(unittest.TestCase):
    def test_capture_default_and_override_reach_server(self):
        for width in (None, 960, 65_535):
            with self.subTest(width=width):
                args = ["serve", "--host", "pi-desktop"]
                if width is not None:
                    args += ["--capture-max-width", str(width)]
                transport = mock.MagicMock()
                transport.__enter__.return_value = transport
                with mock.patch.object(cli, "SSHTransport", return_value=transport), \
                        mock.patch.object(cli, "create_server") as create:
                    self.assertEqual(cli.main(args), 0)
                create.assert_called_once_with("pi-desktop", transport=transport, capture_max_width=width,
                                               idle_timeout=300)
                create.return_value.run.assert_called_once_with(transport="stdio")
                transport.request.assert_not_called()
                transport.__exit__.assert_called_once()

    def test_bad_capture_policy_rejected_before_connection(self):
        for value in ("0", "-1", "65536", "true", "960.0", "no"):
            with self.subTest(value=value), mock.patch.object(cli, "SSHTransport") as transport, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["serve", "--capture-max-width", value])
                self.assertEqual(raised.exception.code, 2)
                transport.assert_not_called()

    def test_idle_policy_default_bounds_and_disabled_reach_server(self):
        for idle_timeout in (0, 1, 300, 3600):
            with self.subTest(idle_timeout=idle_timeout), mock.patch.object(cli, "SSHTransport") as transport, \
                    mock.patch.object(cli, "create_server") as create:
                self.assertEqual(cli.main(["serve", "--idle-timeout", str(idle_timeout)]), 0)
                self.assertEqual(create.call_args.kwargs["idle_timeout"], idle_timeout)

    def test_bad_idle_policy_rejected_before_connection(self):
        for value in ("-1", "3601", "true", "1.0", "no"):
            with self.subTest(value=value), mock.patch.object(cli, "SSHTransport") as transport, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["serve", "--idle-timeout", value])
                self.assertEqual(raised.exception.code, 2)
                transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
