"""Build the vendored dependency tree the console boot imports.

Run once from a normal shell, before the first console paste:

    python3 tools/setup_inproc.py

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
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS = os.path.join(REPO, ".inproc", "deps")
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
    print(f"target      : {args.dest}")

    if args.check:
        if not os.path.isdir(args.dest):
            print("not built yet")
            return 1
        entries = sorted(e for e in os.listdir(args.dest)
                         if not e.endswith(".dist-info") and e != "__pycache__")
        print(f"packages    : {len(entries)}")
        for entry in entries:
            print(f"  {entry}")
        return 0

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
