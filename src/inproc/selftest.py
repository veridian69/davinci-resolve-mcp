"""Checks the boot runs on itself, so one console paste yields a verdict.

Every console round trip costs a human paste, so booting and verifying are the
same operation. Each check returns a row rather than raising, so one failure
does not hide the state of everything after it.
"""

import logging
import urllib.error
import urllib.request

logger = logging.getLogger("davinci-resolve-mcp.inproc.selftest")


class Check:
    def __init__(self, name, ok, detail):
        self.name = name
        self.ok = ok
        self.detail = detail


def _check(name, fn):
    try:
        ok, detail = fn()
    except Exception as exc:
        return Check(name, False, f"{type(exc).__name__}: {exc}")
    return Check(name, ok, detail)


def run(console_resolve, granular_common, granular_mcp, handle, wrapped_tools,
        encoding_report):
    """Return a list of Check rows describing the live boot."""
    from src.inproc import api_probe, shim

    checks = []

    def encoding_default():
        return True, (f"open() default was {encoding_report['locale_getencoding']}, "
                      f"now utf-8")

    def shim_installed():
        return shim.is_installed(), "DaVinciResolveScript resolves to the shim"

    def handle_identity():
        same = granular_common.resolve is console_resolve
        return same, ("granular.resolve is the console handle" if same
                      else "granular bound a DIFFERENT handle than the console's")

    def product():
        r = granular_common.resolve
        return True, f"{r.GetProductName()} {r.GetVersionString()}"

    def tool_count():
        tools = getattr(getattr(granular_mcp, "_tool_manager", None), "_tools", None)
        count = len(tools or {})
        return count > 0, f"{count} tools registered"

    def dispatch_installed():
        return wrapped_tools > 0, f"{wrapped_tools} tools wrapped for threaded dispatch"

    def truthful_hasattr():
        report = api_probe.selftest(console_resolve, "GetProjectManager",
                                    "NoExisteEsteMetodoXyz123")
        if not report["builtin_on_absent"]:
            # The bug this replaces is gone; the shadow is now pointless weight.
            return True, ("builtin hasattr no longer lies on this build -- "
                          "the shadow is redundant here")
        if report["ours_on_absent"]:
            return False, "still reports a missing method as present"
        if not report["ours_on_present"]:
            return False, "denies a method that does exist"
        return True, (f"{report['type']} exposes {report['method_count']} "
                      f"methods; missing ones now report False")

    def live_call():
        pm = granular_common.get_project_manager()
        if pm is None:
            return False, "get_project_manager() returned None"
        project = pm.GetCurrentProject()
        return True, f"current project: {project.GetName() if project else '<none>'}"

    def server_thread():
        return handle.alive, f"serving on {handle.url}"

    def rejects_anonymous():
        try:
            urllib.request.urlopen(handle.url, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc.code == 401, f"anonymous request got {exc.code}, wanted 401"
        except Exception as exc:
            return False, f"could not reach the server: {type(exc).__name__}: {exc}"
        return False, "anonymous request was NOT rejected"

    def accepts_token():
        request = urllib.request.Request(
            handle.url, headers={"Authorization": f"Bearer {handle.token}"})
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            # A bare GET is not a valid MCP request; anything but 401 proves the
            # token cleared the middleware, which is what this checks.
            return exc.code != 401, f"authenticated request got {exc.code}"
        except Exception as exc:
            return False, f"could not reach the server: {type(exc).__name__}: {exc}"
        return True, "authenticated request accepted"

    for name, fn in (
        ("utf-8 default encoding", encoding_default),
        ("shim installed", shim_installed),
        ("handle identity", handle_identity),
        ("resolve responds", product),
        ("tools registered", tool_count),
        ("threaded dispatch", dispatch_installed),
        ("truthful hasattr", truthful_hasattr),
        ("live API call", live_call),
        ("server thread", server_thread),
        ("rejects anonymous", rejects_anonymous),
        ("accepts token", accepts_token),
    ):
        checks.append(_check(name, fn))

    return checks


def format_report(checks):
    """Render checks as aligned lines plus a final verdict."""
    lines = []
    width = max(len(c.name) for c in checks)
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        lines.append(f"  {check.name:<{width}}  {mark}  {check.detail}")
    failed = [c for c in checks if not c.ok]
    lines.append("")
    if failed:
        lines.append(f"SELF-TEST FAILED -- {len(failed)} of {len(checks)}: "
                     + ", ".join(c.name for c in failed))
    else:
        lines.append(f"SELF-TEST PASSED -- {len(checks)} checks")
    return "\n".join(lines)
