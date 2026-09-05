"""Static guard: no undefined names anywhere under free_edition/.

Upstream's `tests/test_static_undefined_names.py` scans `src/` and only `src/`,
and it stays that way -- it is an upstream file this fork keeps byte-identical.
So the moment our layer moved out of `src/`, it lost that coverage entirely.
This is the same guard pointed at our tree.

The bug class it keeps extinct does not show up in a unit test: an undefined
name inside a guard or a try/except does not crash, it silently falls back to a
default. Three shipped bugs were this class -- the confirm-token gate calling a
misspelled preference reader (v2.37.0), the update-channel resource reporting
"stable" unconditionally, and the auto-run idle-timeout preference being
ignored.

It earns its place here more than upstream, not less. `free_edition/subtitles/
tools.py` does `from src.granular.common import *`, and under a star import
pyflakes cannot distinguish an undefined name from an imported one at all; and
the three boot scripts read console-injected values through `globals().get(...)`
rather than as bare names, which is exactly the sort of thing that decays into a
bare name during a refactor and then fails only inside Resolve, where the
traceback goes to a console nobody is watching.

Skips when pyflakes is not installed (it is a dev dependency, not a runtime
one): `pip install pyflakes`.
"""
from __future__ import annotations

import io
import pathlib
import unittest

try:
    from pyflakes.api import checkPath
    from pyflakes.reporter import Reporter
    HAVE_PYFLAKES = True
except ImportError:
    HAVE_PYFLAKES = False

TREE = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAVE_PYFLAKES, "pyflakes not installed")
class UndefinedNamesTest(unittest.TestCase):
    def test_no_undefined_names_in_free_edition(self):
        sources = sorted(TREE.rglob("*.py"))
        # A guard that scans nothing passes, which is the failure mode this
        # whole file exists to prevent, one level up.
        self.assertTrue(sources, f"no Python files under {TREE}")

        out = io.StringIO()
        reporter = Reporter(out, out)
        for path in sources:
            checkPath(str(path), reporter)
        undefined = [
            line
            for line in out.getvalue().splitlines()
            if "undefined name" in line and "unable to detect undefined names" not in line
        ]
        self.assertEqual(undefined, [], "undefined names found:\n" + "\n".join(undefined))


if __name__ == "__main__":
    unittest.main()
