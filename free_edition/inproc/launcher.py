"""Serve a FastMCP instance from a background thread inside Resolve.

Not reusing src/utils/mcp_transport.run_networked because it calls
uvicorn.run(), which installs signal handlers -- only legal on the main thread,
and the main thread here belongs to Resolve's UI. It also blocks forever, and
the console needs its thread back after the paste returns.

The auth posture is kept identical to the repo's: loopback bind, bearer token on
every request, constant-time compare, and the same transport state file so the
control panel can still find a live instance.
"""

import logging
import secrets
import socket
import threading
import time

logger = logging.getLogger("davinci-resolve-mcp.inproc.launcher")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ServerHandle:
    """A running server plus what a caller needs to reach or stop it."""

    def __init__(self, server, thread, host, port, token):
        self.server = server
        self.thread = thread
        self.host = host
        self.port = port
        self.token = token
        self.error = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def alive(self):
        return self.thread.is_alive()

    def stop(self, timeout=10.0):
        """Ask the server to exit and wait for its thread to unwind."""
        self.server.should_exit = True
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()


def _bearer_middleware(token):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected = f"Bearer {token}"

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            provided = request.headers.get("authorization", "")
            if not secrets.compare_digest(provided, expected):
                return JSONResponse(
                    {"error": "unauthorized: Authorization: Bearer <token> required"},
                    status_code=401)
            return await call_next(request)

    return BearerAuth


def port_is_free(host, port):
    """True when nothing holds the port. Checked before bind for a clear error."""
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def start(fastmcp, token, host=DEFAULT_HOST, port=DEFAULT_PORT, startup_timeout=15.0):
    """Serve `fastmcp` over authenticated streamable-http on a daemon thread.

    Raises RuntimeError if the port is taken or the server fails to come up,
    with the thread's own traceback attached rather than lost.
    """
    import uvicorn

    if not port_is_free(host, port):
        raise RuntimeError(
            f"port {port} on {host} is already bound -- stop the previous "
            f"server, or pass a different port")

    if host not in LOOPBACK_HOSTS:
        logger.warning(
            "SECURITY: binding MCP transport to NON-loopback host %r exposes "
            "Resolve control on the network", host)

    fastmcp.settings.host = host
    fastmcp.settings.port = port
    app = fastmcp.streamable_http_app()
    app.add_middleware(_bearer_middleware(token))

    # log_config=None keeps uvicorn from running dictConfig, whose default
    # formatter probes sys.stdout.isatty() -- absent on Fusion's fu_stdout.
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            log_config=None, lifespan="on")
    server = uvicorn.Server(config)
    # Signal handling is main-thread only; this server never owns that thread.
    server.install_signal_handlers = lambda: None
    server.capture_signals = _null_context

    failure = {}

    def serve():
        try:
            server.run()
        except BaseException:
            import traceback
            # A thread takes its traceback with it; keep a copy reachable.
            failure["traceback"] = traceback.format_exc()

    thread = threading.Thread(target=serve, name="inproc-mcp", daemon=True)
    thread.start()

    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        if getattr(server, "started", False):
            handle = ServerHandle(server, thread, host, port, token)
            logger.info("MCP streamable-http serving on %s", handle.url)
            return handle
        if not thread.is_alive():
            raise RuntimeError(
                "server thread died during startup:\n"
                + failure.get("traceback", "<no traceback recorded>"))
        time.sleep(0.1)

    server.should_exit = True
    raise RuntimeError(
        f"server did not report started within {startup_timeout}s:\n"
        + failure.get("traceback", "<thread still running, no traceback>"))


def _null_context():
    import contextlib
    return contextlib.nullcontext()


def write_transport_state(handle, state_path):
    """Publish url + token the way the repo's networked mode does."""
    import json
    import os

    payload = {
        "transport": "streamable-http",
        "host": handle.host,
        "port": handle.port,
        "url": f"http://{handle.host}:{handle.port}",
        "token": handle.token,
        "loopback": handle.host in LOOPBACK_HOSTS,
        "pid": os.getpid(),
        "started_at": time.time(),
        "inproc": True,
    }
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError as exc:
        logger.warning("could not write transport state: %s", exc)
    return payload
