"""The `inproc` status payload the dashboard card renders.

Lifted out of upstream's `tests/test_dashboard_transport.py`, which stays
byte-identical to upstream and covers only upstream's own networked transport.
These cases cover `free_edition.dashboard.status`, which upstream neither
imports nor knows about -- `free_edition.integrate.install_dashboard_card()`
wraps `_mcp_status_payload` at runtime to add the key these functions compute.
"""

import http.server
import json
import os
import tempfile
import threading
import unittest
import unittest.mock

from free_edition.dashboard import status as inproc_status_module
from free_edition.dashboard.status import inproc_status


# The relocated functions derive the checkout root themselves rather than
# borrowing upstream's dashboard `_repo_root` (they now sit two levels below the
# root instead of one). Whichever name that seam ends up with, it has to stay
# patchable: these tests must never read the real .inproc/, which on a developer
# machine may hold a live bridge inside an actual Resolve session.
_ROOT_HOOK_NAMES = ("_repo_root", "repo_root", "_REPO_ROOT", "REPO_ROOT", "REPO")


def _point_at_fake_checkout(test, repo):
    """Patch the status module's repo-root seam to `repo`. Returns its name."""
    for name in _ROOT_HOOK_NAMES:
        if not hasattr(inproc_status_module, name):
            continue
        current = getattr(inproc_status_module, name)
        if callable(current):
            patcher = unittest.mock.patch.object(
                inproc_status_module, name, return_value=repo)
        else:
            # A module-level constant (str or pathlib.Path): keep its type so
            # os.path.join and Path arithmetic behave the same as in production.
            patcher = unittest.mock.patch.object(
                inproc_status_module, name, type(current)(repo))
        patcher.start()
        test.addCleanup(patcher.stop)
        return name
    raise AssertionError(
        "free_edition.dashboard.status exposes no patchable repo-root seam "
        f"(looked for {', '.join(_ROOT_HOOK_NAMES)}). inproc_status() must "
        "resolve the checkout root through a named module attribute, not an "
        "inline pathlib expression, or it cannot be tested against a fake "
        "checkout and these tests would read the developer's real .inproc/.")


class InprocStatusTest(unittest.TestCase):
    """inproc_status() reads .inproc/transport.json from a repo checkout the
    dashboard is NOT running inside of -- unlike upstream's networked
    transport, the bridge is a separate process this dashboard cannot start or
    stop, only report on. So every case here fakes a checkout via a temp dir
    rather than touching the real .inproc/ (which may have a live bridge
    running inside an actual Resolve session)."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.inproc_dir = os.path.join(self.repo, ".inproc")
        os.makedirs(self.inproc_dir, exist_ok=True)
        _point_at_fake_checkout(self, self.repo)

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
        status = inproc_status()

        self.assertEqual(status, {"configured": False})

    def test_dead_pid_is_reported_as_not_alive(self):
        # A pid essentially guaranteed not to exist.
        self._write_state(pid=2**30)

        status = inproc_status()

        self.assertTrue(status["configured"])
        self.assertFalse(status["pid_alive"])
        self.assertFalse(status["reachable"])

    def test_live_pid_but_nothing_listening_is_not_reachable(self):
        # Our own pid is alive, but nothing is bound to this port.
        self._write_state(pid=os.getpid(), url="http://127.0.0.1:1")

        status = inproc_status()

        self.assertTrue(status["pid_alive"])
        self.assertFalse(status["reachable"])

    def test_a_real_server_answering_401_counts_as_reachable(self):
        """Mirrors what the bridge's bearer-auth middleware actually returns
        for an unauthenticated request -- this is the same signal
        free_edition/inproc/selftest.py uses to prove the server thread is
        serving."""
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

        status = inproc_status()

        self.assertTrue(status["reachable"])

    def test_log_tail_is_the_last_twenty_lines(self):
        self._write_state(pid=2**30)
        with open(os.path.join(self.inproc_dir, "inproc.log"), "w") as fh:
            for i in range(30):
                fh.write(f"line {i}\n")

        status = inproc_status()

        self.assertEqual(len(status["log_tail"]), 20)
        self.assertEqual(status["log_tail"][0], "line 10\n")
        self.assertEqual(status["log_tail"][-1], "line 29\n")

    def test_missing_log_is_an_empty_tail_not_an_error(self):
        self._write_state(pid=2**30)

        status = inproc_status()

        self.assertEqual(status["log_tail"], [])
        self.assertIsNone(status["log_path"])

    def test_token_is_carried_through_for_the_ui_to_display(self):
        self._write_state(pid=2**30, token="a-real-looking-token")

        status = inproc_status()

        self.assertEqual(status["token"], "a-real-looking-token")


if __name__ == "__main__":
    unittest.main()
