"""Find out whether a Resolve object can be identified and introspected.

Paste in Resolve's console (Workspace -> Console -> Py3):

    exec(open("/path/to/davinci-resolve-mcp/free_edition/tools/probe_api_identity.py").read())

The idea under test: shadow builtins.hasattr inside the repo's modules with a
version that answers truthfully, by looking the method up in the API table
Blackmagic ships in README.txt. That needs two things this probe measures.

  1. Can we tell a Project from a Timeline at runtime? The README indexes
     methods by object type, so a lookup needs the object's type.
  2. What does calling a method that does not exist actually do? If it raises,
     a probe-by-calling is possible for getters. If it silently returns
     something, every failure will be a confusing null instead of an error.

Calling a nonexistent method is safe by construction: there is nothing there to
run. Nothing else in this file touches project state.

ASCII only -- the console's open() defaults to US-ASCII.
"""

import sys

BOGUS = "NoExisteEsteMetodoXyz123"

resolve_handle = globals().get("resolve")
if resolve_handle is None:
    print("no `resolve` in the console namespace")
else:
    pm = resolve_handle.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    mp = project.GetMediaPool() if project else None
    timeline = project.GetCurrentTimeline() if project else None

    subjects = [
        ("Resolve", resolve_handle),
        ("ProjectManager", pm),
        ("Project", project),
        ("MediaPool", mp),
        ("Timeline", timeline),
    ]

    print("=" * 66)
    print("TYPE IDENTITY")
    print("=" * 66)
    print("  Can we distinguish object types at runtime?")
    print()
    for label, obj in subjects:
        if obj is None:
            print(f"  {label:16s} <none available>")
            continue
        print(f"  {label:16s} type={type(obj).__name__!r}")
        print(f"  {'':16s} repr={repr(obj)[:70]}")
        print(f"  {'':16s} str ={str(obj)[:70]}")

    print()
    print("=" * 66)
    print("INTROSPECTION")
    print("=" * 66)
    for label, obj in subjects:
        if obj is None:
            continue
        try:
            names = [n for n in dir(obj) if not n.startswith("_")]
        except Exception as exc:
            print(f"  {label:16s} dir() raised {type(exc).__name__}: {exc}")
            continue
        print(f"  {label:16s} dir() -> {len(names)} public names")
        if names:
            print(f"  {'':16s} {names[:12]}")

    print()
    print("=" * 66)
    print("MISSING METHOD BEHAVIOUR")
    print("=" * 66)
    print("  What the bridge does with a name that does not exist.")
    print()
    for label, obj in subjects:
        if obj is None:
            continue
        line = f"  {label:16s} hasattr={hasattr(obj, BOGUS)!r}"
        try:
            attr = getattr(obj, BOGUS)
            line += f"  getattr={type(attr).__name__}"
        except Exception as exc:
            print(line + f"  getattr raised {type(exc).__name__}")
            continue
        try:
            value = attr()
            line += f"  call -> {type(value).__name__}={str(value)[:28]!r}"
        except Exception as exc:
            line += f"  call raised {type(exc).__name__}: {str(exc)[:40]}"
        print(line)

    print()
    print("  For contrast, the same shape on a method that DOES exist:")
    try:
        value = project.GetName() if project else None
        print(f"  {'Project.GetName':16s} call -> {type(value).__name__}="
              f"{str(value)[:28]!r}")
    except Exception as exc:
        print(f"  {'Project.GetName':16s} raised {type(exc).__name__}: {exc}")

    print()
    print("=" * 66)
    print("STUDIO-GATED METHOD")
    print("=" * 66)
    print("  A method the README marks Studio-only. On the free edition this is")
    print("  the failure mode every such tool will hit.")
    print()
    for label, obj, method in (
        ("Project", project, "GetSetting"),
        ("MediaPool", mp, "AutoSyncAudio"),
        ("Timeline", timeline, "DetectSceneCuts"),
    ):
        if obj is None:
            continue
        try:
            attr = getattr(obj, method)
            print(f"  {label}.{method:18s} getattr ok, type="
                  f"{type(attr).__name__}  (not called)")
        except Exception as exc:
            print(f"  {label}.{method:18s} getattr raised "
                  f"{type(exc).__name__}: {exc}")

    print()
    print("probe done -- nothing was modified")
