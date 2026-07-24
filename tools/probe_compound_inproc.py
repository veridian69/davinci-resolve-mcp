"""Can the compound server be imported inside Resolve's console?

The granular surface (341 tools) is what the in-process bridge serves today.
Everything that turns a transcript into cuts -- media analysis, the edit engine,
the transcript store, strata -- hangs off the *other* FastMCP instance in
src/server.py, and `--full` runs one INSTEAD of the other rather than both.

So: does src/server.py even import in there? It is 25k lines with a wide
dependency surface, and .inproc/deps was vendored for the granular server only.
Any missing third-party module shows up here, by name, instead of halfway
through a redesign.

Run AFTER the normal boot, so the shim and sys.path are already in place:

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/resolve_console_boot.py").read())
    exec(open("/path/to/davinci-resolve-mcp/tools/probe_compound_inproc.py").read())

This probe imports and counts. It starts no server and touches no project.

ASCII-only, like every file the console reads.
"""

import sys
import time
import traceback


def _line(label, value):
    print("  {:<32} {}".format(label, value))


def main():
    print("=" * 68)
    print("COMPOUND SERVER IN-PROCESS PROBE")
    print("=" * 68)

    bridge = sys.modules.get("DaVinciResolveScript")
    shim_active = getattr(bridge, "__file__", None) == "<inproc-shim>"
    _line("shim installed", shim_active)
    if not shim_active:
        print()
        print("  Run resolve_console_boot.py first. Without the shim the import")
        print("  below will try the real bridge, get None, and tell you nothing")
        print("  about whether the compound server itself is viable.")
        return

    granular = sys.modules.get("src.granular.common")
    if granular is not None:
        try:
            _line("granular tools already up",
                  len(granular.mcp._tool_manager._tools))
        except Exception as exc:
            _line("granular tool count", "unavailable ({})".format(exc))

    print()
    print("-- importing src.server")
    started = time.time()
    try:
        import src.server as compound
    except ImportError as exc:
        elapsed = time.time() - started
        print()
        print("  IMPORT FAILED after {:.1f}s".format(elapsed))
        # The module name is the whole point of running this: it names exactly
        # what still has to be vendored into .inproc/deps.
        _line("missing module", getattr(exc, "name", None) or "<unknown>")
        _line("message", str(exc)[:160])
        print()
        print("  Vendor it with:")
        print("    python3 tools/setup_inproc.py --python <console python>")
        print("  after adding the package to that script's requirement list.")
        return
    except Exception:
        print()
        print("  IMPORT RAISED (not a missing dependency):")
        traceback.print_exc()
        print()
        print("  This is the more interesting failure: something in the module")
        print("  body does not survive being imported inside Resolve.")
        return

    elapsed = time.time() - started
    print()
    _line("imported in", "{:.1f}s".format(elapsed))

    try:
        tools = compound.mcp._tool_manager._tools
        _line("compound tools", len(tools))
        names = sorted(tools)
        _line("first few", ", ".join(names[:6]))
    except Exception as exc:
        _line("tool count", "unavailable ({}: {})".format(type(exc).__name__, exc))
        names = []

    # The specific capabilities the transcript-to-cuts workflow needs. Named
    # individually because "it imported" is not the same as "the parts I care
    # about are reachable".
    print()
    print("-- workflow pieces")
    for module_path, why in (
        ("src.utils.media_analysis", "transcription + capability detection"),
        ("src.utils.analysis_store", "writes transcript_segments incl. speaker"),
        ("src.utils.edit_engine", "plans cuts from transcript gaps"),
        ("src.utils.strata_story", "transcript digest with speakers"),
        ("src.utils.timeline_brain_db", "the brain DB schema"),
    ):
        try:
            __import__(module_path)
            _line(module_path.rsplit(".", 1)[-1], "ok -- " + why)
        except Exception as exc:
            _line(module_path.rsplit(".", 1)[-1],
                  "FAILED ({}: {})".format(type(exc).__name__, str(exc)[:70]))

    print()
    print("probe done -- no server started, nothing written")


main()
