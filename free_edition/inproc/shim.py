"""Stand in for DaVinciResolveScript so the repo binds to the live console handle.

The repo reaches Resolve through exactly one door:

    src/granular/common.py:74   sys.path.append(RESOLVE_MODULES_PATH)
    src/granular/common.py:234  import DaVinciResolveScript as dvr_script
    src/granular/common.py:236  resolve = dvr_script.scriptapp("Resolve")

Left alone inside the console that import finds the real module, which loads
fusionscript and asks the internal script server for a handle -- the server the
free edition disables. scriptapp() returns None and every tool is dead.

Registering this module in sys.modules first means the import resolves from the
module cache and never consults sys.path, so fusionscript is never loaded and
scriptapp() hands back the object the console already has.

Install this BEFORE importing any repo module. If the repo imports first,
common.py catches the ImportError and leaves both `resolve` and `dvr_script`
as None at module scope; _try_connect() then raises AttributeError on None and
the module stays poisoned until it is purged from sys.modules and re-imported.
"""

import logging
import sys
import types

logger = logging.getLogger("davinci-resolve-mcp.inproc.shim")

MODULE_NAME = "DaVinciResolveScript"
MARKER = "<inproc-shim>"


def build(resolve_handle):
    """Return a module object that impersonates the native scripting bridge."""
    module = types.ModuleType(MODULE_NAME)
    module.__file__ = MARKER
    module.__doc__ = "In-process stand-in for the DaVinci Resolve scripting bridge."

    def scriptapp(name="Resolve"):
        """Mirror the native entry point for the app objects the repo asks for."""
        if name == "Resolve":
            return resolve_handle
        if name == "Fusion":
            try:
                return resolve_handle.Fusion()
            except Exception as exc:
                logger.warning("Fusion() unavailable through the shim: %s", exc)
                return None
        logger.warning("scriptapp(%r) is not served by the shim", name)
        return None

    module.scriptapp = scriptapp
    return module


def install(resolve_handle):
    """Put the shim in sys.modules. Returns the module for verification."""
    module = build(resolve_handle)
    sys.modules[MODULE_NAME] = module
    logger.info("DaVinciResolveScript shim installed")
    return module


def is_installed():
    """True when an import of DaVinciResolveScript would resolve to the shim."""
    module = sys.modules.get(MODULE_NAME)
    return getattr(module, "__file__", None) == MARKER


def purge_repo_modules():
    """Drop cached repo modules so a reload re-runs their import-time connect.

    granular/common.py binds `resolve` at import time, so a stale module keeps a
    stale handle -- or a None left over from an import that ran before the shim.

    Covers `free_edition.*` as well as `src.*`: the documented workflow is to
    edit a file and re-paste the boot line, and a purge that skipped our own
    package would silently keep serving the code the edit replaced.
    """
    doomed = [name for name in list(sys.modules)
              if name in ("src", "free_edition")
              or name.startswith(("src.", "free_edition."))]
    for name in doomed:
        del sys.modules[name]
    return doomed
