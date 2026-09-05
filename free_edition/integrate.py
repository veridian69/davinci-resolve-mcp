"""Every hook this fork needs in upstream, applied at runtime instead of in the file.

This module is the keystone of the layout: it is the reason `git diff
<upstream> HEAD -- ':!free_edition'` is empty. Where the fork used to carry
edits inside `src/`, it now imports upstream unmodified and rebinds module
attributes afterwards. Same behaviour, zero merge conflicts.

What used to be an edit, and what replaces it:

===========================  ==============================================
static edit                  runtime replacement
===========================  ==============================================
`_launch_resolve` guard in   `disable_autolaunch()` -- and it covers
`src/granular/common.py`     `src/server.py`'s byte-identical duplicate too,
                             which the static edit never did
12 edits to                  `register_whisperx()`
`src/utils/media_analysis.py`
`subtitles,` in              `register_subtitle_tools(granular_mcp)`
`src/granular/__init__.py`
4 hunks in                   `install_dashboard_card(dash)`
`src/analysis_dashboard.py`
3 lines in `.gitignore`      `ensure_state_dir()`
===========================  ==============================================

ORDERING CONTRACT
-----------------
The boot scripts call these after upstream is imported and before the server
serves. Two orderings are load-bearing and neither one raises when violated:

* `register_whisperx()` BEFORE `register_subtitle_tools()`. The tools module
  reaches upstream helpers through the backend; registering the backend second
  means the first transcription runs against a `_transcribe` that has never
  heard of whisperX.

* `register_subtitle_tools(mcp)` BEFORE `dispatch.install(mcp)`. `install()`
  iterates the registry once, so tools registered afterwards never get wrapped:
  they still appear in the tool list and still work, but they run on the
  asyncio event-loop thread and can enter the single-threaded Resolve bridge
  concurrently with another tool. The result is an intermittent hang that never
  reproduces on demand. `register_subtitle_tools()` detects that inversion and
  re-runs `dispatch.install()` itself (it skips already-wrapped tools, so this
  costs nothing and cannot double-wrap), but the correct order is still the
  documented one.

IDEMPOTENCE
-----------
Every public function is safe to call twice, because the documented workflow is
"edit a file, re-paste the boot line". Each one records a sentinel on the object
it patched and returns the previous report on re-entry. The check is
evidence-based rather than a bare flag: a sentinel whose wrapper is no longer
installed is treated as absent, so a partially unwound state re-installs cleanly
instead of returning a report about wrappers that are gone.

NOTHING UPSTREAM IS IMPORTED AT MODULE SCOPE
--------------------------------------------
Every `src.*` import happens inside a function body. The boot purges both `src.*`
and `free_edition.*` from `sys.modules` and re-imports in a fixed order (shim,
then upstream, then this module); a module-scope import here would bind whatever
happened to be loaded when this file was first read and quietly outlive the
purge, which is exactly the failure the purge exists to prevent.
"""

import importlib
import logging
import os
import pathlib
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("davinci-resolve-mcp.integrate")


class IntegrationError(RuntimeError):
    """A hook could not be installed. Raised at install time, never at call time."""


class UpstreamAnchorMissing(IntegrationError):
    """Upstream no longer has something a hook needs -- usually after a merge."""


class SubtitleToolsNotRegistered(IntegrationError):
    """The two subtitle tools did not land on the FastMCP instance."""


# ---------------------------------------------------------------------------
# What we patch, named once.
# ---------------------------------------------------------------------------

#: Modules that define their own `_launch_resolve`. `src/server.py:819` is a
#: byte-for-byte duplicate of `src/granular/common.py:326`; the static edit this
#: replaces only ever covered the first, so the compound boot could still open a
#: second Resolve and block ~60s waiting for it.
AUTOLAUNCH_MODULES: Tuple[str, ...] = ("src.granular.common", "src.server")

#: Modules that bind `detect_capabilities` with a MODULE-LEVEL from-import, each
#: therefore holding the ORIGINAL function object forever. Patching
#: `media_analysis.detect_capabilities` alone leaves them calling the unwrapped
#: version: whisperX is reported absent, transcription silently falls back to a
#: backend that cannot diarize, and nothing raises.
#:
#: Four, not three. Every recon survey of this delta listed the first three and
#: missed `src/server.py` -- which is the module the compound console boots, so
#: the miss lands precisely on the least-tested path.
#:
#: `src/server.py` binds it under an ALIAS (`detect_capabilities as
#: detect_media_analysis_capabilities`, line 76, used at 7752/17605/17761), which
#: is why the rebind below matches on object identity instead of on the name.
#: A name-based rebind would create an unused `detect_capabilities` attribute
#: there and leave the alias -- the one the code actually calls -- pointing at
#: the original. Same silent failure, now with a green verification.
DETECT_CAPABILITIES_CAPTORS: Tuple[str, ...] = (
    "src.utils.media_analysis_jobs",
    "src.batch_cli",
    "src.analysis_dashboard",
    "src.server",
)

#: The two tools `free_edition/subtitles/tools.py` registers by import side effect.
SUBTITLE_TOOLS: Tuple[str, ...] = (
    "whisperx_transcribe_timeline_item",
    "whisperx_import_subtitles",
)

#: Two tools in `src/granular/project.py` that raise on every call in pristine
#: upstream. The fork used to carry these as static one-line edits; they are
#: patched at runtime now for the same reason as everything else here -- so
#: `src/granular/project.py` stays byte-identical and never conflicts on a merge.
#:
#: These are NOT free-edition concerns. They are upstream defects that happen to
#: be ours to work around, and both deserve an upstream PR; see
#: `free_edition/docs/upstream-bugs.md`. Until such a PR lands, a fork that
#: reverted them to pristine to win a clean diff would be shipping two tools it
#: knows are broken, which is a worse trade than a nine-line wrapper.
PROJECT_BUGFIX_TOOLS: Tuple[str, ...] = ("close_project", "load_cloud_project_tool")

PROJECT_BUGFIX_SENTINEL = "__free_edition_project_bugfixes__"

#: Attributes of `src.utils.media_analysis` that `register_whisperx()` reads or
#: replaces. Checked up front so an upstream merge that renames one fails here,
#: naming it, instead of at the first transcription an hour later.
_MEDIA_ANALYSIS_ANCHORS: Tuple[str, ...] = (
    "TOOL_INSTALL", "install_plan_for", "detect_capabilities", "_transcribe",
    "_coerce_bool", "DEFAULT_TRANSCRIPTION_ENABLED",
    "AVG_TRANSCRIPTION_TOKENS_PER_SECOND", "_check_caps_pre_call",
    "_record_caps_usage", "_resolve_active_caps", "_analysis_caps",
)

# Sentinels. Dunder-ish because they land in namespaces we do not own.
WHISPERX_SENTINEL = "__free_edition_whisperx__"
AUTOLAUNCH_SENTINEL = "__free_edition_autolaunch__"

#: Marks a callable as ours, so idempotence can be decided from the object
#: rather than from a flag that may have outlived it.
_OURS = "__free_edition__"

#: Duplicate of `free_edition.inproc.shim.MARKER`, used only when that module
#: cannot be imported. Kept in sync by `free_edition/tests/` -- the literal also
#: appears in the console probes, which is why the shim's copy may not change.
_SHIM_MARKER = "<inproc-shim>"


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _repo_root() -> pathlib.Path:
    """The checkout root, proved rather than assumed.

    `parents[1]` because this file is `free_edition/integrate.py`, one package
    below the root. Getting the count wrong is otherwise completely silent:
    `os.makedirs` cheerfully creates `<repo>/free_edition/.inproc/`, and the
    mistake surfaces minutes later and one layer away.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    if not (root / "src" / "granular").is_dir():
        raise IntegrationError(
            f"expected the checkout root one level above this file, got {root}, "
            f"where {root / 'src' / 'granular'} is not a directory -- "
            f"free_edition/integrate.py has moved relative to the repo root")
    return root


def _bridge_installed() -> bool:
    """True when `import DaVinciResolveScript` resolves to the in-process shim."""
    try:
        from free_edition.inproc import shim
    except ImportError:  # pragma: no cover - only if the layer is half-installed
        bridge = sys.modules.get("DaVinciResolveScript")
        return getattr(bridge, "__file__", None) == _SHIM_MARKER
    return shim.is_installed()


def _rebind_by_identity(module: Any, targets: Tuple[Any, ...],
                        replacement: Any) -> List[str]:
    """Rebind every module-level name bound to one of `targets`. Returns them.

    Identity, not name: `src/server.py` imports `detect_capabilities as
    detect_media_analysis_capabilities`, and a name-based rebind would miss it
    while looking like it worked.

    `targets` usually holds both the original and the replacement, so a name
    that is already correct is still *reported* as bound. Without that, a second
    registration would report zero rebinds and read as a regression when in fact
    nothing needed doing.
    """
    try:
        namespace = vars(module)
    except TypeError:  # pragma: no cover - something non-module in sys.modules
        return []
    names = [name for name, value in list(namespace.items())
             if any(value is target for target in targets)]
    for name in names:
        if getattr(module, name) is not replacement:
            setattr(module, name, replacement)
    return names


# ---------------------------------------------------------------------------
# FUNCTION 1: disable_autolaunch
# ---------------------------------------------------------------------------

def disable_autolaunch(*module_names: str) -> List[str]:
    """Stop `_launch_resolve()` from opening a second Resolve, in every module.

    Replaces the seven-line guard the fork used to carry inside
    `src/granular/common.py:_launch_resolve`.

    Running *inside* Resolve there is nothing to launch: a handle that stops
    answering means the project closed, not that the app is gone.
    `get_resolve()` calls `_launch_resolve()` whenever `_try_connect()` fails
    (`src/granular/common.py:371`, `src/server.py:863`), and the unguarded
    version would `open` a SECOND Resolve instance and then block ~60s waiting
    for the instance that can never answer -- inside a tool call, on Resolve's
    own UI thread.

    Both call sites resolve `_launch_resolve` as a bare module global at call
    time, which is what makes rebinding the module attribute enough.

    Defaults to `AUTOLAUNCH_MODULES`. Pass names explicitly to cover a module
    this list does not know about; names not in `sys.modules`, or without a
    `_launch_resolve`, are skipped silently -- the dashboard boot imports
    neither of the defaults and should not have to care.

    Returns the module names that now carry the guard, including ones a previous
    call installed. The boot prints it: a caller that expects two and sees one
    has found a regression, which is the whole reason this returns a list rather
    than None.
    """
    targets = module_names or AUTOLAUNCH_MODULES
    guarded: List[str] = []

    for name in targets:
        module = sys.modules.get(name)
        if module is None:
            logger.debug("free-edition: %s not imported; no autolaunch guard", name)
            continue
        original = getattr(module, "_launch_resolve", None)
        if not callable(original):
            logger.debug("free-edition: %s has no _launch_resolve", name)
            continue
        if getattr(original, _OURS, False):
            guarded.append(name)  # already guarded; re-paste, not a second wrap
            continue

        setattr(module, "_launch_resolve", _guarded_launch_resolve(original, name))
        setattr(module, AUTOLAUNCH_SENTINEL, True)
        guarded.append(name)

    logger.info("free-edition: autolaunch guard installed in %s",
                ", ".join(guarded) or "no modules")
    return guarded


def _guarded_launch_resolve(original: Callable[..., Any], module_name: str) -> Callable[..., Any]:
    def _launch_resolve(*args: Any, **kwargs: Any) -> Any:
        if _bridge_installed():
            logger.info("in-process bridge active; skipping Resolve auto-launch "
                        "(%s)", module_name)
            return False
        return original(*args, **kwargs)

    _launch_resolve.__name__ = getattr(original, "__name__", "_launch_resolve")
    _launch_resolve.__doc__ = (
        "Launch DaVinci Resolve and wait for it to become available.\n\n"
        "Wrapped by free_edition.integrate.disable_autolaunch(): returns False "
        "without launching anything while the in-process bridge is installed, "
        "because there is nothing to launch from inside Resolve.")
    _launch_resolve.__wrapped__ = original
    setattr(_launch_resolve, _OURS, True)
    return _launch_resolve


# ---------------------------------------------------------------------------
# FUNCTION 1b: fix_upstream_project_bugs
# ---------------------------------------------------------------------------

def fix_upstream_project_bugs() -> List[str]:
    """Repair two `src/granular/project.py` tools that raise on every call.

    Both are unconditional failures in pristine upstream, verified by reading
    the pristine source rather than inferred from a traceback:

    `close_project` reads a name that does not exist. Its body binds
    `pm, current_project = get_current_project()` and then calls
    `project_manager.CloseProject(...)`. `project_manager` is neither a local
    nor a module global -- the other two uses of that name in the file are
    unrelated locals inside `open_project` and `create_project`. Every call
    raises `NameError`, and the function's own broad `except Exception` catches
    it and returns "Error closing project: name 'project_manager' is not
    defined". It has never closed a project; it only ever looked like a
    connection failure.

    `load_cloud_project_tool` is a name collision. It calls the bare global
    `load_cloud_project`, meaning to reach the `src.utils.cloud_operations`
    helper that `from src.granular.common import *` puts in the namespace at
    line 3. But a separate `@mcp.tool()` later in the same file is *also* named
    `load_cloud_project`, and module-level names bind in source order, so by
    import time the global points at that later definition -- whose signature
    has no leading `resolve_obj`. The call passes `get_resolve()` into its
    `project_name` slot while also passing `project_name=` by keyword:
    `TypeError: got multiple values for argument 'project_name'`, every time.

    Both wrappers replace the whole function rather than patching a line,
    because the fix is in *which object a name resolves to*, and there is no
    smaller unit than the call to change.

    Returns the tool names now carrying a fix. A caller expecting two and
    seeing fewer has found a regression -- most likely an upstream merge that
    renamed one of them.
    """
    project = sys.modules.get("src.granular.project")
    if project is None:
        logger.debug("free-edition: src.granular.project not imported; no bugfixes")
        return []

    missing = [name for name in PROJECT_BUGFIX_TOOLS
               if not callable(getattr(project, name, None))]
    if missing:
        raise UpstreamAnchorMissing(
            "src.granular.project no longer defines "
            + ", ".join(missing)
            + " -- upstream may have renamed or fixed these; re-check "
              "free_edition/docs/upstream-bugs.md before dropping the patch")

    fixed: List[str] = []
    for name, build in (("close_project", _fixed_close_project),
                        ("load_cloud_project_tool", _fixed_load_cloud_project_tool)):
        original = getattr(project, name)
        if getattr(original, _OURS, False):
            fixed.append(name)  # re-paste, not a second wrap
            continue
        setattr(project, name, build(project, original))
        fixed.append(name)

    setattr(project, PROJECT_BUGFIX_SENTINEL, True)
    _rewrap_registered_tool(project, fixed)
    logger.info("free-edition: upstream project.py bugfixes installed for %s",
                ", ".join(fixed) or "nothing")
    return fixed


def _fixed_close_project(project: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    def close_project() -> str:
        resolve = project.get_resolve()
        if resolve is None:
            return "Error: Not connected to DaVinci Resolve"
        pm, current_project = project.get_current_project()
        if not current_project:
            return "Error: No project currently open"
        project_name = current_project.GetName()
        try:
            # `pm`, not the undefined `project_manager` upstream reaches for.
            result = pm.CloseProject(current_project)
        except Exception as exc:  # noqa: BLE001 -- mirrors upstream's own contract
            project.logger.error("Error closing project: %s", exc)
            return f"Error closing project: {exc}"
        if result:
            project.logger.info("Project '%s' closed successfully", project_name)
            return f"Successfully closed project '{project_name}'"
        project.logger.error("Failed to close project '%s'", project_name)
        return f"Failed to close project '{project_name}'"

    close_project.__doc__ = original.__doc__
    close_project.__wrapped__ = original
    setattr(close_project, _OURS, True)
    return close_project


def _fixed_load_cloud_project_tool(project: Any,
                                   original: Callable[..., Any]) -> Callable[..., Any]:
    def load_cloud_project_tool(project_name: Optional[str] = None,
                                project_media_path: Optional[str] = None,
                                sync_mode: Optional[str] = None) -> Dict[str, Any]:
        # Imported here by its real home, so the shadowing global in
        # src/granular/project.py cannot intercept it.
        from src.utils.cloud_operations import load_cloud_project as helper
        return helper(project.get_resolve(), project_name=project_name,
                      project_media_path=project_media_path, sync_mode=sync_mode)

    load_cloud_project_tool.__doc__ = original.__doc__
    load_cloud_project_tool.__wrapped__ = original
    setattr(load_cloud_project_tool, _OURS, True)
    return load_cloud_project_tool


def _rewrap_registered_tool(project: Any, names: Iterable[str]) -> None:
    """Point the FastMCP registry at the repaired callables.

    Rebinding the module attribute is not enough on its own: `@mcp.tool()` ran
    at import time and the registry holds a reference to the ORIGINAL function
    object, so an MCP client would keep calling the broken one. Same reason
    `register_subtitle_tools` has to touch the registry rather than the module.

    Replacing `tool.fn` also discards whatever `dispatch.install()` had wrapped
    around the broken original, so this has to hand the repaired callables back
    to dispatch -- otherwise they run on the asyncio event-loop thread and can
    enter the single-threaded Resolve bridge concurrently with another tool.
    That is the same silent hang `_rewrap_for_dispatch` exists to prevent, and
    the detection here is deliberately the same shape.
    """
    common = sys.modules.get("src.granular.common")
    granular_mcp = getattr(common, "mcp", None) if common else None
    registry = _tool_registry(granular_mcp) if granular_mcp is not None else {}

    replaced = []
    for name in names:
        tool = registry.get(name)
        if tool is None:
            continue
        tool.fn = getattr(project, name)
        # Clear the mark so a dispatch pass will consider it again; dispatch
        # skips anything already flagged `is_async`.
        tool.is_async = False
        replaced.append(name)

    if not replaced:
        return

    try:
        from free_edition.inproc import dispatch

        wrapper_code_name = dispatch._offloaded(lambda: None).__code__.co_name
    except Exception:  # noqa: BLE001 - detection must not break the bugfix
        logger.warning("free-edition: cannot tell whether dispatch.install() has "
                       "run; project.py bugfixes are installed either way",
                       exc_info=True)
        return

    already = any(
        getattr(getattr(getattr(tool, "fn", None), "__code__", None),
                "co_name", None) == wrapper_code_name
        for tool_name, tool in registry.items() if tool_name not in replaced)
    if already:
        logger.info("free-edition: re-running dispatch.install() so the repaired "
                    "%s stay off the event-loop thread", ", ".join(replaced))
        dispatch.install(granular_mcp)


# ---------------------------------------------------------------------------
# FUNCTION 2: register_whisperx
# ---------------------------------------------------------------------------

def register_whisperx() -> Dict[str, Any]:
    """Install the whisperX backend into upstream's media analysis, at runtime.

    Replaces all twelve edits the fork used to carry in
    `src/utils/media_analysis.py`. Four things happen, in this order:

    1. **Registry.** `TOOL_INSTALL["whisperx"]` gains the install plan.
       `install_plan_for()` reads that dict at call time, so a plain item
       assignment is enough and no wrapper is needed.

    2. **Capabilities.** `detect_capabilities` is wrapped to post-process its
       return value: a `tools["whisperx"]` entry, and `whisperx` INSERTED AT
       POSITION 0 of `transcription["backends"]`. Position 0, never append:
       `_transcribe` takes `backends[0]` when the caller names no backend, so
       appending would leave every analysis running on whisper_cli with no
       speaker labels and no error anywhere.

    3. **The four captured references.** See `DETECT_CAPABILITIES_CAPTORS`.

    4. **Dispatch.** `_transcribe` is replaced wholesale. There is no finer
       seam: the branches the fork used to edit live inside `_run_backend`, a
       closure defined inside `_transcribe`, unreachable from outside the
       module by any patch. The replacement delegates to the original for every
       backend but whisperX, and for whisperX replays upstream's gates, wall
       clock timeout and caps accounting around our backend.

    Speaker labels are deliberately NOT patched here: upstream's normalizers
    drop them, and `free_edition/subtitles/whisperx.py` puts them back after
    calling the normalizer rather than monkeypatching two upstream parsers. See
    `_reattach_speaker_labels` there for why.

    Must run after `src.utils.media_analysis` is importable, and before
    `register_subtitle_tools()` or any call to `detect_capabilities()` /
    `_transcribe()`.

    Returns `{"tool_install", "detect_capabilities", "transcribe", "rebound",
    "rebound_attrs", "extra_rebound"}`. `rebound` is the list of captor modules
    actually patched; assert it against the captors present in `sys.modules`.
    Raises `UpstreamAnchorMissing` if upstream no longer has what the hooks need
    -- loudly, at install time, rather than degrading at the first transcription.
    """
    media_analysis = importlib.import_module("src.utils.media_analysis")
    from free_edition.subtitles import whisperx as fe_whisperx

    missing = [name for name in _MEDIA_ANALYSIS_ANCHORS
               if not hasattr(media_analysis, name)]
    if missing:
        raise UpstreamAnchorMissing(
            "src.utils.media_analysis is missing "
            + ", ".join(missing)
            + " -- free_edition.integrate.register_whisperx() cannot install the "
              "whisperX hooks against this version of upstream")

    cached = getattr(media_analysis, WHISPERX_SENTINEL, None)
    if (isinstance(cached, dict)
            and getattr(media_analysis.detect_capabilities, _OURS, False)
            and getattr(media_analysis._transcribe, _OURS, False)):
        logger.debug("free-edition: whisperX already registered, skipping")
        return cached

    report: Dict[str, Any] = {
        "tool_install": False,
        "detect_capabilities": False,
        "transcribe": False,
        "rebound": [],
        "rebound_attrs": {},
        "extra_rebound": {},
    }

    # (1) registry
    media_analysis.TOOL_INSTALL["whisperx"] = fe_whisperx.TOOL_INSTALL_ENTRY
    report["tool_install"] = "whisperx" in media_analysis.TOOL_INSTALL

    # (2) capabilities
    original_detect = media_analysis.detect_capabilities
    if getattr(original_detect, _OURS, False):
        # Sentinel was lost but the wrapper survived. Wrapping it again would
        # advertise whisperx twice and add a frame per re-paste.
        detect_wrapper = original_detect
        original_detect = getattr(original_detect, "__wrapped__", original_detect)
    else:
        detect_wrapper = _whisperx_capabilities(original_detect, fe_whisperx,
                                                media_analysis)
        media_analysis.detect_capabilities = detect_wrapper
    report["detect_capabilities"] = (
        media_analysis.detect_capabilities is detect_wrapper)

    # (3) the captured references
    rebound_attrs, extra = _rebind_detect_capabilities(original_detect, detect_wrapper)
    report["rebound_attrs"] = rebound_attrs
    report["rebound"] = sorted(rebound_attrs)
    report["extra_rebound"] = extra

    # (4) dispatch
    original_transcribe = media_analysis._transcribe
    if not getattr(original_transcribe, _OURS, False):
        media_analysis._transcribe = _whisperx_dispatch(
            original_transcribe, fe_whisperx, media_analysis)
    report["transcribe"] = getattr(media_analysis._transcribe, _OURS, False)

    setattr(media_analysis, WHISPERX_SENTINEL, report)
    logger.info(
        "free-edition: whisperX registered (tool_install=%s capabilities=%s "
        "transcribe=%s rebound=%s)",
        report["tool_install"], report["detect_capabilities"],
        report["transcribe"], report["rebound"])
    if extra:
        logger.warning(
            "free-edition: detect_capabilities was also captured by %s, which is "
            "not in DETECT_CAPABILITIES_CAPTORS -- rebound anyway; add it to the "
            "list and to free_edition/tests/test_whisperx_backend.py", extra)
    return report


def _whisperx_capabilities(original: Callable[..., Any], fe_whisperx: Any,
                           media_analysis: Any) -> Callable[..., Any]:
    """Wrap `detect_capabilities` so its report knows about whisperX."""

    def detect_capabilities(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        capabilities = original(env)
        try:
            _add_whisperx_capability(capabilities, env, fe_whisperx, media_analysis)
        except Exception:  # noqa: BLE001 - detection must never break the report
            # Logged rather than raised: a broken probe that returns upstream's
            # report costs diarization, a raised one costs every analysis tool.
            logger.exception(
                "free-edition: whisperX capability probe failed; the report is "
                "upstream's, so whisperX will look unavailable")
        return capabilities

    detect_capabilities.__doc__ = (
        "Detect available analysis helpers without installing or downloading.\n\n"
        "Wrapped by free_edition.integrate.register_whisperx(): adds the "
        "whisperX tool entry and puts `whisperx` at the FRONT of "
        "transcription.backends, which is what makes it the implicit default.")
    detect_capabilities.__wrapped__ = original
    setattr(detect_capabilities, _OURS, True)
    return detect_capabilities


def _add_whisperx_capability(capabilities: Dict[str, Any],
                             env: Optional[Dict[str, str]],
                             fe_whisperx: Any, media_analysis: Any) -> None:
    """Mutate a capability report in place to describe whisperX.

    Mirrors what the deleted edits produced, including key order, so a caller
    comparing reports across the refactor sees no difference.
    """
    environ = env if env is not None else os.environ
    executable = fe_whisperx._resolve_whisperx_executable(
        {"executable": environ.get("WHISPERX_BIN")})

    tools = capabilities.setdefault("tools", {})
    entry: Dict[str, Any] = {"available": bool(executable), "path": executable}
    if not executable:
        # Upstream's `_tool_entry` is a closure inside detect_capabilities and
        # cannot be reached; `install_plan_for` is module level, so the plan is
        # built the same way from the same data.
        entry["install"] = media_analysis.install_plan_for(
            "whisperx", platform_id=(capabilities.get("platform") or {}).get("id"))
    tools["whisperx"] = entry

    transcription = capabilities.setdefault("transcription", {})
    transcription["available"] = bool(transcription.get("available")) or bool(executable)
    backends = transcription.setdefault("backends", [])
    if executable and isinstance(backends, list) and "whisperx" not in backends:
        # INSERT AT 0, never append. `_transcribe` takes backends[0] when the
        # caller names no backend; whisperX leads because it is the only backend
        # that produces forced-aligned word timings and speaker labels, which is
        # the entire point of the subtitle feature.
        backends.insert(0, "whisperx")


def _rebind_detect_capabilities(original: Any, wrapper: Any
                                ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Point every module-level capture of `detect_capabilities` at the wrapper.

    Returns `({captor module: [attribute names]}, {unexpected module: [names]})`.

    Two passes on purpose. The first walks `DETECT_CAPABILITIES_CAPTORS`, the
    audited list the boot asserts a count against. The second sweeps the rest of
    the loaded `src.*` / `free_edition.*` modules for anything else still holding
    the original, so an upstream merge that adds a fifth captor is repaired at
    runtime and reported loudly instead of silently reporting whisperX absent.
    The static equivalent of that sweep lives in
    `free_edition/tests/test_whisperx_backend.py`, which fails if the audited
    list goes stale.
    """
    rebound: Dict[str, List[str]] = {}
    for name in DETECT_CAPABILITIES_CAPTORS:
        module = sys.modules.get(name)
        if module is None:
            logger.debug("free-edition: captor %s not imported yet; a later "
                         "import binds the wrapper on its own", name)
            continue
        # Both objects match: on a re-registration the captures already point at
        # the wrapper, and reporting that as "nothing rebound" would look like
        # the failure this list exists to prevent.
        names = _rebind_by_identity(module, (original, wrapper), wrapper)
        if not names:
            continue
        if not any(name.startswith("detect_capabilities") for name in names):
            # The module captured it under an alias only (src/server.py does).
            # Bind the canonical name too so the documented one-liner --
            # `sys.modules[n].detect_capabilities is ma.detect_capabilities` --
            # answers truthfully, and so a merge that adds a plain import here
            # is already covered.
            setattr(module, "detect_capabilities", wrapper)
            names = names + ["detect_capabilities (added)"]
        rebound[name] = names

    extra: Dict[str, List[str]] = {}
    for name, module in list(sys.modules.items()):
        if name in rebound or name in DETECT_CAPABILITIES_CAPTORS:
            continue
        if not (name.startswith(("src.", "free_edition."))
                or name in ("src", "free_edition")):
            continue
        # Only the original here: matching the wrapper too would report every
        # module that already holds it -- starting with media_analysis itself --
        # as an unexpected captor.
        names = _rebind_by_identity(module, (original,), wrapper)
        if names:
            extra[name] = names
    return rebound, extra


def _whisperx_dispatch(original: Callable[..., Any], fe_whisperx: Any,
                       media_analysis: Any) -> Callable[..., Any]:
    """Replace `_transcribe` with one that also knows the whisperX backend.

    Wholesale replacement rather than a narrower patch because the branches
    involved live in `_run_backend`, a closure inside `_transcribe`. Both call
    sites resolve `_transcribe` at call time (`media_analysis.py:5569` as a
    module global, `src/analysis_dashboard.py` as a per-call local import), so
    rebinding the attribute is enough and no captor list is needed here.
    """

    def _transcribe(path: str, artifacts: Dict[str, Any], options: Dict[str, Any],
                    capabilities: Dict[str, Any]) -> Dict[str, Any]:
        transcription = (options or {}).get("transcription") or {}

        # Upstream checks `enabled` before resolving the backend, and returns a
        # specific skipped payload. Delegating keeps that payload upstream's
        # rather than a copy of it that can drift.
        if not media_analysis._coerce_bool(
                transcription.get("enabled"),
                default=media_analysis.DEFAULT_TRANSCRIPTION_ENABLED):
            return original(path, artifacts, options, capabilities)

        backend = transcription.get("backend")
        if not backend:
            backends = (capabilities or {}).get("transcription", {}).get("backends") or []
            backend = backends[0] if backends else None
        if backend != "whisperx":
            return original(path, artifacts, options, capabilities)

        return _run_whisperx(path, artifacts, options or {}, transcription,
                             fe_whisperx, media_analysis)

    _transcribe.__doc__ = (
        "Upstream's transcription dispatcher, plus the whisperX backend.\n\n"
        "Installed by free_edition.integrate.register_whisperx(). Every backend "
        "but whisperX is delegated to upstream unchanged.")
    _transcribe.__wrapped__ = original
    setattr(_transcribe, _OURS, True)
    return _transcribe


def _run_whisperx(path: str, artifacts: Dict[str, Any], options: Dict[str, Any],
                  transcription: Dict[str, Any], fe_whisperx: Any,
                  media_analysis: Any) -> Dict[str, Any]:
    """The whisperX branch of `_transcribe`, with upstream's guarantees intact.

    Everything around the backend call is upstream's own module-level
    machinery -- the pre-call caps refusal, the wall-clock timeout, the usage
    recording -- reached by attribute rather than copied, so a whisperX run is
    governed by exactly the same budget and timeout as a whisper_cli one.
    """
    backend = "whisperx"

    # Pre-call refusal. Transcription cost scales with audio duration; when the
    # caller injected a duration we can estimate it and refuse before spending.
    duration_seconds = 0
    try:
        duration_seconds = int(float(options.get("duration_seconds")
                                     or transcription.get("duration_seconds") or 0))
    except (TypeError, ValueError):
        duration_seconds = 0
    if duration_seconds > 0:
        estimated_tokens = (duration_seconds
                            * media_analysis.AVG_TRANSCRIPTION_TOKENS_PER_SECOND)
        refusal = media_analysis._check_caps_pre_call(
            project_root=options.get("project_root"),
            estimated_vision_tokens=estimated_tokens,
            clip_id=options.get("clip_id"),
            job_id=options.get("job_id"),
        )
        if refusal is not None:
            refusal["backend"] = backend
            return refusal

    caps = media_analysis._resolve_active_caps()
    timeout = caps.wall_clock_seconds_per_call
    started_at = time.time()

    def _run_backend() -> Dict[str, Any]:
        if not media_analysis._coerce_bool(
                transcription.get("allow_model_download"), default=False):
            # whisperX pulls three sets of weights, not one: the ASR model, the
            # per-language alignment model, and the diarization pipeline. This
            # gate covers all of them.
            return {
                "success": False,
                "status": "skipped",
                "backend": backend,
                "reason": ("Local transcription may download model files; set "
                           "allow_model_download=true explicitly to run it."),
            }
        return fe_whisperx._transcribe_with_whisperx(path, artifacts, transcription)

    try:
        result = media_analysis._analysis_caps.run_with_timeout(_run_backend, timeout)
    except media_analysis._analysis_caps.WallClockTimeout as exc:
        return {
            "success": False,
            "status": "wall_clock_timeout",
            "backend": backend,
            "reason": str(exc),
            "elapsed_ms": round((time.time() - started_at) * 1000),
        }

    try:
        if options.get("project_root"):
            media_analysis._record_caps_usage(
                project_root=options.get("project_root"),
                clip_id=options.get("clip_id"),
                job_id=options.get("job_id"),
                wall_clock_ms=round((time.time() - started_at) * 1000),
            )
    except Exception:  # noqa: BLE001 - accounting must never fail a transcript
        pass

    return result if result is not None else {"success": False, "backend": backend}


# ---------------------------------------------------------------------------
# FUNCTION 3: register_subtitle_tools
# ---------------------------------------------------------------------------

def register_subtitle_tools(granular_mcp: Any) -> int:
    """Register the two subtitle tools on the granular FastMCP instance.

    Replaces the single line `subtitles,` in `src/granular/__init__.py`.

    The mechanism is an import for its side effect: executing
    `free_edition/subtitles/tools.py` fires its two `@mcp.tool()` decorators
    against the instance that `from src.granular.common import *` put in its
    namespace -- the same instance upstream's own tool modules decorate.

    Must run AFTER `register_whisperx()` and after `from src.granular import
    mcp`, and BEFORE `dispatch.install(granular_mcp)`. If dispatch already ran,
    this re-runs it for the two stragglers rather than leaving them to execute
    on the event-loop thread; that is a repair, not the contract.

    Returns the number of tools added: 2 on a fresh boot, 0 when both were
    already registered. A 0 that comes with the tools MISSING is impossible --
    that raises `SubtitleToolsNotRegistered` instead, because a silent zero here
    is the single most likely way the whole subtitle feature disappears and
    nothing else in the boot notices (the selftest's tool count only asserts
    `> 0`).
    """
    return _register_subtitle_tools(granular_mcp)["added"]


def _register_subtitle_tools(granular_mcp: Any) -> Dict[str, Any]:
    """`register_subtitle_tools` with the full report, for `install_all`."""
    if granular_mcp is None:
        raise SubtitleToolsNotRegistered(
            "register_subtitle_tools() needs the granular FastMCP instance "
            "(`from src.granular import mcp as granular_mcp`), got None")

    tools = _tool_registry(granular_mcp)
    report: Dict[str, Any] = {
        "added": 0,
        "already_registered": False,
        "reimported": False,
        "rewrapped": 0,
        "tools": list(SUBTITLE_TOOLS),
    }

    missing = [name for name in SUBTITLE_TOOLS if name not in tools]
    if not missing:
        report["already_registered"] = True
        logger.debug("free-edition: subtitle tools already registered")
        return report

    before = set(tools)
    module_name = "free_edition.subtitles.tools"
    if module_name in sys.modules:
        # The module is loaded but its tools are not on THIS instance: the boot
        # purged and re-imported `src.granular`, giving a fresh FastMCP, while
        # our module survived. Importing it again would be a no-op and the two
        # tools would silently never appear, so force the decorators to re-run.
        del sys.modules[module_name]
        report["reimported"] = True
    importlib.import_module(module_name)

    # Re-read the registry rather than trusting the dict object to be the same
    # one: `add_tool` mutates it in place today, and this costs one attribute
    # lookup to stop depending on that.
    tools = _tool_registry(granular_mcp)
    report["added"] = len(set(tools) - before)

    still_missing = [name for name in SUBTITLE_TOOLS if name not in tools]
    if still_missing:
        raise SubtitleToolsNotRegistered(
            "importing " + module_name + " did not register "
            + ", ".join(still_missing)
            + " on this FastMCP instance -- the @mcp.tool() decorators landed "
              "somewhere else, which means `from src.granular.common import *` "
              "resolved to a different `mcp` than the one passed in "
              "(a stale src.granular in sys.modules is the usual cause)")

    report["rewrapped"] = _rewrap_for_dispatch(granular_mcp, tools)
    logger.info("free-edition: %d subtitle tools registered%s",
                report["added"],
                " (re-imported)" if report["reimported"] else "")
    return report


def _tool_registry(fastmcp: Any) -> Dict[str, Any]:
    """The FastMCP tool registry, or a named failure.

    Couples to `ToolManager._tools`, the same private attribute
    `free_edition/inproc/dispatch.py` walks (verified on mcp 1.28).
    """
    manager = getattr(fastmcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        raise SubtitleToolsNotRegistered(
            f"{fastmcp!r} has no _tool_manager._tools dict: the mcp SDK's "
            "internals changed, so free_edition cannot tell whether the "
            "subtitle tools registered")
    return tools


def _rewrap_for_dispatch(granular_mcp: Any, tools: Dict[str, Any]) -> int:
    """Repair the register-after-dispatch inversion. Returns tools wrapped.

    `dispatch.install()` iterates the registry once and marks what it wraps, so
    tools registered after it runs never get wrapped: they work, they appear in
    the tool list, and they execute on the asyncio event-loop thread where they
    can enter the single-threaded Resolve bridge concurrently with another tool.
    The symptom is an intermittent hang that never reproduces on demand.

    Detection has to be exact rather than "are any tools async": the compound
    server has natively-async tools that carry `is_async` without dispatch ever
    having run, and calling `install()` early there would wrap everything and
    leave the boot's own `dispatch.install()` reporting zero -- turning a repair
    into the failure the selftest is watching for. So this looks for our own
    wrapper's code object on some OTHER tool, which only dispatch can have put
    there.
    """
    ours = [tools[name] for name in SUBTITLE_TOOLS if name in tools]
    if all(getattr(tool, "is_async", False) for tool in ours):
        return 0

    try:
        from free_edition.inproc import dispatch

        wrapper_code_name = dispatch._offloaded(lambda: None).__code__.co_name
    except Exception:  # noqa: BLE001 - detection must not break registration
        logger.warning("free-edition: cannot tell whether dispatch.install() has "
                       "run; the subtitle tools are registered either way",
                       exc_info=True)
        return 0

    already = any(
        getattr(getattr(getattr(tool, "fn", None), "__code__", None),
                "co_name", None) == wrapper_code_name
        for name, tool in tools.items() if name not in SUBTITLE_TOOLS)
    if not already:
        return 0  # dispatch has not run yet; the boot wraps everything shortly

    logger.warning(
        "free-edition: dispatch.install() ran before register_subtitle_tools(); "
        "re-running it so %s do not execute on the event-loop thread",
        ", ".join(SUBTITLE_TOOLS))
    return dispatch.install(granular_mcp)


# ---------------------------------------------------------------------------
# FUNCTION 4: install_dashboard_card
# ---------------------------------------------------------------------------

def install_dashboard_card(dash: Any) -> Dict[str, Any]:
    """Add the in-process bridge card to upstream's analysis dashboard.

    Replaces all four hunks the fork used to carry in
    `src/analysis_dashboard.py`. Two patches on an already-imported module:
    `_mcp_status_payload` gains an `inproc` key, and the module-level `HTML`
    string gains a `<style>` and a `<script>`. Both are re-read per request --
    `Handler._html()` encodes `HTML` every time and the payload is called by
    bare global name -- so rebinding the attributes is enough.

    Call after `import src.analysis_dashboard as dash` and before the HTTP
    server starts serving. Idempotent, and it never raises: a missing
    diagnostics card is cosmetic and must not take the dashboard down, so
    failures are logged and reported in the returned dict instead.

    The implementation is `free_edition.dashboard.card.install()`; this is the
    name the boot scripts call.
    """
    from free_edition.dashboard import card

    return card.install(dash)


# ---------------------------------------------------------------------------
# FUNCTION 5: ensure_state_dir
# ---------------------------------------------------------------------------

def ensure_state_dir(repo_root: Optional[Any] = None) -> str:
    """Create `<repo>/.inproc/` and make git ignore it from the inside.

    Replaces the three lines the fork used to add to the repo-root `.gitignore`.

    A `.gitignore` holding a single `*` ignores every file in its own directory
    including itself, so git never reports the directory at all -- which is what
    lets upstream's `.gitignore` stay byte-identical. Same trick, same content
    as `free_edition/tools/setup_inproc.py`, so whichever runs first wins and
    the other rewrites nothing.

    `repo_root` defaults to this checkout, derived from `__file__` and then
    proved by looking for `src/granular`. An explicitly passed root is trusted
    as given, so a test can point it at a temporary directory.

    Returns the absolute path of the state directory. Costs one stat and one
    read on the happy path.
    """
    root = pathlib.Path(repo_root) if repo_root is not None else _repo_root()
    state = root / ".inproc"
    state.mkdir(parents=True, exist_ok=True)

    ignore = state / ".gitignore"
    wanted = "*\n"
    try:
        if ignore.read_text(encoding="utf-8") == wanted:
            return str(state)
    except OSError:
        pass
    ignore.write_text(wanted, encoding="utf-8")
    logger.debug("free-edition: wrote %s", ignore)
    return str(state)


# ---------------------------------------------------------------------------
# FUNCTION 6: install_all
# ---------------------------------------------------------------------------

def install_all(console_resolve: Any = None, granular_mcp: Any = None,
                dash: Any = None, repo_root: Optional[Any] = None,
                autolaunch_modules: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Run every registration in the documented order and report on all of them.

    One call for the boots and for `free_edition/tools/fake_console.py`, so the
    ordering contract lives here instead of being retyped in four places.

    Everything is optional because the three boots need different subsets: the
    granular boot passes `granular_mcp`, the compound boot passes neither it nor
    `dash` (the subtitle tools hang off the granular instance only), the
    dashboard boot passes `dash`.

    `console_resolve` is not used to install anything -- by the time this runs
    the shim MUST already be installed, because `src/granular/common.py` binds
    `resolve` at import time and a later shim cannot unbind the `None` it
    cached. Passing the handle asks this function to verify that, and to say so
    loudly when it is not true, rather than leaving a server that answers every
    tool call with "Not connected to DaVinci Resolve".

    Returns a report dict with an `ok` flag and a `problems` list. It does NOT
    call `dispatch.install()`: that is the boot's step, and it has to happen
    after this one.
    """
    report: Dict[str, Any] = {
        "state_dir": None,
        "shim_installed": None,
        "handle_identity": None,
        "autolaunch": [],
        "project_bugfixes": [],
        "whisperx": None,
        "subtitle_tools": None,
        "dashboard_card": None,
        "problems": [],
        "ok": False,
    }
    problems: List[str] = report["problems"]

    report["state_dir"] = ensure_state_dir(repo_root)

    if console_resolve is not None:
        report["shim_installed"] = _bridge_installed()
        if not report["shim_installed"]:
            problems.append(
                "the DaVinciResolveScript shim is not installed: upstream was "
                "imported before shim.install(), so it cached a dead handle")
        granular_common = sys.modules.get("src.granular.common")
        if granular_common is not None:
            same = getattr(granular_common, "resolve", None) is console_resolve
            report["handle_identity"] = same
            if not same:
                problems.append(
                    "src.granular.common.resolve is not the console handle")

    report["autolaunch"] = disable_autolaunch(*(autolaunch_modules or ()))
    expected = [name for name in (autolaunch_modules or AUTOLAUNCH_MODULES)
                if name in sys.modules]
    if sorted(report["autolaunch"]) != sorted(expected):
        problems.append(
            f"autolaunch guard landed in {report['autolaunch']}, expected "
            f"{expected} -- an imported module kept its unguarded "
            "_launch_resolve and can open a second Resolve")

    # Before register_subtitle_tools below, so that if either repair has to hand
    # the registry back to dispatch, it happens once rather than being undone.
    report["project_bugfixes"] = fix_upstream_project_bugs()
    if "src.granular.project" in sys.modules:
        expected_fixes = sorted(PROJECT_BUGFIX_TOOLS)
        if sorted(report["project_bugfixes"]) != expected_fixes:
            problems.append(
                f"project.py bugfixes landed for {report['project_bugfixes']}, "
                f"expected {expected_fixes} -- close_project and/or "
                "load_cloud_project_tool are still the broken upstream versions "
                "that raise on every call")

    report["whisperx"] = register_whisperx()
    captors_present = [name for name in DETECT_CAPABILITIES_CAPTORS
                       if name in sys.modules]
    rebound = report["whisperx"]["rebound"]
    if sorted(rebound) != sorted(captors_present):
        # The highest-severity silent failure in this layer: a captor left
        # holding the original reports whisperX absent and falls back to a
        # backend that cannot diarize, with no error anywhere.
        problems.append(
            f"detect_capabilities rebound in {rebound}, but {captors_present} "
            "are imported -- the missing ones still hold the original and will "
            "report whisperX as unavailable")

    if granular_mcp is not None:
        report["subtitle_tools"] = _register_subtitle_tools(granular_mcp)

    if dash is not None:
        card_report = install_dashboard_card(dash)
        report["dashboard_card"] = card_report
        problems.extend(f"dashboard card: {problem}"
                        for problem in card_report.get("problems") or [])

    report["ok"] = not problems
    if problems:
        for problem in problems:
            logger.error("free-edition integration: %s", problem)
    else:
        logger.info("free-edition integration complete: %s", _summary(report))
    return report


def _summary(report: Dict[str, Any]) -> str:
    """One line for the console, since the console is where this gets read."""
    whisperx = report.get("whisperx") or {}
    subtitles = report.get("subtitle_tools") or {}
    parts = [
        f"autolaunch guarded in {len(report.get('autolaunch') or [])} modules",
        f"detect_capabilities rebound in {len(whisperx.get('rebound') or [])}",
        f"_transcribe patched={bool(whisperx.get('transcribe'))}",
    ]
    if report.get("subtitle_tools") is not None:
        parts.append(f"subtitle tools +{subtitles.get('added', 0)}"
                     + (" (already registered)" if subtitles.get("already_registered")
                        else ""))
    if report.get("dashboard_card") is not None:
        card = report["dashboard_card"]
        parts.append("dashboard card already installed"
                     if card.get("already_installed")
                     else (f"dashboard card markup={card.get('markup')} "
                           f"payload={card.get('payload')}"))
    return "; ".join(parts)


__all__ = [
    "AUTOLAUNCH_MODULES",
    "DETECT_CAPABILITIES_CAPTORS",
    "IntegrationError",
    "SUBTITLE_TOOLS",
    "SubtitleToolsNotRegistered",
    "UpstreamAnchorMissing",
    "disable_autolaunch",
    "ensure_state_dir",
    "install_all",
    "install_dashboard_card",
    "register_subtitle_tools",
    "register_whisperx",
]
