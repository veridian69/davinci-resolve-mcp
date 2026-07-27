"""Call every read-only tool against live Resolve and record what the free edition supports.

The repo probes for newer API with hasattr(). Against a real PyRemoteObject that
always answers True, so those version guards never fire and a missing method
surfaces as a failure at call time instead. This sweep is how we find out which
tools those are, by calling them and writing down what happened.

    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \\
        free_edition/tools/sweep_free_edition.py

Calls run one at a time on purpose: the Resolve API executes one call at a time,
and the server serializes bodies on a lock anyway, so concurrency here would buy
nothing and muddy the timings.
"""

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time


def _repo_root():
    """Locate the checkout root, then prove it rather than assume it.

    This file lives three levels down (free_edition/tools/sweep_free_edition.py),
    so the walk is parents[2]. Getting that count wrong is completely silent:
    the whitelist and the transport handoff would be read from paths that never
    existed, and the sweep would blame a running server for not being there.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    if not (root / "src" / "granular").is_dir():
        raise SystemExit(
            f"cannot locate the checkout root: walked up from {__file__} to "
            f"{root}, which has no src/granular in it. Did this file move?")
    return str(root)


REPO = _repo_root()
# Runtime state lives at the CHECKOUT ROOT, not under free_edition/.
STATE_PATH = os.path.join(REPO, ".inproc", "transport.json")
CARTOGRAPHY = os.path.join(REPO, ".inproc", "cartography")
WHITELIST = os.path.join(CARTOGRAPHY, "whitelist.json")
RESULTS = os.path.join(CARTOGRAPHY, "sweep_results.json")
sys.path.insert(0, os.path.join(REPO, ".inproc", "deps"))

# The whitelist is produced by agents reading source. Trusting it blindly would
# put a project one misclassification away from harm, so every name gets a
# second opinion here -- one that reads only the name, independently of what the
# agents concluded from the code.
#
# The rule is the FIRST verb in the name, not any word in it. Substring matching
# was the obvious implementation and it was wrong: it rejected
# get_project_preset_list for containing "reset", timeline_get_markers for
# "mark", and get_current_render_mode for "render" -- all three verified
# read-only by hand. What decides a tool's action is the verb that leads it.
READ_VERBS = frozenset({
    "get", "list", "is", "are", "has", "have", "can", "count", "find", "read",
    "exists", "fetch", "query", "inspect", "describe", "show", "current",
})

WRITE_VERBS = frozenset({
    "add", "append", "apply", "analyze", "analyse", "archive", "auto",
    "classify", "clear", "close", "convert", "copy", "create", "cut", "delete",
    "detect", "disable", "duplicate", "enable", "export", "flatten", "generate",
    "grab", "import", "insert", "link", "load", "lock", "mark", "move", "new",
    "open", "paste", "perform", "quit", "refresh", "reload", "remove", "rename",
    "render", "reset", "restore", "save", "set", "sort", "start", "stop",
    "swap", "sync", "toggle", "transcribe", "trim", "unlink", "unlock",
    "update", "write", "append", "relink", "reorder", "replace",
})

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402


def load_endpoint():
    if not os.path.exists(STATE_PATH):
        raise SystemExit(
            f"no transport state at {STATE_PATH}\n"
            f"  paste free_edition/boot/resolve_console_boot.py in Resolve's "
            f"console first")
    with open(STATE_PATH, encoding="utf-8") as fh:
        state = json.load(fh)
    return f"{state['url']}/mcp", state["token"]


def load_whitelist():
    if not os.path.exists(WHITELIST):
        raise SystemExit(
            f"no whitelist at {WHITELIST}\n"
            f"  run the cartography workflow first")
    with open(WHITELIST, encoding="utf-8") as fh:
        names = json.load(fh)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise SystemExit(f"{WHITELIST} is not a JSON array of tool names")
    return names


def leading_verb(name):
    """The first recognised verb in a tool name, and whether it only reads.

    Names carry a namespace before the verb (ti_get_markers, folder_analyze_...),
    so unknown leading tokens are skipped rather than judged.
    """
    for token in name.lower().split("_"):
        if token in READ_VERBS:
            return token, True
        if token in WRITE_VERBS:
            return token, False
    return None, False


def screen(names):
    """Split the whitelist into names this script will call and ones it refuses."""
    allowed, refused = [], []
    for name in names:
        verb, reads = leading_verb(name)
        if verb is None:
            refused.append((name, "no recognised verb"))
        elif not reads:
            refused.append((name, f"leads with {verb!r}"))
        else:
            allowed.append(name)
    return allowed, refused


def classify_failure(payload):
    """Turn an error payload into a coarse cause, for grouping in the report."""
    text = str(payload).lower()
    if "not connected" in text or "no project" in text or "no timeline" in text:
        return "no-context"
    if "attribute" in text or "has no attribute" in text:
        return "missing-api"
    if "none" in text and "nonetype" in text:
        return "missing-api"
    if "not supported" in text or "studio" in text or "license" in text:
        return "studio-only"
    if "timeout" in text:
        return "timeout"
    return "other"


async def sweep(url, token, names, per_call_timeout):
    results = []
    async with streamablehttp_client(
            url, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            advertised = {t.name for t in (await session.list_tools()).tools}

            for index, name in enumerate(names, 1):
                if name not in advertised:
                    results.append({"tool": name, "status": "not-advertised",
                                    "cause": "absent", "seconds": 0.0})
                    print(f"  [{index:3d}/{len(names)}] {name:44s} NOT ADVERTISED")
                    continue

                started = time.time()
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(name, {}), timeout=per_call_timeout)
                except asyncio.TimeoutError:
                    elapsed = time.time() - started
                    results.append({"tool": name, "status": "timeout",
                                    "cause": "timeout", "seconds": elapsed})
                    print(f"  [{index:3d}/{len(names)}] {name:44s} TIMEOUT "
                          f"after {elapsed:.0f}s")
                    continue
                except Exception as exc:
                    elapsed = time.time() - started
                    results.append({"tool": name, "status": "raised",
                                    "cause": "transport",
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "seconds": elapsed})
                    print(f"  [{index:3d}/{len(names)}] {name:44s} RAISED "
                          f"{type(exc).__name__}")
                    continue

                elapsed = time.time() - started
                payload = result.structuredContent
                if isinstance(payload, dict) and set(payload) == {"result"}:
                    payload = payload["result"]
                if payload is None:
                    payload = " ".join(
                        getattr(b, "text", "") or "" for b in result.content)

                if result.isError:
                    results.append({"tool": name, "status": "error",
                                    "cause": classify_failure(payload),
                                    "detail": str(payload)[:400],
                                    "seconds": elapsed})
                    print(f"  [{index:3d}/{len(names)}] {name:44s} ERROR  "
                          f"{str(payload)[:60]}")
                else:
                    results.append({"tool": name, "status": "ok",
                                    "cause": None,
                                    "detail": str(payload)[:400],
                                    "seconds": elapsed})
                    print(f"  [{index:3d}/{len(names)}] {name:44s} ok     "
                          f"{str(payload)[:60]}")
    return results


def report(results, refused):
    by_status = {}
    for row in results:
        by_status.setdefault(row["status"], []).append(row)

    print()
    print("=" * 70)
    print("SWEEP SUMMARY")
    print("=" * 70)
    for status in sorted(by_status):
        print(f"  {status:16s} {len(by_status[status])}")

    failures = [r for r in results if r["status"] != "ok"]
    if failures:
        by_cause = {}
        for row in failures:
            by_cause.setdefault(row["cause"], []).append(row["tool"])
        print()
        print("  failures by cause:")
        for cause, tools in sorted(by_cause.items()):
            print(f"    {cause:14s} {len(tools)}")
            for tool in tools[:8]:
                print(f"      {tool}")
            if len(tools) > 8:
                print(f"      ... and {len(tools) - 8} more")

    slow = sorted(results, key=lambda r: -r["seconds"])[:5]
    print()
    print("  slowest calls:")
    for row in slow:
        print(f"    {row['seconds']:6.2f}s  {row['tool']}")

    if refused:
        print()
        print(f"  refused by the local screen ({len(refused)}) -- the whitelist "
              f"proposed these but their names carry a mutating verb:")
        for name, why in refused:
            print(f"    {name}  ({why})")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds before a single call is abandoned")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N tools; 0 means all")
    parser.add_argument("--dry-run", action="store_true",
                        help="screen the whitelist and print the plan, call nothing")
    args = parser.parse_args()

    names = load_whitelist()
    allowed, refused = screen(names)
    if args.limit:
        allowed = allowed[:args.limit]

    print(f"whitelist  : {len(names)} tools")
    print(f"screened   : {len(allowed)} will be called, {len(refused)} refused")
    if refused:
        for name, why in refused:
            print(f"  refused {name}  ({why})")

    if args.dry_run:
        print()
        print("dry run -- nothing was called")
        return 0

    url, token = load_endpoint()
    print(f"endpoint   : {url}")
    print()

    started = time.time()
    results = await sweep(url, token, allowed, args.timeout)
    elapsed = time.time() - started

    os.makedirs(CARTOGRAPHY, exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump({"results": results,
                   "refused": [{"tool": n, "why": w} for n, w in refused],
                   "elapsed_seconds": elapsed}, fh, indent=2)

    report(results, refused)
    print()
    print(f"  {elapsed:.0f}s total, written to {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
