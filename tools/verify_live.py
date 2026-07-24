"""Drive the in-process server from outside, against real Resolve.

Reads the url and bearer token the boot published, so nothing is hardcoded and
no secret has to be copied around.

    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \\
        tools/verify_live.py [--no-write]

The write cycle creates a timeline and a marker, reads both back, then removes
them. An interrupted earlier run leaves its timeline behind; this cleans that up
on the way in rather than refusing to start.
"""

import argparse
import asyncio
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO, ".inproc", "transport.json")
sys.path.insert(0, os.path.join(REPO, ".inproc", "deps"))

TL_NAME = "mcp-verify-live"
MARKER_FRAME = 0
MARKER_COLOR = "Blue"

READ_ONLY = [
    "get_current_database",
    "get_database_list",
    "get_current_project_folder",
    "get_color_groups_list",
    "get_current_render_mode",
    "get_current_render_format_and_codec",
]

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402


def load_endpoint():
    if not os.path.exists(STATE_PATH):
        raise SystemExit(
            f"no transport state at {STATE_PATH}\n"
            f"  boot the server first: paste resolve_console_boot.py in "
            f"Resolve's console")
    with open(STATE_PATH, encoding="utf-8") as fh:
        state = json.load(fh)
    return f"{state['url']}/mcp", state["token"]


class Runner:
    def __init__(self, session):
        self.session = session
        self.failures = []

    async def call(self, name, args=None, label=None, quiet=False):
        label = label or name
        try:
            result = await self.session.call_tool(name, args or {})
        except Exception as exc:
            print(f"  {label:36s} RAISED {type(exc).__name__}: {exc}")
            self.failures.append(label)
            return None

        payload = result.structuredContent
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]
        if payload is None:
            payload = " ".join(getattr(b, "text", "") or "" for b in result.content)

        if result.isError:
            print(f"  {label:36s} ERROR  {str(payload)[:120]}")
            self.failures.append(label)
            return None
        if not quiet:
            print(f"  {label:36s} ok     {str(payload)[:120]}")
        return payload


async def timeline_names(run):
    names = await run.call("list_timelines_tool", quiet=True)
    if isinstance(names, str):
        names = [] if "No timelines" in names else [names]
    return names or []


async def timeline_id(run, name, count):
    for index in range(1, count + 1):
        info = await run.call("get_timeline_by_index", {"index": index}, quiet=True)
        if isinstance(info, dict) and info.get("name") == name:
            return info.get("unique_id")
    return None


async def drop_timeline(run, name, names):
    found = await timeline_id(run, name, len(names))
    if not found:
        print(f"  {'locate timeline id':36s} FAIL   delete '{name}' by hand")
        return False
    await run.call("delete_timelines_by_id", {"timeline_ids": [found]},
                   label=f"delete {name}")
    return True


async def write_cycle(run):
    print()
    print("WRITE CYCLE")
    names = await timeline_names(run)
    if TL_NAME in names:
        print(f"  leftover '{TL_NAME}' from an earlier run -- removing it first")
        await drop_timeline(run, TL_NAME, names)
        names = await timeline_names(run)
    baseline = list(names)
    print(f"  baseline timelines: {baseline}")

    await run.call("create_timeline", {"name": TL_NAME})
    names = await timeline_names(run)
    if TL_NAME not in names:
        print("  create_timeline reported success but state did not change")
        return False
    print(f"  after create: {names}")

    await run.call("timeline_add_marker", {
        "frame_id": MARKER_FRAME, "color": MARKER_COLOR, "name": TL_NAME})
    markers = await run.call("timeline_get_markers")
    marker_ok = bool(markers) and str(MARKER_FRAME) in str(markers)
    print(f"  marker read back: {marker_ok}")
    if marker_ok:
        await run.call("timeline_delete_marker_at_frame", {"frame_id": MARKER_FRAME})

    await drop_timeline(run, TL_NAME, names)
    names = await timeline_names(run)
    print(f"  after revert: {names}")

    restored = sorted(names) == sorted(baseline)
    print(f"  restored to baseline: {restored}")
    return restored and marker_ok


async def main(do_write):
    url, token = load_endpoint()
    print(f"endpoint: {url}")

    async with streamablehttp_client(
            url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            print(f"  server: {init.serverInfo.name} {init.serverInfo.version}, "
                  f"{len(listed.tools)} tools")

            run = Runner(session)
            print()
            print("READ-ONLY")
            for name in READ_ONLY:
                await run.call(name)

            write_ok = True
            if do_write:
                write_ok = await write_cycle(run)

            print()
            if run.failures:
                print(f"FAILED -- {len(run.failures)} call(s): {run.failures}")
                return 1
            if not write_ok:
                print("FAILED -- the write cycle did not round-trip cleanly")
                return 1
            print("LIVE VERIFICATION PASSED"
                  + ("" if do_write else " (reads only)"))
            return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-write", action="store_true",
                        help="skip the write cycle; touch nothing in the project")
    parsed = parser.parse_args()
    sys.exit(asyncio.run(main(not parsed.no_write)))
