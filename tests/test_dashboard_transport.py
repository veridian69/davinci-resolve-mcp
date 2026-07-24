"""Tests for dashboard transport status/start/stop helpers."""
import http.server
import json
import os
import tempfile
import threading
import unittest
import unittest.mock

import src.analysis_dashboard as dash
from src.utils import mcp_transport as T


class DashboardTransportTest(unittest.TestCase):
    def tearDown(self):
        T.clear_transport_state()

    def test_status_local_when_no_state(self):
        T.clear_transport_state()
        st = dash._transport_status()
        self.assertFalse(st["networked"])
        self.assertIn("stdio", st["mode"])

    def test_status_networked_from_state(self):
        T.write_transport_state("streamable-http", "127.0.0.1", 8799, "tok")
        st = dash._transport_status()
        self.assertTrue(st["networked"])
        self.assertTrue(st["loopback"])
        self.assertEqual(st["url"], "http://127.0.0.1:8799")
        self.assertTrue(st["has_token"])

    def test_start_refuses_when_already_running(self):
        # Our own live pid keeps the state "alive", so start must refuse.
        import os
        T.write_transport_state("streamable-http", "127.0.0.1", 8799, "tok")
        # ensure the state pid is this (alive) process so read_transport_state keeps it
        import json
        with open(T.TRANSPORT_STATE_PATH, "w") as fh:
            json.dump({"transport": "streamable-http", "host": "127.0.0.1", "port": 8799,
                       "url": "http://127.0.0.1:8799", "token": "tok", "loopback": True,
                       "pid": os.getpid()}, fh)
        out = dash._transport_start()
        self.assertFalse(out["success"])
        self.assertIn("already running", out["error"])

    def test_stop_noop_when_not_running(self):
        T.clear_transport_state()
        out = dash._transport_stop()
        self.assertTrue(out["success"])


class InprocStatusTest(unittest.TestCase):
    """_inproc_status() reads .inproc/transport.json from a repo checkout the
    dashboard is NOT running inside of -- unlike the networked transport
    above, the bridge is a separate process this dashboard cannot start or
    stop, only report on. So every case here fakes a checkout via a temp dir
    rather than touching the real .inproc/ (which may have a live bridge
    running inside an actual Resolve session)."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.inproc_dir = os.path.join(self.repo, ".inproc")
        os.makedirs(self.inproc_dir, exist_ok=True)
        self.patcher = unittest.mock.patch.object(
            dash, "_repo_root", return_value=self.repo)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _write_state(self, **overrides):
        state = {
            "transport": "streamable-http", "host": "127.0.0.1", "port": 8765,
            "url": "http://127.0.0.1:8765", "token": "tok", "loopback": True,
            "pid": os.getpid(), "started_at": 1700000000.0,
        }
        state.update(overrides)
        with open(os.path.join(self.inproc_dir, "transport.json"), "w") as fh:
            json.dump(state, fh)

    def test_unconfigured_when_no_state_file(self):
        status = dash._inproc_status()

        self.assertEqual(status, {"configured": False})

    def test_dead_pid_is_reported_as_not_alive(self):
        # A pid essentially guaranteed not to exist.
        self._write_state(pid=2**30)

        status = dash._inproc_status()

        self.assertTrue(status["configured"])
        self.assertFalse(status["pid_alive"])
        self.assertFalse(status["reachable"])

    def test_live_pid_but_nothing_listening_is_not_reachable(self):
        # Our own pid is alive, but nothing is bound to this port.
        self._write_state(pid=os.getpid(), url="http://127.0.0.1:1")

        status = dash._inproc_status()

        self.assertTrue(status["pid_alive"])
        self.assertFalse(status["reachable"])

    def test_a_real_server_answering_401_counts_as_reachable(self):
        """Mirrors what the bridge's bearer-auth middleware actually returns
        for an unauthenticated request -- this is the same signal
        src/inproc/selftest.py uses to prove the server thread is serving."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        self._write_state(pid=os.getpid(), url=f"http://127.0.0.1:{port}")

        status = dash._inproc_status()

        self.assertTrue(status["reachable"])

    def test_log_tail_is_the_last_twenty_lines(self):
        self._write_state(pid=2**30)
        with open(os.path.join(self.inproc_dir, "inproc.log"), "w") as fh:
            for i in range(30):
                fh.write(f"line {i}\n")

        status = dash._inproc_status()

        self.assertEqual(len(status["log_tail"]), 20)
        self.assertEqual(status["log_tail"][0], "line 10\n")
        self.assertEqual(status["log_tail"][-1], "line 29\n")

    def test_missing_log_is_an_empty_tail_not_an_error(self):
        self._write_state(pid=2**30)

        status = dash._inproc_status()

        self.assertEqual(status["log_tail"], [])
        self.assertIsNone(status["log_path"])

    def test_token_is_carried_through_for_the_ui_to_display(self):
        self._write_state(pid=2**30, token="a-real-looking-token")

        status = dash._inproc_status()

        self.assertEqual(status["token"], "a-real-looking-token")


if __name__ == "__main__":
    unittest.main()
