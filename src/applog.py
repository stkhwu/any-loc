#!/usr/bin/env python3
"""
applog.py — logging + standard-stream foundation for any-loc.

This module exists because of one brutal fact about a *windowed* (console=False)
frozen build on Windows: **there are no standard streams at all**. PyInstaller
gives the process no console, so `sys.stdout`, `sys.stderr` AND `sys.stdin` are
all literally `None`.

Two consequences drive everything here:

  1. `print()` and `logging` do NOT crash with `sys.stdout is None` — the output
     is silently DISCARDED. A windowed build with no file log is undiagnosable:
     the app either works or vanishes, and you get nothing either way. Hence
     `setup_logging()` always writes a rotating file log under `log_dir()`.

  2. Some libraries *probe* the streams instead of just writing to them, and
     those probes explode on `None`. The one that actually kills us:
     uvicorn's default `log_config` builds a `ColourizedFormatter`, whose
     `__init__` calls `sys.stdout.isatty()` when `use_colors` is None
     (uvicorn/logging.py). With `sys.stdout = None` that's an AttributeError,
     which `logging.config.dictConfig` re-raises as
     `ValueError: Unable to configure formatter 'default'` — tunneld never
     starts. `install_null_streams()` is the fix: a stream object that answers
     every probe truthfully-but-harmlessly and throws writes away.
     (Belt and braces: the tunnel code should ALSO pass `log_config=None`.)

The third piece is `RingBufferHandler`, an in-memory ring of recent log records.
The file log is for post-mortems; the ring is what an in-app "show me the log"
panel reads, so the user can see what went wrong without hunting for a file.

Nothing in here may ever raise during startup. Logging setup that can kill the
app it is supposed to diagnose is worse than no logging at all — so every
filesystem touch is guarded and falls back rather than propagating.
"""
from __future__ import annotations

import io
import itertools
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Any

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# Ring size / file-log rotation. 500 records is a few hundred KB at most and
# comfortably covers a whole session's interesting events.
RING_CAPACITY = 500
LOG_MAX_BYTES = 1024 * 1024        # 1 MB per file …
LOG_BACKUP_COUNT = 3               # … times 4 files max on disk.

# Loggers that are chatty enough to bury our own messages. Same list the old
# launcher.main() used; keep them in sync if you add a dependency.
NOISY_LOGGERS = (
    "pymobiledevice3",
    "urllib3",
    "asyncio",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)

_CONSOLE_FMT = "%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
_CONSOLE_DATEFMT = "%H:%M:%S"
# The file log outlives a single session, so it needs the date too.
_FILE_FMT = "%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"


# =============================================================================
# Null streams — what stands in for sys.stdout/stderr/stdin when there is no
# console at all (windowed frozen build).
# =============================================================================
class _NullStream(io.TextIOBase):
    """
    A text stream that swallows everything written to it.

    It is deliberately *not* a bare object: libraries do not only call
    `write()`, they interrogate the stream first. This class answers all of the
    probes we have actually seen in the wild:

      * `isatty()`  -> False   (uvicorn's ColourizedFormatter — see module doc;
                                also click/colorama style detection)
      * `encoding`  -> "utf-8" (anything that re-encodes before writing)
      * `errors`    -> "replace"
      * `fileno()`  -> raises io.UnsupportedOperation, exactly like a real
                       non-file stream. Callers that want a real fd already
                       have to handle this; returning a fake number would send
                       writes to a random descriptor instead.
      * `write()`   -> returns len(s), because callers may check the count.

    Subclassing io.TextIOBase (rather than faking it) means the dozens of
    attributes nobody thought to test still exist with sane defaults.
    """

    # -- identity probes ------------------------------------------------------
    def isatty(self) -> bool:
        # THE important one. If this ever returns True, uvicorn's colourized
        # formatter will emit ANSI escapes into a stream that goes nowhere, and
        # worse, code that trusts isatty() may try interactive behaviour.
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    @property
    def name(self) -> str:
        return "<null>"

    @property
    def mode(self) -> str:
        return "w"

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    # -- capabilities ---------------------------------------------------------
    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    # -- the actual (non-)work ------------------------------------------------
    def write(self, s: Any) -> int:
        # Report the full length: callers that loop "until everything is
        # written" must not spin forever.
        try:
            return len(s)
        except TypeError:
            return 0

    def writelines(self, lines: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    # `readable()` is False, so a strict stream would raise here. We return EOF
    # instead: a caller reading from a stdin that does not exist wants "there is
    # nothing", not a crash in a windowed app with no way to show the traceback.
    def read(self, size: Any = -1) -> str:
        return ""

    def readline(self, size: Any = -1) -> str:
        return ""

    def readlines(self, hint: Any = -1) -> list:
        return []

    def close(self) -> None:
        # Intentionally a no-op. If some library's atexit handler closes
        # sys.stdout, every later write would raise ValueError("I/O operation
        # on closed file") — during interpreter shutdown, where it is least
        # debuggable. A stream that discards everything has nothing to close.
        return None


def install_null_streams() -> None:
    """
    Make sys.stdout/stderr/stdin safe to touch, whatever the build is.

    Two distinct repairs, because there are two distinct broken states:

      1. Stream is None (windowed frozen build, no console) -> substitute a
         `_NullStream`. This is what stops uvicorn's default log_config from
         raising `ValueError: Unable to configure formatter 'default'`.
      2. Stream exists but speaks the legacy code page (cp1252 on many Windows
         installs) -> reconfigure it to UTF-8. Without this, the first Chinese
         status line crashes with UnicodeEncodeError. (This is the behaviour
         that used to live at the top of launcher.py.)

    Call this as early as possible — before importing anything that formats log
    records — and call it as often as you like: it is idempotent.
    """
    for stream_name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, stream_name, None)

        if isinstance(stream, _NullStream):
            continue  # already shimmed; nothing to do (idempotency)

        if stream is None:
            null = _NullStream()
            setattr(sys, stream_name, null)
            # Also patch the dunder originals. They are None in a windowed
            # build too, and third-party code reaches for sys.__stderr__
            # specifically when it wants to bypass a redirected stream.
            if getattr(sys, "__%s__" % stream_name, None) is None:
                try:
                    setattr(sys, "__%s__" % stream_name, null)
                except Exception:
                    pass
            continue

        # A real stream: force UTF-8 so Chinese text cannot kill a print().
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Not a TextIOWrapper (already wrapped, or a pipe someone replaced).
            # Nothing to do — this is best-effort by design.
            pass


def has_real_console() -> bool:
    """
    True when there is an actual console attached to write to.

    "Real" means: not None (windowed build) and not our own null shim. Anything
    else — a pipe, a redirect to a file, a terminal — counts as real, because a
    developer running from a shell still wants to see log lines.
    """
    stream = getattr(sys, "stdout", None)
    return stream is not None and not isinstance(stream, _NullStream)


# =============================================================================
# In-memory ring buffer of recent log records (for the in-app log viewer).
# =============================================================================
class RingBufferHandler(logging.Handler):
    """
    Keeps the last `capacity` log records in memory as plain dicts.

    Each entry is:
        {"seq": int, "time": "HH:MM:SS", "level": "INFO",
         "name": "any-loc", "msg": "...formatted, traceback included..."}

    `seq` is a process-wide monotonically increasing integer. It is the whole
    point of the design: a poller (e.g. the /api/log endpoint) passes back the
    last seq it saw and gets only what is new, with no dependence on list
    indices that shift as the ring drops old records.

    Thread-safety: a `deque(maxlen=...)` is atomic for append under CPython, but
    the seq counter and multi-step snapshot are not, so both go under an
    explicit lock. Records arrive from the device-worker thread, the tunnel
    thread, the HTTP threads and the GUI thread — all at once.
    """

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        super().__init__()
        self._buf: deque = deque(maxlen=max(1, int(capacity)))
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        # Message-only formatter: level/name/time are separate fields in the
        # entry dict, so duplicating them inside msg would just be noise. This
        # still appends the exception traceback, which is what we actually need.
        self.setFormatter(logging.Formatter("%(message)s"))

    # -- logging.Handler API --------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        """
        Record one entry. MUST NOT RAISE, ever.

        The base class would call `handleError()`, which writes to sys.stderr —
        in a windowed build that is a null stream, so a failure here would be
        both invisible and potentially recursive (a logging error triggering
        more logging). We swallow instead: losing a log line is always better
        than destabilising the app the log line was describing.
        """
        try:
            # format() renders the message AND appends exc_info's traceback
            # text, so a caught exception's full stack lands in the ring.
            msg = self.format(record)
            entry = {
                "seq": 0,  # filled under the lock, so seq order == buffer order
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            }
            with self._lock:
                entry["seq"] = next(self._seq)
                self._buf.append(entry)
        except Exception:
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        # emit() already swallows everything; this is here so that a failure
        # anywhere else in the Handler machinery cannot print to stderr either.
        pass

    # -- reader API -----------------------------------------------------------
    def snapshot(self, after: int = 0, limit: int = 200) -> tuple[list, int]:
        """
        Return `(entries, next_seq)` for every retained entry with seq > after.

        `entries` are shallow copies, so the caller can serialise them without
        holding the lock or racing a concurrent emit(). At most `limit` of them
        (pass limit=0 or None for "everything retained").

        `next_seq` is what to pass as `after` on the next poll: the seq of the
        last entry returned, or `after` unchanged when nothing is new. Note it
        is the seq of the LAST RETURNED entry, not the newest in the ring — if
        `limit` truncated the result, the next poll picks up exactly where this
        one stopped instead of skipping the remainder.
        """
        try:
            after = int(after)
        except (TypeError, ValueError):
            after = 0

        with self._lock:
            # Copy under the lock; the deque may be mutated by any thread.
            entries = [dict(e) for e in self._buf if e["seq"] > after]

        if limit:
            try:
                lim = int(limit)
                if lim > 0:
                    entries = entries[:lim]
            except (TypeError, ValueError):
                pass

        next_seq = entries[-1]["seq"] if entries else after
        return entries, next_seq

    def clear(self) -> None:
        """Drop everything retained. `seq` keeps counting (pollers stay valid)."""
        with self._lock:
            self._buf.clear()


# =============================================================================
# Where the log file goes.
# =============================================================================
def _real_user_home() -> str:
    """
    The logged-in user's home, even when running as root under `sudo`.

    Under sudo, expanduser("~") can resolve to /var/root, which would hide the
    log from the person actually using the app (and split it across two files
    depending on how AnyLoc was started). Honour SUDO_USER so the elevated run
    and the ordinary run write to the SAME log — the same reason Windows uses
    LOCALAPPDATA, which UAC does not redirect.
    """
    # SUDO_HOME is set by our own native-auth elevation path (osascript's
    # `with administrator privileges` is NOT sudo and exports neither), so it
    # is checked first; SUDO_USER covers a real `sudo` invocation.
    sudo_home = os.environ.get("SUDO_HOME")
    if sudo_home and os.path.isdir(sudo_home):
        return sudo_home
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            import pwd
            return pwd.getpwnam(sudo_user).pw_dir
        except Exception:
            pass
    return os.path.expanduser("~")


def log_dir() -> str:
    """
    Per-user log directory, created if missing. NEVER raises.

      Windows -> %LOCALAPPDATA%/AnyLoc/logs
      macOS   -> ~/Library/Logs/AnyLoc
      other   -> ~/.local/state/AnyLoc

    %LOCALAPPDATA% is deliberate on Windows: it is writable and resolves to the
    SAME path elevated and non-elevated (UAC does not redirect it), so the log
    from an elevated run and the log from a normal run land in one file instead
    of a VirtualStore shadow copy.

    If the directory cannot be created (locked-down profile, read-only home,
    exotic OneDrive redirection), fall back to the temp dir. A log we cannot
    write is a nuisance; an exception here would kill startup outright.
    """
    try:
        if IS_WINDOWS:
            base = os.environ.get("LOCALAPPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Local"
            )
            path = os.path.join(base, "AnyLoc", "logs")
        elif IS_MACOS:
            path = os.path.join(_real_user_home(), "Library", "Logs", "AnyLoc")
        else:
            path = os.path.join(_real_user_home(), ".local", "state", "AnyLoc")

        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        try:
            return tempfile.gettempdir()
        except Exception:
            # Genuinely nowhere to write. "." at least keeps the caller's
            # os.path.join() valid; the file handler is guarded anyway.
            return "."


# =============================================================================
# Setup.
# =============================================================================
# Module-level singleton so backend.py (and the GUI) can reach the ring without
# importing launcher.py — that would be an import cycle, since launcher imports
# backend.
RING: RingBufferHandler | None = None

# Handlers this module installed on the root logger, so a second setup_logging()
# call replaces them instead of stacking duplicates.
_OUR_HANDLERS: list = []


def get_ring() -> RingBufferHandler | None:
    """The installed RingBufferHandler, or None if setup_logging() hasn't run."""
    return RING


def setup_logging(level: int = logging.INFO, console: bool | None = None) -> RingBufferHandler:
    """
    Install the app's logging: rotating file + in-memory ring + optional console.

    `console`:
      None  (default) — auto-detect: add a console handler only when there is a
                        real console to write to (see `has_real_console()`).
                        In a windowed build there is none, so we skip it: a
                        StreamHandler onto a null stream is pure overhead that
                        formats every record and throws it away.
      True/False      — force. `True` is still ignored if sys.stdout is None,
                        because StreamHandler(None) would raise.

    Returns the RingBufferHandler (also reachable later via `get_ring()`).

    Like everything else here, this cannot raise: if the log file cannot be
    opened we carry on with the ring alone rather than taking the app down.
    """
    global RING, _OUR_HANDLERS

    root = logging.getLogger()

    # Idempotency: drop the handlers a previous call installed. Anyone else's
    # handlers are left strictly alone — this must not fight a caller that
    # deliberately added their own.
    for h in _OUR_HANDLERS:
        try:
            root.removeHandler(h)
            h.close()
        except Exception:
            pass
    _OUR_HANDLERS = []

    root.setLevel(level)

    # -- 1. rotating file log (the only durable record in a windowed build) ---
    try:
        path = os.path.join(log_dir(), "anyloc.log")
        fh = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",   # non-negotiable: the UI strings are Chinese
        )
        fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATEFMT))
        fh.setLevel(level)
        root.addHandler(fh)
        _OUR_HANDLERS.append(fh)
    except Exception:
        # Disk full, permissions, path too long, AV lock — carry on regardless.
        pass

    # -- 2. in-memory ring (what the in-app log viewer reads) ----------------
    ring = RingBufferHandler()
    ring.setLevel(level)
    root.addHandler(ring)
    _OUR_HANDLERS.append(ring)
    RING = ring

    # -- 3. console, only if one actually exists ------------------------------
    want_console = has_real_console() if console is None else bool(console)
    if want_console and getattr(sys, "stdout", None) is not None:
        try:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_CONSOLE_DATEFMT))
            ch.setLevel(level)
            root.addHandler(ch)
            _OUR_HANDLERS.append(ch)
        except Exception:
            pass

    # -- 4. squelch the chatty dependencies ----------------------------------
    for noisy in NOISY_LOGGERS:
        try:
            logging.getLogger(noisy).setLevel(logging.WARNING)
        except Exception:
            pass

    return ring
