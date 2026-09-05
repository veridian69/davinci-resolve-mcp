"""A hasattr() that tells the truth about Resolve objects.

The repo guards newer API behind `hasattr(obj, "SomeMethod")`. Against a live
PyRemoteObject that guard never fires, because:

    getattr(project, "NoSuchMethod")  ->  None          (no exception raised)
    getattr(project, "GetName")       ->  PyFunctionCall

hasattr() is defined as "getattr did not raise", so a missing method reports
True. The guard passes, the code calls None, and the caller gets
"TypeError: 'NoneType' object is not callable" instead of the clear message the
guard was written to produce.

Measured on DaVinci Resolve 21.0.3.7, free edition, via
free_edition/tools/probe_api_identity.py.

Shadowing rather than editing: assigning a module-global named `hasattr` wins
over the builtin for code in that module, so the repo's 25 guard sites are fixed
without touching a line of upstream source.

What this does NOT solve: Studio-gated features. AutoSyncAudio and
DetectSceneCuts both exist as PyFunctionCall on the free edition -- the method is
present and the gate is enforced when it runs. No amount of introspection finds
those; only calling them does.
"""

import builtins
import logging
import re

logger = logging.getLogger("davinci-resolve-mcp.inproc.api_probe")

REMOTE_TYPE = "PyRemoteObject"

# str(obj) renders as "Project (0x0x31146efc0) [App: 'Resolve' on 127.0.0.1...]"
_TYPE_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*\(")

# Method sets are per object type, not per instance, so one dir() per type is
# enough. dir() on a remote object is a round trip; the guards run on hot paths.
_method_cache = {}


def remote_type(obj):
    """Return 'Project', 'Timeline', ... for a Resolve object, else None."""
    if type(obj).__name__ != REMOTE_TYPE:
        return None
    try:
        match = _TYPE_PATTERN.match(str(obj))
    except Exception:
        return None
    return match.group(1) if match else None


def methods(obj):
    """The object's real method names, cached per remote type."""
    type_name = remote_type(obj)
    if type_name is None:
        return frozenset()
    cached = _method_cache.get(type_name)
    if cached is None:
        try:
            cached = frozenset(n for n in dir(obj) if not n.startswith("_"))
        except Exception as exc:
            logger.warning("dir() failed on %s: %s", type_name, exc)
            cached = frozenset()
        _method_cache[type_name] = cached
    return cached


def has_api(obj, name):
    """True when `name` is really implemented on this Resolve object."""
    if type(obj).__name__ != REMOTE_TYPE:
        return builtins.hasattr(obj, name)
    # getattr is the cheap authority: absent names come back as None. dir() is
    # the cross-check, and covers any object whose str() we cannot parse.
    try:
        if getattr(obj, name, None) is not None:
            return True
    except Exception:
        return False
    return name in methods(obj)


def hasattr(obj, name):  # noqa: A001 - shadowing the builtin is the point
    """Drop-in replacement for the builtin, honest about Resolve objects."""
    return has_api(obj, name)


def install(module_prefix="src.granular"):
    """Shadow hasattr inside every already-imported module under `module_prefix`.

    Must run AFTER the repo is imported: it patches module namespaces, so a
    module imported later keeps the builtin.
    """
    import sys

    patched = []
    for name, module in list(sys.modules.items()):
        if not (name == module_prefix or name.startswith(module_prefix + ".")):
            continue
        if module is None:
            continue
        try:
            module.hasattr = hasattr
            patched.append(name)
        except Exception as exc:
            logger.warning("could not shadow hasattr in %s: %s", name, exc)
    logger.info("truthful hasattr installed in %d modules", len(patched))
    return patched


def selftest(obj, present, absent):
    """Compare the builtin against this module on one known-present, one absent.

    Returns a dict rather than asserting, so a boot can report the comparison
    even when it comes out wrong.
    """
    return {
        "type": remote_type(obj),
        "method_count": len(methods(obj)),
        "builtin_on_present": builtins.hasattr(obj, present),
        "builtin_on_absent": builtins.hasattr(obj, absent),
        "ours_on_present": has_api(obj, present),
        "ours_on_absent": has_api(obj, absent),
    }
