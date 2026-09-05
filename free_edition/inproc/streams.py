"""Make stdout/stderr safe to write from a background thread inside Resolve.

Resolve's console replaces sys.stdout and sys.stderr with the Fusion modules
`fu_stdout` and `fu_stderr`. Those write through a PyCapsule bound to the
console's own thread, so calling write() from any other thread raises:

    ValueError: PyCapsule_GetPointer called with invalid PyCapsule object
    SystemError: <built-in function write> returned a result with an exception set

The failure is worse than it looks. logging's emit() catches the error and calls
handleError(), which writes to sys.stderr -- raising again and discarding the
original traceback. A server thread that dies this way reports nothing at all.

The fix routes console-thread writes to the console and everything else to a
file, so background output survives and never touches the capsule.
"""

import logging
import threading

logger = logging.getLogger("davinci-resolve-mcp.inproc.streams")

_STASH = "__inproc_original_streams__"


class ConsoleSafeStream:
    """Console output on the console's thread; file output everywhere else."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, console_stream, sink, console_thread_id):
        self._console = console_stream
        self._sink = sink
        self._console_thread_id = console_thread_id
        self._lock = threading.Lock()

    def write(self, text):
        if threading.get_ident() == self._console_thread_id:
            try:
                self._console.write(text)
            except Exception:
                # Never let a console write failure propagate into logging,
                # which would trigger the handleError cascade described above.
                pass
        with self._lock:
            self._sink.write(text)
            self._sink.flush()
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        with self._lock:
            self._sink.flush()

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    @property
    def closed(self):
        return False


def _retarget_stray_handlers(safe_stream):
    """Point every StreamHandler at a stream that survives a background thread.

    logging's state is global and outlives any sys.modules purge, so a handler
    built while sys.stderr was still Fusion's raw stream keeps that stream
    forever. Every record emitted from the server thread then raises SystemError
    inside emit(), and logging's own error path writes to stderr and raises
    again -- which is how a real traceback gets destroyed.

    Handlers we cannot control are the norm here, not the exception: the repo
    calls logging.basicConfig() at import, and a console session accumulates
    handlers from every script that ran before this one.
    """
    retargeted = []
    loggers = [logging.getLogger()]
    loggers.extend(
        obj for obj in logging.Logger.manager.loggerDict.values()
        if isinstance(obj, logging.Logger))

    for log in loggers:
        for handler in list(getattr(log, "handlers", [])):
            # FileHandler subclasses StreamHandler but owns a real file; it is
            # thread-safe here and must keep its own stream.
            if not isinstance(handler, logging.StreamHandler):
                continue
            if isinstance(handler, logging.FileHandler):
                continue
            if getattr(handler, "stream", None) is safe_stream:
                continue
            try:
                handler.setStream(safe_stream)
                retargeted.append(f"{log.name}:{type(handler).__name__}")
            except Exception:
                # Better a silent handler than one that raises on every record.
                try:
                    log.removeHandler(handler)
                    retargeted.append(f"{log.name}:{type(handler).__name__} (removed)")
                except Exception:
                    pass
    return retargeted


def install(log_path, namespace):
    """Wrap sys.stdout/sys.stderr and add a file handler to the root logger.

    `namespace` is the console's globals(), used to stash the original streams
    so repeated installs re-wrap the originals instead of nesting wrappers.
    Must be called from the console's own thread -- that thread's identity is
    what the wrapper uses to decide where a write goes.
    """
    import sys

    original = namespace.get(_STASH)
    if original:
        sys.stdout, sys.stderr = original
    namespace[_STASH] = (sys.stdout, sys.stderr)

    console_thread_id = threading.get_ident()
    sink = open(log_path, "a", encoding="utf-8", errors="replace")
    sys.stdout = ConsoleSafeStream(sys.stdout, sink, console_thread_id)
    sys.stderr = ConsoleSafeStream(sys.stderr, sink, console_thread_id)

    retargeted = _retarget_stray_handlers(sys.stdout)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_inproc", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    file_handler._inproc = True
    root.addHandler(file_handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

    return {"log_path": log_path,
            "console_thread_id": console_thread_id,
            "retargeted_handlers": retargeted}
