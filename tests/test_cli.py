"""Exercise lease-free health diagnostics and their readiness exit status."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import types
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


class DashboardLaunchTests(unittest.TestCase):
    def test_ui_defaults_and_overrides_reach_launcher_without_opening_ssh(self):
        for options, port, width in (([], 0, 960), (["--port", "41233", "--capture-max-width", "640"], 41233, 640)):
            with self.subTest(options=options), mock.patch.object(cli, "run_ui") as launch, \
                    mock.patch.object(cli, "SSHTransport") as transport:
                self.assertEqual(cli.main(["ui", "--host", "pi-target", *options]), 0)
                launch.assert_called_once_with("pi-target", port=port, capture_max_width=width)
                transport.assert_not_called()

    def test_invalid_ui_port_is_rejected_before_launch(self):
        for value in ("-1", "65536", "true", "1.0", "not-a-port"):
            with self.subTest(value=value), mock.patch.object(cli, "run_ui") as launch, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(["ui", "--port", value])
                self.assertEqual(raised.exception.code, 2)
                launch.assert_not_called()

    def test_target_host_never_becomes_the_listen_address_or_token_query(self):
        import uvicorn

        module = types.ModuleType("pi_desktop_bridge.dashboard")
        module.create_dashboard = mock.MagicMock()
        listener = mock.MagicMock()
        listener.getsockname.return_value = ("127.0.0.1", 41233)
        socket_context = mock.MagicMock()
        socket_context.__enter__.return_value = listener
        with mock.patch.dict("sys.modules", {"pi_desktop_bridge.dashboard": module}), \
                mock.patch.object(cli.socket, "socket", return_value=socket_context), \
                mock.patch.object(cli.secrets, "token_urlsafe", return_value="test-capability"), \
                mock.patch.object(uvicorn, "Config") as config, \
                mock.patch.object(uvicorn, "Server") as server, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            cli.run_ui("remote-pi.example", port=0)
        listener.bind.assert_called_once_with(("127.0.0.1", 0))
        self.assertFalse(config.call_args.kwargs["access_log"])
        self.assertEqual(config.call_args.kwargs["host"], "127.0.0.1")
        module.create_dashboard.assert_called_once_with(
            token="test-capability", default_host="remote-pi.example",
            origin="http://127.0.0.1:41233", capture_max_width=960,
        )
        server.return_value.run.assert_called_once_with(sockets=[listener])
        self.assertIn("http://127.0.0.1:41233/#token=test-capability", output.getvalue())
        self.assertNotIn("?token=", output.getvalue())
        socket_context.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
