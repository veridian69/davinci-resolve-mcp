"""Make UTF-8 the default text encoding inside Resolve's console interpreter.

Resolve's embedded interpreter runs under a C locale, so `locale.getencoding()`
reports US-ASCII and every `open()` without an explicit encoding inherits it.
Reading any UTF-8 file with accented content then raises UnicodeDecodeError --
including this project's own sources, JSON config, and log files.

Paths are unaffected: `sys.getfilesystemencoding()` is already utf-8, so a clip
named "anejo.mov" was never the problem. File *contents* were.

This patches `builtins.open` process-wide, which every later script run in the
same console inherits. That is a deliberate trade: the alternative is auditing
and fixing every `open()` call site in the repo.
"""

import builtins
import locale
import logging

logger = logging.getLogger("davinci-resolve-mcp.inproc.encoding")

_STASH = "__inproc_original_open__"


def _original_open():
    """The real builtins.open, surviving repeated installs without nesting."""
    return getattr(builtins, _STASH, None) or builtins.open


def install():
    """Default text-mode open() to UTF-8. Returns a report of what changed.

    Idempotent: re-running wraps the original open(), never a previous wrapper.
    """
    before = {
        "locale_getencoding": locale.getencoding(),
        "locale_getpreferredencoding": locale.getpreferredencoding(False),
        "filesystem_encoding": __import__("sys").getfilesystemencoding(),
    }

    real_open = _original_open()
    setattr(builtins, _STASH, real_open)

    def utf8_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                  newline=None, closefd=True, opener=None):
        """open() with a UTF-8 default; binary mode passes through untouched."""
        if "b" not in mode and encoding is None:
            encoding = "utf-8"
        return real_open(file, mode, buffering, encoding, errors, newline,
                         closefd, opener)

    builtins.open = utf8_open
    locale.getpreferredencoding = lambda do_setlocale=True: "utf-8"

    logger.info("default text encoding forced to utf-8 (was %s)",
                before["locale_getencoding"])
    return before


def verify(scratch_path):
    """Round-trip non-ASCII through a bare open() to prove the patch holds.

    Raises whatever open() raises, so a caller can report the real failure
    rather than a boolean.
    """
    import os

    sample = "acentos y enie: ñ á é — ok"
    with open(scratch_path, "w") as handle:
        handle.write(sample)
    try:
        with open(scratch_path) as handle:
            round_tripped = handle.read()
    finally:
        try:
            os.remove(scratch_path)
        except OSError:
            pass
    if round_tripped != sample:
        raise ValueError(f"utf-8 round trip altered the text: {round_tripped!r}")
    return round_tripped
