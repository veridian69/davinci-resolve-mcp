"""Run the console boot outside Resolve, against a simulated console.

Every real console round trip costs a human paste, so most bugs should be caught
here instead. This reproduces the three console behaviours that broke the
prototype:

  * `resolve` is a PyRemoteObject: attribute access returns a callable for ANY
    name, so hasattr() is always True and never signals a missing method.
  * sys.stdout is bound to the console's thread and raises SystemError when
    written from any other thread.
  * the boot file is read by an ASCII-default open().

What it cannot reproduce is Resolve itself. A pass here means the plumbing is
sound; it says nothing about whether a tool does the right thing to a timeline.

    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \\
        tools/fake_console.py
"""

import argparse
import logging as logging_module
import os
import sys
import threading
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT = os.path.join(REPO, "resolve_console_boot.py")


# Seconds every fake API call blocks for. Zero by default; the concurrency
# check raises it so overlapping calls would actually overlap if the bridge
# lock were missing. With instant calls, a broken lock and a working one look
# the same.
FAKE_CALL_DELAY = 0.0


# The probe name src/inproc/selftest.py asks about. The real bridge returns None
# for every absent name; this fake cannot enumerate Resolve's whole surface, so
# it declares absence for a named set instead and keeps answering everything
# else. Same observable behaviour where it counts.
ABSENT_METHODS = frozenset({
    "NoExisteEsteMetodoXyz123",
    "PerformAudioClassification",
})


class PyRemoteObject:
    """Stands in for Resolve's bridge object, down to the class name.

    src/inproc/api_probe.py keys off type(obj).__name__, so the name matters.
    Reproduces what tools/probe_api_identity.py measured on Resolve 21.0.3.7:
    an absent method comes back as None WITHOUT raising, which is precisely why
    builtins.hasattr reports it as present.
    """

    def __init__(self, type_name="Resolve", responses=None):
        self._type_name = type_name
        self._responses = responses or {}

    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in ABSENT_METHODS:
            return None

        def call(*args, **kwargs):
            if FAKE_CALL_DELAY:
                import time
                time.sleep(FAKE_CALL_DELAY)
            if attr in self._responses:
                value = self._responses[attr]
                # Forwarded so a response can behave like the real Resolve
                # method it stands in for, e.g. GetClipProperty(propertyName)
                # answering based on which property was actually asked for.
                return value(*args, **kwargs) if callable(value) else value
            return PyRemoteObject(f"{self._type_name}.{attr}")

        return call

    def __dir__(self):
        return sorted(self._responses)

    def __str__(self):
        return (f"{self._type_name} (0x0fake0000) "
                f"[App: 'Resolve' on 127.0.0.1, UUID: fake-uuid]")

    def __repr__(self):
        return "<BlackmagicFusion.PyRemoteObject object at 0xfake>"


def build_fake_resolve():
    timeline = PyRemoteObject("Timeline", {
        "GetName": "Fake Timeline",
        "GetUniqueId": "fake-timeline-0001",
        "GetStartFrame": 0,
        "GetEndFrame": 240,
    })
    project = PyRemoteObject("Project", {
        "GetName": "Fake Project",
        "GetTimelineCount": 1,
        "GetCurrentTimeline": timeline,
        "GetTimelineByIndex": timeline,
        "GetSetting": "",
    })
    project_manager = PyRemoteObject("ProjectManager", {
        "GetCurrentProject": project,
        "GetProjectListInCurrentFolder": ["Fake Project"],
        "GetCurrentDatabase": {"DbType": "Disk", "DbName": "Fake Database"},
    })
    return PyRemoteObject("Resolve", {
        "GetProductName": "DaVinci Resolve",
        "GetVersionString": "21.0.3.7",
        "GetVersion": [21, 0, 3, 7],
        "GetProjectManager": project_manager,
        "GetCurrentPage": "edit",
    })


class ThreadBoundStream:
    """Raises off-thread the way Fusion's fu_stdout does."""

    def __init__(self, target):
        self._target = target
        self._owner = threading.get_ident()
        self.off_thread_writes = 0

    def write(self, text):
        if threading.get_ident() != self._owner:
            self.off_thread_writes += 1
            raise SystemError(
                "<built-in function write> returned a result with an exception set")
        return self._target.write(text)

    def flush(self):
        if threading.get_ident() != self._owner:
            raise SystemError("flush called off the console thread")
        self._target.flush()


def check_boot_is_ascii():
    """The console reads this file before any encoding fix can be installed."""
    with open(BOOT, "rb") as handle:
        raw = handle.read()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        offending = raw[exc.start:exc.start + 1]
        line = raw[:exc.start].count(b"\n") + 1
        return False, (f"non-ASCII byte {offending!r} at line {line} -- the "
                       f"console's open() would fail before running a thing")
    return True, f"{len(raw)} bytes, ASCII clean"


def _write_from_thread(target):
    """Run `target` on another thread, returning its exception if it raised."""
    outcome = {}

    def runner():
        try:
            target()
            outcome["ok"] = True
        except BaseException as exc:
            outcome["ok"] = False
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=runner, name="off-thread-probe")
    thread.start()
    thread.join(timeout=10)
    if not thread.is_alive() and "ok" not in outcome:
        outcome["error"] = "thread produced no outcome"
    return outcome


def exercise_off_thread_write(fake_stdout, real_stdout):
    """Force the failure ConsoleSafeStream exists to prevent.

    A clean boot proves nothing on its own: if no background thread ever wrote,
    the wrapper was never exercised. Two probes run here -- a control that must
    fail, then the real one that must not. Without the control, a wrapper that
    silently swallowed everything would look identical to a working one.
    """
    import logging

    marker = "off-thread-write-probe-8f3a"
    lines = []

    # Control: the raw console stream must still blow up off-thread. If this
    # passes, the simulation is not reproducing Resolve and nothing below means
    # anything.
    control = _write_from_thread(lambda: fake_stdout.write("control\n"))
    if control.get("ok"):
        lines.append("  fake console control : FAIL  raw stream did NOT raise "
                     "off-thread; the simulation is not faithful")
        control_ok = False
    else:
        lines.append(f"  fake console control : ok    raw stream raised "
                     f"{control['error'].split(':')[0]} as Resolve would")
        control_ok = True

    before = fake_stdout.off_thread_writes

    def probe():
        print(marker)
        logging.getLogger("davinci-resolve-mcp.faketest").warning(marker)

    outcome = _write_from_thread(probe)

    if not outcome.get("ok"):
        lines.append(f"  off-thread write     : FAIL  "
                     f"{outcome.get('error', 'thread hung')}")
        _flush(lines, real_stdout)
        return False

    log_path = os.path.join(REPO, ".inproc",
                            os.environ.get("INPROC_LOG_NAME", "inproc.log"))
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            landed = marker in fh.read()
    except OSError as exc:
        lines.append(f"  off-thread write     : FAIL  cannot read log: {exc}")
        _flush(lines, real_stdout)
        return False

    if not landed:
        lines.append("  off-thread write     : FAIL  did not raise, but never "
                     "reached the log")
        _flush(lines, real_stdout)
        return False

    # The wrapper should have absorbed the write without ever touching the
    # thread-bound stream underneath.
    reached_raw = fake_stdout.off_thread_writes - before
    if reached_raw:
        lines.append(f"  off-thread write     : FAIL  {reached_raw} write(s) hit "
                     f"the thread-bound stream; the wrapper is not intercepting")
        _flush(lines, real_stdout)
        return False

    lines.append("  off-thread write     : ok    reached the log, never touched "
                 "the console stream")
    _flush(lines, real_stdout)
    return control_ok


def _flush(lines, real_stdout):
    """Report on the real stdout, since sys.stdout is still the wrapper here."""
    real_stdout.write("\n" + "\n".join(lines) + "\n")
    real_stdout.flush()


def run_client(handle):
    """Drive the fake server with a real MCP client, end to end."""
    import asyncio

    sys.path.insert(0, os.path.join(REPO, ".inproc", "deps"))
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def drive():
        async with streamablehttp_client(
                handle.url,
                headers={"Authorization": f"Bearer {handle.token}"}) as (r, w, _):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool("get_current_database", {})
                return init, listed, result

    print()
    try:
        init, listed, result = asyncio.run(drive())
    except Exception as exc:
        print(f"  mcp client       : FAIL  {type(exc).__name__}: {exc}")
        return False

    print(f"  mcp client       : ok    {init.serverInfo.name}, "
          f"{len(listed.tools)} tools")
    payload = result.structuredContent or result.content
    print(f"  tool call        : {'FAIL' if result.isError else 'ok  '}  "
          f"{str(payload)[:110]}")
    if result.isError:
        return False

    return check_serialization(handle)


def check_serialization(handle, calls=8, delay=0.05):
    """Fire concurrent tool calls and require the bridge lock to serialize them.

    The Resolve API executes one call at a time, so peak overlap must stay at 1.
    Anything higher means two tool bodies were inside the bridge together.
    """
    import asyncio
    import time

    global FAKE_CALL_DELAY

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from src.inproc import dispatch

    async def hammer():
        async with streamablehttp_client(
                handle.url,
                headers={"Authorization": f"Bearer {handle.token}"}) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await asyncio.gather(*[
                    session.call_tool("get_current_database", {})
                    for _ in range(calls)
                ])

    dispatch.reset_stats()
    FAKE_CALL_DELAY = delay
    started = time.time()
    try:
        results = asyncio.run(hammer())
    except Exception as exc:
        print(f"  serialization    : FAIL  {type(exc).__name__}: {exc}")
        return False
    finally:
        FAKE_CALL_DELAY = 0.0
    elapsed = time.time() - started

    errors = sum(1 for r in results if r.isError)
    peak = dispatch.stats()["peak_concurrent"]

    if errors:
        print(f"  serialization    : FAIL  {errors}/{calls} calls errored")
        return False
    if peak != 1:
        print(f"  serialization    : FAIL  peak concurrency {peak}, wanted 1 -- "
              f"two tool bodies were in the bridge at once")
        return False
    print(f"  serialization    : ok    {calls} concurrent calls, peak overlap 1, "
          f"{elapsed:.1f}s serial as expected")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="8799",
                        help="a port the real console server is not using")
    parser.add_argument("--keep-alive", action="store_true",
                        help="leave the server running so a client can hit it")
    parser.add_argument("--break-lock", action="store_true",
                        help="disable the bridge lock; the serialization check "
                             "must then FAIL, proving it has teeth")
    args = parser.parse_args()

    os.environ["DAVINCI_MCP_PORT"] = args.port
    os.environ.setdefault("DAVINCI_MCP_TOKEN", "fake-console-token")
    # Its own log, state and token files. Sharing them with the real console
    # interleaves two processes' tracebacks, and -- worse -- publishes this
    # harness's hardcoded token as the live server's credential, which is
    # exactly what happened once.
    os.environ["INPROC_LOG_NAME"] = "fake_console.log"
    os.environ["INPROC_STATE_NAME"] = "fake_transport.json"
    os.environ["INPROC_TOKEN_NAME"] = "fake_token"

    print("=" * 64)
    print("FAKE CONSOLE -- booting outside Resolve")
    print("=" * 64)

    ok, detail = check_boot_is_ascii()
    print(f"  boot file encoding : {'ok' if ok else 'FAIL'}  {detail}")
    if not ok:
        return 1

    real_stdout = sys.stdout
    fake_stdout = ThreadBoundStream(real_stdout)
    sys.stdout = fake_stdout

    # Reproduce the condition that broke the first real boot: a StreamHandler
    # built before we ran, holding the raw console stream. logging's state is
    # global and survives every sys.modules purge, so once one of these exists
    # it poisons every record emitted off the console thread.
    stray = logging_module.StreamHandler(fake_stdout)
    stray.setFormatter(logging_module.Formatter("stray: %(message)s"))
    logging_module.getLogger().addHandler(stray)
    print(f"  planted stray handler: {stray} on the root logger")

    namespace = {
        "resolve": build_fake_resolve(),
        "INPROC_REPO": REPO,
        "__name__": "__console__",
    }

    failed = False
    try:
        with open(BOOT, encoding="ascii") as handle:
            source = handle.read()
        exec(compile(source, BOOT, "exec"), namespace)
    except BaseException:
        failed = True
        sys.stdout = real_stdout
        print()
        print("BOOT RAISED:")
        traceback.print_exc()

    # Deliberately NOT restoring sys.stdout yet. The boot replaced it with a
    # ConsoleSafeStream, and that wrapper is exactly what the next check tests;
    # restoring here would make the probe pass without ever exercising it.
    handle = namespace.get("__inproc_handle__")
    if handle is None:
        sys.stdout = real_stdout
        print()
        print("FAKE CONSOLE: boot did not produce a server handle")
        return 1

    try:
        if not exercise_off_thread_write(fake_stdout, real_stdout):
            failed = True
    finally:
        sys.stdout = real_stdout

    if args.break_lock:
        from src.inproc import dispatch

        class _NoLock:
            def __enter__(self):
                return None

            def __exit__(self, *exc_info):
                return False

        dispatch.bridge_lock = _NoLock()
        print("  bridge lock DISABLED -- serialization check should now fail")

    print(f"  server alive: {handle.alive}  url: {handle.url}")

    if not args.keep_alive:
        if not run_client(handle):
            failed = True

    if args.keep_alive:
        print()
        print(f"  leaving it up. token: {handle.token}")
        print("  press Ctrl-C to stop")
        try:
            while handle.alive:
                threading.Event().wait(1.0)
        except KeyboardInterrupt:
            pass

    stopped = handle.stop()
    print(f"  stopped cleanly: {stopped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
