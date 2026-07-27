"""Start the MCP server inside DaVinci Resolve. Paste one line in the console.

    Workspace -> Console -> Py3 dropdown, then:

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/free_edition/boot/resolve_console_boot.py").read())

INPROC_REPO names the CHECKOUT ROOT, not the directory this file sits in. The
line names it once and the boot reads it back, because exec(open(...).read())
leaves no __file__ to derive a path from. Hardcoding one here would work on
exactly one machine, and there is nothing to derive one from -- which is also
why moving this script into free_edition/boot/ changed the paste line but not
what INPROC_REPO means.

Everything the running server writes stays at <checkout>/.inproc/. The state
directory did not move when this script did.

Re-paste the same line to reload after editing any module: the running server
is stopped and every repo module is re-imported -- ours under free_edition as
well as upstream's under src.

This file is deliberately ASCII-only. The console's open() defaults to the C
locale's US-ASCII, so it reads this file before any encoding fix can exist --
a single accented character here makes the paste fail with UnicodeDecodeError.
Modules imported later are exempt: Python always reads source as UTF-8.

The bearer token is generated once and saved to .inproc/token, so an MCP client
configured against it keeps working across reboots. DAVINCI_MCP_TOKEN overrides
the file when set.
"""

import os
import sys

REPO = globals().get("INPROC_REPO") or os.environ.get("INPROC_REPO")
_INPROC = os.path.join(REPO, ".inproc") if REPO else ""
DEPS = os.path.join(_INPROC, "deps")
LOG_PATH = os.path.join(_INPROC,
                        os.environ.get("INPROC_LOG_NAME", "inproc.log"))
# Overridable so the offline harness cannot overwrite the real server's
# endpoint and token. It shares this process's code but not its identity, and
# clients read these files to find the live server.
STATE_PATH = os.path.join(_INPROC,
                          os.environ.get("INPROC_STATE_NAME", "transport.json"))
TOKEN_PATH = os.path.join(_INPROC,
                          os.environ.get("INPROC_TOKEN_NAME", "token"))
HOST = os.environ.get("DAVINCI_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("DAVINCI_MCP_PORT", "8765"))

_console_resolve = globals().get("resolve")


class BootFailed(Exception):
    """Aborts the boot. Never SystemExit -- that can take Resolve down with it."""


def _print_registrations(report):
    """Print what free_edition wired into upstream, and anything it could not.

    Every line here is a silent failure made visible. A count that reads lower
    than expected is the only symptom several of these have: nothing raises when
    a captured `detect_capabilities` keeps the original, when the second
    `_launch_resolve` stays unguarded, or when the subtitle tools land on a
    different FastMCP instance.
    """
    whisperx = report.get("whisperx") or {}
    subtitles = report.get("subtitle_tools") or {}
    guarded = report.get("autolaunch") or []
    rebound = whisperx.get("rebound") or []

    print(f"  state dir           : {report.get('state_dir')}")
    print(f"  autolaunch guarded  : {len(guarded)} {guarded}")
    print(f"  detect_capabilities : rebound in {len(rebound)} {rebound}")
    print(f"  _transcribe patched : {bool(whisperx.get('transcribe'))}")
    if report.get("subtitle_tools") is not None:
        note = " (already registered)" if subtitles.get("already_registered") else ""
        print(f"  subtitle tools      : +{subtitles.get('added', 0)}{note}")
    if report.get("dashboard_card") is not None:
        card = report["dashboard_card"]
        print(f"  dashboard card      : markup={card.get('markup')} "
              f"payload={card.get('payload')}")
    for problem in report.get("problems") or []:
        print(f"  PROBLEM: {problem}")


def _boot():
    if not REPO:
        raise BootFailed(
            "INPROC_REPO is not set. Paste this instead, with your own path:\n"
            '  INPROC_REPO="/path/to/davinci-resolve-mcp"; '
            'exec(open(INPROC_REPO+"/free_edition/boot/resolve_console_boot.py").read())')

    if not os.path.isdir(os.path.join(REPO, "free_edition", "inproc")):
        raise BootFailed(
            f"{REPO} does not look like the checkout: no free_edition/inproc "
            f"in it (INPROC_REPO is the repo root, not this script's folder)")

    if _console_resolve is None:
        raise BootFailed(
            "no `resolve` in the console namespace -- run this from Resolve's "
            "Workspace -> Console with the Py3 dropdown selected")

    if not os.path.isdir(DEPS):
        raise BootFailed(
            f"dependencies missing at {DEPS}\n"
            f"  build them first, from a normal shell:\n"
            f"  python3 {os.path.join(REPO, 'free_edition', 'tools', 'setup_inproc.py')}")

    # Stop a server from an earlier paste before anything else rebinds state.
    previous = globals().get("__inproc_handle__")
    stopped = None
    if previous is not None:
        stopped = previous.stop()

    for path in (DEPS, REPO):
        while path in sys.path:
            sys.path.remove(path)
    sys.path.insert(0, DEPS)
    sys.path.insert(0, REPO)

    # Purge before importing so edits are picked up on reload. Both packages:
    # a purge that skipped free_edition would reload upstream correctly while
    # silently re-running the free-edition code the edit replaced, and the only
    # evidence would be a fix that "did not work". Safe to drop free_edition
    # here because this script is exec'd, not imported -- it has no module
    # identity to destroy, and nothing under free_edition is executing yet.
    doomed = [name for name in list(sys.modules)
              if name in ("src", "free_edition")
              or name.startswith(("src.", "free_edition."))]
    for name in doomed:
        del sys.modules[name]

    from free_edition.inproc import (api_probe, dispatch, encoding, launcher,
                                     selftest, shim, streams)

    # Encoding first: everything after this reads files.
    encoding_report = encoding.install()
    encoding.verify(os.path.join(_INPROC, "encoding_probe.txt"))

    # Streams second: from here a background traceback can survive.
    stream_report = streams.install(LOG_PATH, globals())

    print("DaVinci Resolve MCP -- in-process boot")
    print(f"  repo    : {REPO}")
    print(f"  log     : {LOG_PATH}")
    if stopped is not None:
        print(f"  previous server stopped: {stopped}")
    print(f"  modules purged: {len(doomed)}")
    strays = stream_report["retargeted_handlers"]
    if strays:
        # Left alone these raise SystemError on every record logged off the
        # console thread, and take the real traceback down with them.
        print(f"  stray log handlers retargeted: {len(strays)} -> {strays}")

    # The shim must land before any repo import: granular/common.py binds
    # `resolve` at import time and caches a None if the bridge answers first.
    shim.install(_console_resolve)

    from src.granular import common as granular_common
    from src.granular import mcp as granular_mcp

    # Upstream is imported and pristine on disk; our layer goes in on top of it
    # at runtime. install_all() owns the ordering contract -- autolaunch guard,
    # then whisperX, then the subtitle tools -- so the three boots cannot drift
    # apart, and it returns one report the selftest can assert against.
    import free_edition.integrate as integrate

    print()
    registrations = integrate.install_all(console_resolve=_console_resolve,
                                          granular_mcp=granular_mcp,
                                          repo_root=REPO)
    _print_registrations(registrations)

    # After the import, before serving: shadowing patches module namespaces, so
    # a module imported later would keep the lying builtin. The prefix stays
    # "src.granular" -- upstream's ~25 hasattr guards are what this protects,
    # and free_edition has none of its own.
    guarded = api_probe.install("src.granular")
    print(f"  truthful hasattr installed in {len(guarded)} modules")

    # Last, and never before the subtitle tools are registered: install() walks
    # the registry once and skips whatever it already wrapped, so a tool that
    # arrives afterwards keeps running on the asyncio event-loop thread and can
    # enter the single-threaded Resolve bridge concurrently with another tool.
    # That failure is an intermittent hang, never an exception.
    wrapped = dispatch.install(granular_mcp)

    # A token that changes every boot invalidates every MCP client config, and
    # Resolve is a GUI app so it never sees a shell's exported variables.
    # Persisting one on first boot is what makes the client config stay valid.
    token = os.environ.get("DAVINCI_MCP_TOKEN")
    source = "environment"
    if not token and os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            token = fh.read().strip() or None
        source = "file"
    if not token:
        import secrets
        token = secrets.token_urlsafe(24)
        with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(TOKEN_PATH, 0o600)
        source = "generated, saved"

    handle = launcher.start(granular_mcp, token, host=HOST, port=PORT)
    launcher.write_transport_state(handle, STATE_PATH)
    globals()["__inproc_handle__"] = handle

    print()
    checks = selftest.run(_console_resolve, granular_common, granular_mcp,
                          handle, wrapped, encoding_report, registrations)
    print(selftest.format_report(checks))

    print()
    print(f"  url   : {handle.url}")
    print(f"  token : {token}   ({source})")
    print(f"  state : {STATE_PATH}")
    print()
    print("  re-paste the same exec(...) line to reload after edits")
    return handle


try:
    _boot()
except BootFailed as _exc:
    print()
    print(f"BOOT FAILED: {_exc}")
except Exception:
    import traceback
    print()
    print("BOOT FAILED with an unexpected error:")
    traceback.print_exc()
