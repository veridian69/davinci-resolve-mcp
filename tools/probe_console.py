"""Probe Resolve's Python console to see what the in-process MCP approach needs.

Run from Resolve: Workspace -> Console -> Py3 dropdown, then paste one line:

    exec(open("/path/to/davinci-resolve-mcp/tools/probe_console.py").read())

Answers three questions:
  1. Which interpreter does the console use (so we know where to pip install)?
  2. Can it spawn a background thread?
  3. Can it bind a loopback socket?
"""

import os
import platform
import site
import socket
import sys
import threading

print("=" * 60)
print("INTERPRETER")
print("=" * 60)
print(f"  sys.executable : {sys.executable!r}")
print(f"  sys.version    : {sys.version.splitlines()[0]}")
print(f"  sys.prefix     : {sys.prefix}")
print(f"  sys.base_prefix: {sys.base_prefix}")
print(f"  platform       : {platform.platform()}")
try:
    print(f"  user site      : {site.getusersitepackages()}")
except Exception as exc:
    print(f"  user site      : unavailable ({exc})")
print("  sys.path:")
for entry in sys.path:
    print(f"    {entry or '<cwd>'}")

print()
print("=" * 60)
print("RESOLVE HANDLE")
print("=" * 60)
handle = globals().get("resolve")
if handle is None:
    print("  resolve is NOT in the console namespace -- the whole plan depends on it")
else:
    print(f"  product : {handle.GetProductName()}")
    print(f"  version : {handle.GetVersionString()}")
    pm = handle.GetProjectManager()
    proj = pm.GetCurrentProject() if pm else None
    print(f"  project : {proj.GetName() if proj else '<none open>'}")
    # The hasattr question: does a missing method raise AttributeError?
    print(f"  hasattr(resolve, 'NoExisteEsteMetodo') = "
          f"{hasattr(handle, 'NoExisteEsteMetodo')}   # want False")

print()
print("=" * 60)
print("THREADING")
print("=" * 60)
_thread_result = {}


def _worker():
    _thread_result["tid"] = threading.get_ident()
    try:
        h = globals().get("resolve")
        _thread_result["api_from_thread"] = h.GetVersionString() if h else "<no handle>"
    except Exception as exc:
        _thread_result["api_from_thread"] = f"FAILED: {exc}"


_t = threading.Thread(target=_worker, daemon=True)
_t.start()
_t.join(timeout=10)
if _t.is_alive():
    print("  thread did NOT finish in 10s -- background server is not viable")
else:
    print(f"  main tid   : {threading.get_ident()}")
    print(f"  worker tid : {_thread_result.get('tid')}")
    print(f"  API call from worker thread: {_thread_result.get('api_from_thread')}")

print()
print("=" * 60)
print("SOCKET")
print("=" * 60)
try:
    _s = socket.socket()
    _s.bind(("127.0.0.1", 0))
    _s.listen(1)
    print(f"  bound loopback OK: {_s.getsockname()}")
    _s.close()
except Exception as exc:
    print(f"  bind FAILED: {exc}")

print()
print("=" * 60)
print("THIRD-PARTY IMPORTS")
print("=" * 60)
for _mod in ("pip", "mcp", "anyio", "pydantic", "rpyc"):
    try:
        _m = __import__(_mod)
        print(f"  {_mod:10s} present  {getattr(_m, '__file__', '<builtin>')}")
    except ImportError:
        print(f"  {_mod:10s} MISSING")

print()
print("=" * 60)
print("SUBPROCESS ENV")
print("=" * 60)
for _var in ("RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB", "PYTHONPATH", "PYTHONHOME"):
    print(f"  {_var:20s} = {os.environ.get(_var, '<unset>')}")

print()
print("probe done")
