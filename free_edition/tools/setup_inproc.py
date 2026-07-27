"""Build the vendored dependency tree the console boot imports.

Run once from a normal shell, before the first console paste:

    python3 free_edition/tools/setup_inproc.py

Resolve's console has an embedded interpreter: sys.executable points at the
Resolve binary, and the user site-packages directory is not on its sys.path, so
`pip install --user` would be invisible to it. Installing into a target
directory the boot puts on sys.path sidesteps both problems, needs no sudo, and
leaves the system framework untouched.

The wheels must match the console's ABI exactly, so the install runs under the
framework interpreter the console reports as its sys.prefix -- not whatever
python3 happens to be first on PATH.
"""

import argparse
import os
import pathlib
import subprocess
import sys


def _repo_root():
    """Locate the checkout root, then prove it rather than assume it.

    This file lives three levels down (free_edition/tools/setup_inproc.py), so
    the walk is parents[2]. Getting that count wrong is completely silent:
    os.path.join builds a plausible <repo>/free_edition/.inproc/deps, os.makedirs
    creates it, 53 MB lands in the wrong tree, and the mistake only surfaces
    minutes later and one layer away as ModuleNotFoundError inside Resolve's
    console. The marker check turns that into an immediate, named failure.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    if not (root / "src" / "granular").is_dir():
        raise SystemExit(
            f"cannot locate the checkout root: walked up from {__file__} to "
            f"{root}, which has no src/granular in it. Did this file move?")
    return str(root)


REPO = _repo_root()
# Runtime state: vendored deps, logs, the bearer token, the transport handoff.
# It stays at the CHECKOUT ROOT. Nothing about .inproc/ moved into free_edition/
# -- the boot, the probes and the dashboard all read <repo>/.inproc.
STATE = os.path.join(REPO, ".inproc")
DEPS = os.path.join(STATE, "deps")
REQUIREMENTS = ["mcp[cli]"]

# sys.prefix as reported by Resolve 21's console on macOS.
DEFAULT_INTERPRETER = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"


def _interpreter_report(python):
    code = ("import sys, platform;"
            "print(sys.version.split()[0]);"
            "print(sys.prefix);"
            "print(platform.machine())")
    out = subprocess.run([python, "-c", code], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"cannot run {python}:\n{out.stderr.strip()}")
    version, prefix, machine = out.stdout.strip().splitlines()
    return version, prefix, machine


def ensure_state_dir(state=STATE):
    """Create the state directory and make git ignore it, from the inside.

    A `.gitignore` holding a single `*` ignores every file in the directory
    including itself, so git never reports the directory at all and the
    repo-root .gitignore needs no entry for it -- which is what lets that file
    stay byte-identical to upstream. Rewritten only when the content differs, so
    repeat runs do not churn the mtime.
    """
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, ".gitignore")
    wanted = "*\n"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == wanted:
                return path, False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(wanted)
    return path, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=DEFAULT_INTERPRETER,
                        help="interpreter matching the console's sys.prefix")
    parser.add_argument("--dest", default=DEPS, help="where to vendor the wheels")
    parser.add_argument("--check", action="store_true",
                        help="report what is installed and exit")
    args = parser.parse_args()

    version, prefix, machine = _interpreter_report(args.python)
    print(f"interpreter : {args.python}")
    print(f"  version   : {version}")
    print(f"  prefix    : {prefix}")
    print(f"  machine   : {machine}")
    print(f"repo        : {REPO}")
    print(f"state dir   : {STATE}")
    print(f"target      : {args.dest}")

    if args.check:
        ignore = os.path.join(STATE, ".gitignore")
        try:
            with open(ignore, encoding="utf-8") as fh:
                ignored = fh.read() == "*\n"
        except OSError:
            ignored = False
        print(f"self-ignored: {ignored}"
              + ("" if ignored else f"  (missing or stale {ignore})"))
        if not os.path.isdir(args.dest):
            print("not built yet")
            return 1
        entries = sorted(e for e in os.listdir(args.dest)
                         if not e.endswith(".dist-info") and e != "__pycache__")
        print(f"packages    : {len(entries)}")
        for entry in entries:
            print(f"  {entry}")
        return 0

    ignore, written = ensure_state_dir()
    print(f"self-ignore : {ignore} ({'written' if written else 'already correct'})")

    os.makedirs(os.path.dirname(args.dest), exist_ok=True)
    cmd = [args.python, "-m", "pip", "install", "--upgrade",
           "--target", args.dest] + REQUIREMENTS
    print()
    print("running:", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode

    natives = []
    for root, _dirs, files in os.walk(args.dest):
        for name in files:
            if name.endswith(".so"):
                natives.append(os.path.relpath(os.path.join(root, name), args.dest))
    print()
    print(f"native extensions: {len(natives)}")
    for native in sorted(natives):
        print(f"  {native}")
    print()
    print("Those filenames carry the ABI tag. They must say cpython-<console")
    print("python version>-darwin, or the console will refuse to import them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
