"""Serialize tool bodies and keep them off the transport's event loop.

Two separate problems, one wrapper.

Blocking: the MCP SDK calls a synchronous tool function directly on the single
asyncio event-loop thread. A slow Resolve call freezes the whole server -- the
transport included -- until it returns.

Concurrency: the Resolve scripting API executes one call at a time. Two tool
bodies must never be inside it at once. Note that in-process this is easier to
violate than it looks, because the console itself is a second caller: a script
pasted while the server is working is concurrency the lock cannot see.

This mirrors src/server.py's _install_threaded_tool_dispatch rather than
importing it, because importing src.server pulls in 25k lines and a second
FastMCP instance for a surface we are not serving.

Couples to mcp SDK private attributes (ToolManager._tools, Tool.fn,
Tool.is_async; verified on mcp 1.28). Best effort: an unexpected shape leaves
the tool untouched, falling back to inline behavior.
"""

import functools
import logging
import threading

logger = logging.getLogger("davinci-resolve-mcp.inproc.dispatch")

# Held for the whole body of any synchronous tool. A body that outlives a
# client cancellation still runs to completion holding it, so the bridge is
# never left half-mutated.
bridge_lock = threading.Lock()

# Diagnostics for the concurrency test: peak observed overlap must stay at 1.
_active = 0
_peak_active = 0
_counter_lock = threading.Lock()


def stats():
    """Return dispatch counters. peak_concurrent > 1 means the lock failed."""
    with _counter_lock:
        return {"active": _active, "peak_concurrent": _peak_active}


def reset_stats():
    global _peak_active
    with _counter_lock:
        _peak_active = _active


def _offloaded(fn):
    @functools.wraps(fn)
    async def run_off_thread(**kwargs):
        import anyio

        def call():
            global _active, _peak_active
            with bridge_lock:
                with _counter_lock:
                    _active += 1
                    _peak_active = max(_peak_active, _active)
                try:
                    return fn(**kwargs)
                finally:
                    with _counter_lock:
                        _active -= 1

        return await anyio.to_thread.run_sync(call)

    return run_off_thread


def install(fastmcp):
    """Wrap every synchronous tool on `fastmcp`. Returns the count wrapped."""
    manager = getattr(fastmcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict) or not tools:
        logger.warning("no tool registry found; threaded dispatch not installed")
        return 0

    wrapped = 0
    for tool in tools.values():
        if getattr(tool, "is_async", False):
            continue
        try:
            tool.fn = _offloaded(tool.fn)
            tool.is_async = True
        except Exception as exc:
            logger.warning("threaded dispatch skipped for %s: %s",
                           getattr(tool, "name", "?"), exc)
            continue
        wrapped += 1

    logger.info("threaded tool dispatch installed for %d tools", wrapped)
    return wrapped
