#!/usr/bin/env python3
"""
any-loc all-in-one launcher (becomes AnyLoc.exe on Windows / AnyLoc.app on macOS)
=================================================================================
Double-click behaviour:
  1. Re-launch itself with elevated rights if not already privileged — the
     tunnel needs Administrator (Windows) / root (macOS).
       * Windows -> UAC prompt (ShellExecuteW "runas").
       * macOS   -> opens Terminal and runs itself under `sudo` (inline password
                    prompt); if already in a terminal, just re-execs via sudo.
  2. Start the web UI server + device worker on a background thread.
  3. Start pymobiledevice3's tunneld on a background daemon thread.
  4. Give the MAIN thread to the app window (pywebview / Edge WebView2).

One window, one elevation prompt. No batch/shell files.

THREAD LAYOUT — the two constraints that decide everything here:
  * pywebview MUST own the main thread (webview.start() raises off-main-thread),
    so the UI is last and everything else is a daemon thread.
  * tunneld is happy on a background thread (uvicorn merely skips installing
    signal handlers when it isn't on the main thread).
That is the opposite of the old console build, where tunneld blocked main and
there was no window at all.
"""
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
import webbrowser

# ---- platform ---------------------------------------------------------------
# The core app is byte-for-byte identical on both OSes; only *elevation* and a
# couple of native dialogs differ (UAC on Windows, sudo/root on macOS).
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# ctypes.windll only exists on Windows; import it lazily where needed so the
# module imports cleanly on macOS/Linux.
if IS_WINDOWS:
    import ctypes

# THIS MUST STAY THE FIRST THING WE RUN, and it must come BEFORE `import backend`
# (which drags in pymobiledevice3 → uvicorn).
#
# In the windowed build (console=False) sys.stdout/stderr/stdin are all None.
# uvicorn's default log config builds a ColourizedFormatter that calls
# sys.stdout.isatty() at *configuration* time — on None that raises
# AttributeError, which dictConfig re-raises as
# ValueError("Unable to configure formatter default"), and the app dies before
# showing anything. install_null_streams() puts back harmless write-to-nowhere
# stream objects so any library that pokes at the streams simply works.
# (Belt and braces: we also pass log_config=None to uvicorn.Config — see
# start_tunneld(). Both are needed; neither alone is sufficient.)
# It also handles the old job this block used to do: forcing UTF-8 on real
# streams, since the legacy console code page (cp1252) chokes on our Chinese
# status text.
#
# Make our own directory importable FIRST (normally sys.path[0] already covers
# it, but not when this module is imported rather than run):
#   * frozen  -> next to the executable (PyInstaller bundle root)
#   * source  -> this file's own directory (src/)
for _d in (os.path.dirname(os.path.abspath(sys.executable)),
           os.path.dirname(os.path.abspath(__file__))):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import applog
applog.install_null_streams()

# ---- import the web server + worker from backend.py -------------------------
# (backend.py lives next to this file in src/ / is bundled by PyInstaller)
import backend

from http.server import ThreadingHTTPServer

WEB_HOST = "127.0.0.1"
WEB_PORT = 8765


# =============================================================================
# Elevation (cross-platform)
#   Windows -> Administrator via UAC.
#   macOS   -> root via sudo (the tunnel creates a TUN interface, which needs
#              root, exactly like admin is needed on Windows).
# =============================================================================
def is_admin() -> bool:
    """True if we already have the elevated rights the tunnel needs."""
    if IS_WINDOWS:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    # POSIX (macOS): root is uid 0.
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _native_error(message: str, title: str = "AnyLoc") -> None:
    """Show a native, blocking error dialog (best-effort; falls back to print)."""
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    elif IS_MACOS:
        try:
            script = (
                f'display dialog {json_str(message)} with title {json_str(title)} '
                f'buttons {{"OK"}} default button "OK" with icon stop'
            )
            subprocess.run(["osascript", "-e", script], check=False)
            return
        except Exception:
            pass
    print(f"\n[{title}] {message}\n", file=sys.stderr)


def json_str(s: str) -> str:
    """
    Quote a Python string as an AppleScript string literal.

    ensure_ascii=False is REQUIRED: AppleScript's `do script`/`display dialog`
    reject Python's \\uXXXX escapes, so a non-ASCII path (e.g. a Chinese folder
    name — this project can live under exactly such a path) would otherwise make
    the Terminal-elevation osascript fail with a syntax error and silently break
    the whole macOS launch. Emitting the raw UTF-8 characters works correctly.
    """
    import json
    return json.dumps(s, ensure_ascii=False)


def relaunch_as_admin() -> None:
    """Re-run this program elevated, then exit the current (non-elevated) copy."""
    if IS_WINDOWS:
        return _relaunch_windows_uac()
    if IS_MACOS:
        return _relaunch_macos_sudo()
    # Other POSIX: best-effort sudo re-exec in place.
    _relaunch_posix_sudo()


def _relaunch_windows_uac() -> None:
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in [os.path.abspath(__file__)] + sys.argv[1:])

    # ShellExecuteW with "runas" triggers the UAC prompt
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if rc <= 32:
        # user declined UAC, or it failed
        _native_error(
            "AnyLoc needs Administrator rights to create the tunnel to your iPhone.\n\n"
            "Please allow the prompt (or right-click the app > Run as administrator).",
            "AnyLoc",
        )
    sys.exit(0)


def _elevation_argv() -> list:
    """The command that re-runs *this* program, frozen (.app/.exe) or as .py."""
    # Finder sometimes passes a "-psn_0_12345" process-serial-number arg to an
    # .app; strip it so it isn't forwarded through the sudo re-launch.
    passthru = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    if getattr(sys, "frozen", False):
        return [sys.executable] + passthru
    return [sys.executable, os.path.abspath(__file__)] + passthru


def _relaunch_posix_sudo() -> None:
    """Re-exec in place under sudo (works when a controlling terminal exists)."""
    argv = _elevation_argv()
    os.execvp("sudo", ["sudo", "-E", "--"] + argv)


def _relaunch_macos_sudo() -> None:
    """
    Get root on macOS.

    Three situations, in order of preference:

      * Launched from a Terminal (there's a TTY) -> just re-exec via `sudo`;
        the password prompt appears right here in the same window.

      * Double-clicked in Finder -> ask for the password with the NATIVE macOS
        authentication dialog (`osascript ... with administrator privileges`),
        which is the Mac counterpart of the Windows UAC prompt: a familiar system
        sheet, no Terminal window to keep open and no window the user can close
        by accident. Requires NSAppleEventsUsageDescription in the bundle's
        Info.plist (see AnyLoc.spec) or macOS denies the Apple Event outright.

      * If that fails -> fall back to the old Terminal.app path, which still
        works and at least shows the live log.

    NOTE: with the userspace tunnel (pymobiledevice3 10+) this whole function is
    usually never reached on macOS — see run_ios_userspace(). It remains the path
    for iOS 17.0-17.3, which genuinely needs the privileged kernel tunnel.
    """
    argv = _elevation_argv()

    # Has a controlling terminal? Then elevate in place — simplest and cleanest.
    if sys.stdin and sys.stdin.isatty():
        try:
            os.execvp("sudo", ["sudo", "-E", "--"] + argv)
        except Exception as e:
            _native_error(f"Could not run sudo: {e}", "AnyLoc")
            sys.exit(1)
        return

    cmd = " ".join(shlex.quote(a) for a in argv)

    # --- preferred: the native macOS password sheet --------------------------
    # `do shell script ... with administrator privileges` runs the command as
    # root after macOS collects the password itself. The command is detached
    # (nohup + &) so osascript returns immediately instead of blocking for the
    # entire life of the app.
    #
    # SUDO_USER/SUDO_HOME are exported BY HAND because this path is not `sudo`:
    # osascript hands the command to root with no record of who asked, so
    # everything that resolves "the real user's home" (the log dir, the WebView
    # profile, the single-instance lockfile) would silently fall back to
    # /var/root. That would hide the log, reset the profile, and — worst —
    # give the elevated instance a DIFFERENT lockfile from the normal one, so
    # the single-instance guard would stop guarding. `sudo` sets these itself;
    # here we must.
    _user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    _home = os.path.expanduser("~")
    prelude = ""
    if _user and _user != "root":
        prelude = (f"export SUDO_USER={shlex.quote(_user)} "
                   f"SUDO_HOME={shlex.quote(_home)}; ")
    detached = f"{prelude}nohup {cmd} >/dev/null 2>&1 &"
    try:
        subprocess.run(
            ["osascript", "-e",
             f'do shell script {json_str(detached)} with administrator privileges'],
            check=True, capture_output=True, timeout=300,
        )
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace")
        # -128 is "User canceled" — they clicked Cancel on the password sheet.
        # That is a deliberate choice, not a failure to report as a crash.
        if "-128" in err or "User canceled" in err:
            log = logging.getLogger("any-loc")
            log.info("User cancelled the authentication prompt.")
            sys.exit(0)
        logging.getLogger("any-loc").info(
            "Native auth prompt failed (%s); falling back to Terminal.", err.strip())
    except Exception as e:
        logging.getLogger("any-loc").info(
            "Native auth prompt unavailable (%s); falling back to Terminal.", e)

    # --- fallback: open Terminal.app running `sudo <us>` ---------------------
    # `exec sudo ...` so the tunnel process replaces the shell (closing the
    # Terminal window then cleanly stops AnyLoc).
    shell_line = f"clear; echo '--- AnyLoc (macOS) ---'; exec sudo {cmd}"
    osa = (
        'tell application "Terminal"\n'
        f"  do script {json_str(shell_line)}\n"
        "  activate\n"
        "end tell\n"
    )
    try:
        subprocess.run(["osascript", "-e", osa], check=True)
    except Exception as e:
        _native_error(
            "AnyLoc needs root to create the tunnel to your iPhone, and could not "
            f"open Terminal automatically.\n\n{e}\n\n"
            "You can start it manually in Terminal with:\n"
            f"   sudo {cmd}",
            "AnyLoc",
        )
    sys.exit(0)


# =============================================================================
# Web UI server thread (reuses backend.DeviceWorker + backend.Handler)
# =============================================================================
def start_web_server(worker: "backend.DeviceWorker") -> ThreadingHTTPServer:
    backend.Handler.worker = worker
    httpd = ThreadingHTTPServer((WEB_HOST, WEB_PORT), backend.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="web-server")
    t.start()
    return httpd


def open_browser(url: str) -> None:
    """
    Open the default browser at `url`.

    On macOS we usually run elevated via `sudo`, and asking root to open the
    browser tends to spawn a root-owned browser (or nothing) instead of using
    the logged-in user's session. When we detect that case, drop back to the
    original user ($SUDO_USER) via `open` so the page appears in *their* browser.
    """
    if IS_MACOS:
        sudo_user = os.environ.get("SUDO_USER")
        try:
            euid_root = os.geteuid() == 0
        except AttributeError:
            euid_root = False
        if euid_root and sudo_user and sudo_user != "root":
            try:
                subprocess.Popen(["sudo", "-u", sudo_user, "open", url])
                return
            except Exception:
                pass  # fall through to webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


# =============================================================================
# Single-instance lock (Windows)
# =============================================================================
# The old guard was "the web server failed to bind port 8765, so we must already
# be running". That was never true: ThreadingHTTPServer sets
# allow_reuse_address=True, so a second process binds the same LIVE port quite
# happily and you end up with two AnyLocs fighting over one phone. A named
# kernel mutex is the real thing — and the kernel releases it even if we are
# hard-killed, so a crash cannot leave a stale lock behind.
_MUTEX_NAME = "Local\\AnyLoc.SingleInstance.v1"
_ERROR_ALREADY_EXISTS = 183
_instance_mutex = None  # module-level: keep the handle alive for the process


def _acquire_single_instance_posix() -> bool:
    """
    POSIX single-instance guard: hold an exclusive, non-blocking flock().

    Returns True if we got the lock, False if another AnyLoc holds it.

    The lock FILE is never deleted — deleting it would race a second process
    that already has it open. What matters is the flock itself, which the kernel
    releases automatically when this process exits (crash included), so there is
    no stale-lock cleanup to get wrong.

    The fd is stashed in a module-level global on purpose: if it were garbage
    collected the file would close and the lock would silently vanish.

    Any failure here returns True — the lock is a convenience, and a broken lock
    mechanism must never be the reason the app refuses to start.
    """
    global _instance_mutex
    try:
        import fcntl

        # Alongside the app's other per-user state, and writable without root.
        try:
            import gui
            lock_dir = gui._app_data_root()
        except Exception:
            lock_dir = os.path.join(os.path.expanduser("~"), ".anyloc")
        os.makedirs(lock_dir, exist_ok=True)
        path = os.path.join(lock_dir, "anyloc.lock")

        fd = open(path, "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Held by another live AnyLoc.
            fd.close()
            return False

        # Record who holds it — purely diagnostic, for reading the file by hand.
        try:
            fd.write(f"{os.getpid()}\n")
            fd.flush()
        except Exception:
            pass

        _instance_mutex = fd      # keep alive for the life of the process
        return True
    except Exception:
        return True


def _already_running_error() -> None:
    """
    Tell the user another AnyLoc holds the lock but shows no window.

    The instructions MUST match the OS: sending a Mac user to "Task Manager" to
    kill "AnyLoc.exe" names a program and an application that do not exist on
    their machine. Shared by both the iOS and Android paths so the two can never
    drift apart.
    """
    if IS_WINDOWS:
        how = ("请打开任务管理器（Ctrl+Shift+Esc），结束所有 AnyLoc.exe，然后重新打开。\n\n"
               "An AnyLoc process is still holding the lock but has no window.\n"
               "Open Task Manager, end any AnyLoc.exe, then start it again.")
    else:
        how = ("请打开「活动监视器」（Activity Monitor），搜索 AnyLoc 并强制退出，然后重新打开。\n"
               "也可以在终端运行：pkill -f AnyLoc\n\n"
               "An AnyLoc process is still holding the lock but has no window.\n"
               "Open Activity Monitor, search for AnyLoc and Force Quit it, then "
               "start it again — or run:  pkill -f AnyLoc")
    _native_error("AnyLoc 好像还有一个没退干净的进程占着，但它没有窗口。\n" + how,
                  "AnyLoc")


def focus_existing_window() -> bool:
    """
    Find an already-running AnyLoc window and bring it to the front.

    Used when the single-instance lock turns out to be held: switching the user
    to the window they already have is what they actually wanted, and it beats a
    dialog telling them to go hunt through the taskbar themselves.

    Returns False when no window is found — which is the interesting case. It
    means the lock is held by a process with no window (wedged, or still tearing
    down), and the caller should say so rather than pretend all is well.
    """
    if IS_MACOS:
        # Raise the running AnyLoc via System Events. Without this a second
        # launch always fell through to the "it has no window, Force Quit it"
        # dialog — alarming advice for the ordinary case of double-clicking the
        # app twice, where the right answer is simply to show the window that is
        # already open. Matches on the bundle/process name, and is best-effort:
        # if System Events is unavailable or denied, we return False and the
        # caller explains the situation instead.
        me = os.getpid()
        script = f'''
            tell application "System Events"
                set procs to (every process whose name contains "AnyLoc")
                repeat with p in procs
                    if unix id of p is not {me} then
                        set frontmost of p to true
                        return "ok"
                    end if
                end repeat
            end tell
            return "none"
        '''
        try:
            out = subprocess.run(["osascript", "-e", script],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() == "ok"
        except Exception:
            return False

    if not IS_WINDOWS:
        return False
    try:
        u = ctypes.windll.user32
        # restype/argtypes are NOT optional here. ctypes defaults a function's
        # return type to C `int` (32-bit), but an HWND is a POINTER — on 64-bit
        # Windows a handle above 2^31 comes back sign-extended/truncated, and
        # the following calls then operate on a bogus handle (or silently fail).
        # Declaring the signatures keeps the full 64-bit value intact.
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.FindWindowW.restype = ctypes.c_void_p
        u.IsIconic.argtypes = [ctypes.c_void_p]
        u.IsIconic.restype = ctypes.c_int
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.ShowWindow.restype = ctypes.c_int
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.restype = ctypes.c_int

        # Match on the title set by gui.run_window(title="AnyLoc"); the window
        # class belongs to pywebview's WinForms host and is not ours to rely on.
        hwnd = u.FindWindowW(None, "AnyLoc")
        if not hwnd:
            return False
        # Restore first — SetForegroundWindow is ignored on a minimized window.
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)      # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def acquire_single_instance() -> bool:
    """
    Take the single-instance lock. True if we got it, False if AnyLoc is already
    running (caller should tell the user and exit).

    TIMING IS LOAD-BEARING: call this AFTER the elevation relaunch, never before.
    The non-elevated parent stays alive while ShellExecuteW spawns the elevated
    child, so a parent holding the mutex would block its own child and the app
    would refuse to start every single time.

    POSIX/macOS: an exclusive flock() on a lockfile. The claim that "macOS gets
    one app instance from LaunchServices" does NOT hold for this app: the Finder
    launch immediately re-execs itself under sudo/Terminal and the original
    LaunchServices-tracked process exits, so LaunchServices believes AnyLoc quit
    and will happily start a second one. The .command scripts bypass it entirely.
    flock has the property we actually need — the KERNEL drops it when the
    process dies, including on a hard kill, so a crash cannot strand the lock.
    """
    global _instance_mutex
    if not IS_WINDOWS:
        return _acquire_single_instance_posix()
    try:
        # use_last_error=True is not optional: with the plain `ctypes.windll`
        # handle, ctypes does not preserve the thread's Win32 last-error across
        # the call, so a separate GetLastError() can read a value clobbered by
        # ctypes' own internals. This routes it through ctypes' saved copy,
        # which is the documented way to read it reliably.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        err = ctypes.get_last_error()
        if handle and err == _ERROR_ALREADY_EXISTS:
            # Someone else owns the name. Drop our (valid but redundant) handle
            # so we don't keep the object alive past our own exit.
            try:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
            except Exception:
                pass
            return False
        if not handle:
            # CreateMutexW itself failed — don't let that block the launch.
            return True
        # Stash the handle so it lives as long as the process does. The kernel
        # releases the name when we exit, INCLUDING on a hard kill, so a crash
        # can never leave a stale lock behind.
        _instance_mutex = handle
        return True
    except Exception:
        # Never let the lock mechanism itself stop the app from launching.
        return True


# =============================================================================
# tunneld on a background thread + the ordered shutdown
# =============================================================================
def start_tunneld(host: str, port: int):
    """
    Run pymobiledevice3's tunneld under a uvicorn server WE own, on a daemon
    thread. Returns (server, thread).

    Why not TunneldRunner.create()? Two reasons, both fatal here:
      1. It calls uvicorn.run() with the DEFAULT log config, whose colourised
         formatter calls sys.stdout.isatty() — and in the windowed build the
         streams are None. (We install null streams early AND pass
         log_config=None; belt and braces, because this crash happens before
         anything is on screen and is thus invisible.)
      2. No server handle escapes it, so there is no way to stop it. Closing the
         window could then never stop the tunnel.
    Its __init__, by contrast, only builds the FastAPI app — it does not serve.
    So we construct it and serve `_app` ourselves, which gives us
    `server.should_exit`. Setting that from another thread stops it in ~120ms
    AND runs the FastAPI lifespan shutdown (TunneldCore.close()) — a real
    cleanup, unlike the /shutdown route (see shutdown_all).
    """
    import uvicorn
    from pymobiledevice3.tunneld.server import TunneldRunner

    runner = TunneldRunner(host, port)

    # `_app` is private, and pymobiledevice3 has a habit of reshuffling modules
    # between versions. If it ever disappears, say so loudly instead of dying
    # with a bare AttributeError in a window with no console.
    app = getattr(runner, "_app", None)
    if app is None:
        raise RuntimeError(
            "This version of pymobiledevice3 no longer exposes "
            "TunneldRunner._app, so AnyLoc cannot host the tunnel itself. "
            "Try:  pip install -U pymobiledevice3"
        )

    config = uvicorn.Config(
        app, host=host, port=port,
        loop="asyncio",
        log_config=None,   # REQUIRED — see the docstring above.
    )
    server = uvicorn.Server(config)

    def _serve():
        try:
            server.run()
        except SystemExit as e:
            # uvicorn calls sys.exit(1) when the port is already taken. On a
            # background thread that just kills the thread silently, and with no
            # console the user would see a window that simply never connects.
            # Surface it in the log + the UI's device banner instead.
            _tunneld_failed(
                f"隧道端口 {host}:{port} 被占用（可能已有 AnyLoc 或 "
                f"pymobiledevice3 tunneld 在运行）。code={e.code}")
        except Exception as e:
            _tunneld_failed(f"隧道启动失败：{type(e).__name__}: {e}")

    t = threading.Thread(target=_serve, daemon=True, name="tunneld")
    t.start()
    return server, t


def _tunneld_failed(msg: str) -> None:
    """Report a tunnel start failure to the log AND to the UI status banner."""
    logging.getLogger("any-loc").error("%s", msg)
    try:
        w = backend.Handler.worker
        if w is not None:
            # Reuse the devmode channel — it is already the UI's single place
            # for "what is wrong with the device connection".
            w.devmode = {"state": "error", "msg": msg}
    except Exception:
        pass


class _Shutdown:
    """
    The one ordered, idempotent teardown. Registered with atexit AND wired to
    the window's close event, so it runs whichever way the app ends.

    ORDER MATTERS, and step 1 is the whole point:
      1. worker.shutdown(clear_first=True) — BLOCKS until the phone is actually
         back on real GPS. This is the fix for the long-standing bug where
         closing the app left the phone frozen at the fake coordinate: the old
         shutdown() returned immediately, the interpreter exited, and the daemon
         worker thread was killed before its async cleanup could run.
      2. httpd.shutdown() — stop serving the UI (nothing left to serve).
      3. tunneld server.should_exit = True, then join — the tunnel is what the
         worker was talking THROUGH, so it must outlive step 1.
    Every step is logged, so the log file shows exactly how far teardown got if
    something wedges.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._done = False
        self.worker = None
        self.httpd = None
        self.tunneld_server = None
        self.tunneld_thread = None
        self.log = logging.getLogger("any-loc")

    def __call__(self) -> None:
        # Lock, not just a flag: the window-close handler and atexit can land on
        # two different threads, and a half-done teardown is worse than none.
        with self._lock:
            if self._done:
                return
            self._done = True
            self.log.info("Shutting down …")

            # 1) device first — this is what restores real GPS. Blocking.
            if self.worker is not None:
                try:
                    ok = self.worker.shutdown(timeout=8.0, clear_first=True)
                    self.log.info("  worker stopped (real GPS restored=%s)", ok)
                except Exception:
                    self.log.exception("  worker shutdown failed")

            # 2) web UI
            if self.httpd is not None:
                try:
                    self.httpd.shutdown()
                    self.log.info("  web server stopped")
                except Exception:
                    self.log.exception("  web server shutdown failed")

            # 3) tunnel. NEVER via its GET /shutdown route: on Windows that does
            #    os.kill(pid, SIGINT) == TerminateProcess(handle, 2), i.e. this
            #    whole process dies instantly with exit code 2 and no cleanup —
            #    which would skip everything above.
            if self.tunneld_server is not None:
                try:
                    self.tunneld_server.should_exit = True
                    self.log.info("  tunnel asked to stop")
                except Exception:
                    self.log.exception("  could not signal the tunnel")
            if self.tunneld_thread is not None:
                try:
                    self.tunneld_thread.join(timeout=10.0)
                    self.log.info("  tunnel stopped (alive=%s)",
                                  self.tunneld_thread.is_alive())
                except Exception:
                    self.log.exception("  tunnel join failed")

            self.log.info("Shutdown complete.")


def install_signal_handlers(shutdown_all: "_Shutdown") -> None:
    """
    Run the ordered teardown when the OS asks us to quit. POSIX only.

    WHY THIS EXISTS — atexit is NOT enough on macOS.
    `atexit` handlers run on a normal interpreter exit, but a process killed by
    a SIGNAL never reaches them. Verified: a process sent SIGHUP dies with
    atexit skipped entirely.

    That is not a corner case here, it is the DOCUMENTED way to quit on macOS.
    Closing the Terminal window (README: "To stop: close that Terminal window")
    sends SIGHUP to the foreground process group. Without this, the teardown —
    including `worker.shutdown(clear_first=True)`, the step that puts the phone
    back on its REAL GPS — is skipped, and the phone stays frozen at the fake
    coordinate. That was the single most user-visible macOS bug.

      SIGHUP  — Terminal window closed / session hung up  (the important one)
      SIGTERM — `kill`, Activity Monitor "Quit", logout/shutdown
      SIGINT  — Ctrl+C in a terminal run

    Windows has no meaningful SIGHUP and closes via the window's `closing`
    event, so this is a no-op there and the Windows path is untouched.

    Handlers must be installed from the MAIN thread (Python raises ValueError
    otherwise), which is why this is called from main() before the window takes
    over the main thread.

    `_Shutdown` is idempotent and lock-guarded, so a signal arriving *during* a
    window-close teardown is harmless — the second call returns immediately.
    """
    if IS_WINDOWS:
        return

    import signal

    log = logging.getLogger("any-loc")

    # Two mechanisms can deliver the same signal (see the long note below), so
    # the teardown must be entered exactly once. _Shutdown is itself idempotent;
    # this guard additionally stops the second caller from racing the os.kill()
    # re-raise while the first is still restoring the phone.
    _firing = threading.Lock()
    _fired = [False]

    def _teardown(signum):
        with _firing:
            if _fired[0]:
                return
            _fired[0] = True
        name = signal.Signals(signum).name
        log.info("Received %s — restoring real GPS before exit …", name)
        try:
            shutdown_all()
        except Exception:
            log.exception("teardown on %s failed", name)
        # Re-raise with the DEFAULT disposition so the process dies the way the
        # sender intended (and reports the right exit status) instead of us
        # silently swallowing the signal. Teardown has already run by now.
        try:
            # Detach the wakeup pipe first: with SIG_DFL about to fire, a stale
            # fd would only invite a write error on the way out.
            try:
                signal.set_wakeup_fd(-1)
            except Exception:
                pass
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:
            # If re-raising fails for any reason, still leave — the caller asked
            # us to stop and the device is already released.
            os._exit(0)

    # HOW the signal is caught matters as much as that it is caught. There are
    # two independent traps here; both were verified experimentally, and the
    # combination below is the only one that survives all of them.
    #
    # TRAP 1 — a Python-level handler runs on the MAIN thread, and only when
    # that thread next executes Python bytecode. Once the native window opens,
    # main is parked inside Cocoa's run loop (C code), so the handler is
    # deferred until something else wakes it — exactly the case this exists to
    # cover. (Verified: teardown did not run until the window closed by other
    # means.)
    #
    # TRAP 2 — pthread_sigmask() masks only the CALLING thread and is inherited
    # only by threads created afterwards. The worker and web-server threads
    # already exist here with the signal unblocked, and a process-directed
    # signal may be delivered to ANY thread that accepts it. With the default
    # disposition, one of those threads takes it and the process dies instantly
    # with no teardown. (Verified: silent death, no handler, no atexit.)
    # sigwait() alone therefore does NOT work, and neither does SIG_IGN (which
    # discards the signal before sigwait can dequeue it).
    #
    # THE COMBINATION THAT WORKS:
    #   1. Install a real Python handler. This sets the process-wide
    #      disposition, so whichever thread accepts the signal no longer dies —
    #      it just marks it for the main thread. This defuses trap 2.
    #   2. Point set_wakeup_fd() at a pipe. CPython writes the signal number to
    #      that fd from inside the C-level handler, on whatever thread took it,
    #      with no dependency on the main thread running Python. A watcher
    #      thread blocked on that pipe therefore wakes IMMEDIATELY, which
    #      defuses trap 1.
    # The handler and the watcher both call _teardown, which is guarded so only
    # the first one through does the work.
    sigs = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)

    for sig in sigs:
        try:
            signal.signal(sig, lambda s, _f: _teardown(s))
        except (ValueError, OSError, AttributeError) as e:
            log.debug("Could not install a handler for %s: %s", sig, e)

    try:
        r_fd, w_fd = os.pipe()
        os.set_blocking(r_fd, False)
        os.set_blocking(w_fd, False)
        # Must be called from the main thread; returns the previous fd (-1).
        signal.set_wakeup_fd(w_fd)

        def _wakeup_watch():
            import selectors
            sel = selectors.DefaultSelector()
            sel.register(r_fd, selectors.EVENT_READ)
            while True:
                if not sel.select(timeout=None):
                    continue
                try:
                    data = os.read(r_fd, 64)
                except BlockingIOError:
                    continue
                if not data:
                    return
                # The byte(s) written are the signal number(s).
                _teardown(data[-1])
                return

        threading.Thread(target=_wakeup_watch, daemon=True,
                         name="signal-watch").start()
    except (ValueError, OSError, AttributeError) as e:
        # Without the pipe we still have the plain handlers, which cover every
        # case except "main thread parked in a native run loop".
        log.debug("set_wakeup_fd unavailable (%s); using plain handlers only.", e)


def run_ui_blocking(url: str, shutdown_all: "_Shutdown", log) -> None:
    """
    Hand the MAIN thread to the UI and block until the user closes the window.

    pywebview raises WebViewException("pywebview must be run on a main thread")
    if started anywhere else, so this is always the last thing main() does and
    everything else in the app is a daemon thread.

    Three tiers, best first:
      1. native window (Edge WebView2) — the real product,
      2. a DEDICATED browser window, whose process exiting tells us the user
         closed it (the same "closed → tear down" signal the native window's
         close event gives us),
      3. the plain default browser + an idle loop, when even that fails.
    `gui` is imported lazily so that --dev / --selftest never need pywebview.
    """
    import gui

    if gui.can_use_native_window():
        log.info("Opening the app window …")
        if gui.run_window(
            url,
            on_close=shutdown_all,
            title="AnyLoc",
            # pywebview sizes are PHYSICAL pixels, not CSS pixels — these are
            # the values that look right on a 150% display.
            width=1280, height=860, min_size=(940, 640),
        ):
            return
        log.warning("Native window unavailable — falling back to the browser.")

    log.info("Opening in a browser window …")
    proc = gui.run_browser_fallback(url)
    if proc is None:
        # Last resort: the default browser, which we cannot watch for close.
        open_browser(url)
        log.info("界面已在默认浏览器打开：%s", url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
    shutdown_all()


# =============================================================================
# Main
# =============================================================================
def run_dev(log) -> None:
    """
    Developer mode — fast UI iteration with live-reload (edit web/ → browser
    auto-refreshes in ~1s), NO packaging needed.

    Two ways to run:
      * normal        -> serves the UI only (perfect for pure front-end work,
                         but the tunnel is skipped so you can't reach a device)
      * elevated      -> UI live-reload PLUS the real tunnel + Developer Mode
                         watcher, so you can test spoofing on a real iPhone
    To get the full flow:
      * Windows: right-click dev.bat > Run as administrator
      * macOS:   run `sudo ./dev.command` (or `sudo python3 launcher.py --dev`)
    """
    os.environ["ANYLOC_DEV"] = "1"
    # re-evaluate backend's DEV_MODE now that the env var is set
    backend.DEV_MODE = True

    dev_host, dev_port = "127.0.0.1", 8766
    admin = is_admin()

    # Platform-specific hint for how to get the real-device flow in dev mode.
    if IS_WINDOWS:
        _elevate_hint = "关掉重来，右键 dev.bat > 以管理员身份运行"
    else:
        _elevate_hint = "关掉重来，改用 sudo ./dev.command（或 sudo python3 launcher.py --dev）"

    print("\n" + "=" * 62)
    print("  AnyLoc — 开发模式 (DEV)")
    print(f"  网页:      http://{dev_host}:{dev_port}/")
    print("  热重载:    改 web/ 里的文件 → 浏览器自动刷新（~1秒）")
    if admin:
        print("  真机隧道:  ✓ 已启用 —— 可连 iPhone 实测定位")
    else:
        print("  真机隧道:  ✗ 未启用（当前非管理员/非 root）")
        print(f"             要实测定位：{_elevate_hint}")
    print("  改 Python 后端: Ctrl+C 停，再重新运行开发脚本（dev.bat / dev.command）")
    print("  按 Ctrl+C 停止。")
    print("=" * 62 + "\n")

    worker = _ios_worker()
    worker.start()

    # bind the web server on the dev port
    backend.Handler.worker = worker
    try:
        httpd = ThreadingHTTPServer((dev_host, dev_port), backend.Handler)
    except OSError as e:
        log.error("无法启动开发服务器 %s:%s (%s)。可能已在运行？", dev_host, dev_port, e)
        return
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web-dev").start()

    url = f"http://{dev_host}:{dev_port}/"
    threading.Timer(0.8, lambda: open_browser(url)).start()

    if admin:
        # full flow available — web server (with live-reload) is already running
        # above; now bring up the device worker, dev-mode watcher, and tunnel.
        worker.connect()
        DevModeWatcher(worker).start()
        log.info("以管理员/root 运行：启动隧道（连接 iPhone）…")
        from pymobiledevice3.tunneld.server import TunneldRunner
        from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS
        try:
            TunneldRunner.create(TUNNELD_DEFAULT_ADDRESS[0], TUNNELD_DEFAULT_ADDRESS[1])
        except KeyboardInterrupt:
            pass
        finally:
            worker.shutdown(); httpd.shutdown()
    else:
        # UI-only: just keep serving until Ctrl+C
        log.info("界面已就绪（仅网页）。做前端改动 → 浏览器会自动刷新。Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("退出开发模式。")
            worker.shutdown(); httpd.shutdown()


def _log_file_path() -> str:
    """
    Where applog put the log file, asked of logging itself rather than of applog.

    Reading it back off the root logger's handlers means we don't depend on an
    applog API beyond setup_logging() — and it still works if setup_logging()
    never ran (then there is no file handler, and we say so).
    """
    for h in logging.getLogger().handlers:
        name = getattr(h, "baseFilename", None)
        if name:
            return name
    return "(no log file)"


def main() -> None:
    """
    Entry point. Wraps _main() so that a crash in the WINDOWED build is visible.

    With console=False there is no console and sys.stdout is None: an unhandled
    traceback goes precisely nowhere and the app just vanishes with no
    explanation. So catch everything at the top, log it (the file log survives),
    and put it in front of the user in a native box along with the log path so
    they have something to send us.
    """
    try:
        _main()
    except SystemExit:
        raise                       # --selftest et al. exit deliberately
    except KeyboardInterrupt:
        pass
    except BaseException as e:
        try:
            logging.getLogger("any-loc").exception("Fatal error")
        except Exception:
            pass
        try:
            path = _log_file_path()
        except Exception:
            path = "(unknown)"
        _native_error(
            f"AnyLoc 遇到了一个错误，需要退出。\n\n"
            f"{type(e).__name__}: {e}\n\n"
            f"详细日志 / full log:\n{path}",
            "AnyLoc")
        raise SystemExit(1)


def _main() -> None:
    # In the windowed build there is no console and sys.stdout is None, so
    # print()/logging output is silently DISCARDED — a file log is the only way
    # this app is diagnosable at all. setup_logging() gives us that file plus an
    # in-memory ring that the UI reads back over /api/logs.
    # console=None means "attach a console handler only if there is a real
    # console", which keeps --dev/--selftest/CLI output exactly as it was.
    applog.setup_logging(level=logging.INFO)
    # NOTE: the chatty third-party loggers (pymobiledevice3, urllib3, asyncio,
    # uvicorn*) are squelched by setup_logging() itself via applog.NOISY_LOGGERS.
    # This used to repeat that list inline here; two copies of the same tuple
    # only ever drift apart, so the single owner is applog.
    log = logging.getLogger("any-loc")

    # --dev: developer mode. Fast iteration without packaging.
    #   * no elevation (so it starts instantly; tunnel/devmode only run if you
    #     happen to already be admin/root)
    #   * separate port (8766) so it never clashes with a running release build
    #   * live-reload: edit anything in web/ and the browser auto-refreshes
    # Use this while working on the UI. Package to .exe/.app only for release.
    if "--dev" in sys.argv:
        return run_dev(log)

    # --devmode-test: run only the DevModeWatcher against a connected device for
    # ~12s and print its status transitions, then exit. Proves the frozen bundle
    # can actually drive Developer Mode setup. Needs no elevation/tunnel.
    if "--devmode-test" in sys.argv:
        import time
        class _W:
            devmode = {"state": "idle", "msg": ""}
        w = _W()
        dm = DevModeWatcher(w)
        dm.start()
        last = None
        for _ in range(12):
            time.sleep(1)
            if w.devmode != last:
                print(f"[devmode-test] {w.devmode['state']}: {w.devmode['msg']}")
                last = dict(w.devmode)
            if w.devmode["state"] in ("enabled", "error"):
                break
        print("[devmode-test] FINAL:", w.devmode["state"])
        sys.exit(0 if w.devmode["state"] in ("enabled", "waiting", "trust", "reveal_done") else 1)

    # --selftest: boot the web stack WITHOUT elevation or tunnel, prove the
    # frozen import graph + web server work, then exit. Used to validate builds.
    if "--selftest" in sys.argv:
        print("[selftest] booting web stack (no elevation, no tunnel) ...")
        worker = _ios_worker()
        worker.start()
        try:
            httpd = start_web_server(worker)
        except OSError as e:
            print(f"[selftest] FAIL: web server: {e}")
            sys.exit(2)
        import urllib.request
        ok = True
        try:
            # /api/logs is in here deliberately: it is the ONLY way to see what
            # the windowed build is doing, so a build where it 500s is broken
            # even though everything else looks fine.
            for path in ("/", "/app.js", "/api/status", "/api/logs"):
                with urllib.request.urlopen(f"http://{WEB_HOST}:{WEB_PORT}{path}", timeout=5) as r:
                    code = r.getcode()
                    print(f"[selftest] GET {path} -> {code}")
                    ok = ok and (code == 200)
            # verify tunnel components import (the risky frozen bit)
            from pymobiledevice3.remote.module_imports import verify_tunnel_imports
            from pymobiledevice3.tunneld.server import TunneldRunner  # noqa
            print(f"[selftest] verify_tunnel_imports -> {verify_tunnel_imports()}")
        except Exception as e:
            print(f"[selftest] FAIL: {type(e).__name__}: {e}")
            ok = False
        finally:
            try:
                worker.shutdown(); httpd.shutdown()
            except Exception:
                pass
        print("[selftest] RESULT:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)

    # 0) Pick a device backend. Android needs neither elevation nor the tunnel,
    #    so we decide BEFORE elevating. Explicit --ios / --android override the
    #    probe; otherwise, if an authorized Android device is plugged in, use it.
    force_ios = "--ios" in sys.argv
    force_android = "--android" in sys.argv
    use_android = force_android or (not force_ios and detect_android())
    if use_android:
        return run_android(log)

    # 1) Decide whether we need elevation AT ALL.
    #
    #    Historically the answer was always yes: the tunnel builds a kernel
    #    `utun` interface, which requires Administrator/root. pymobiledevice3 10
    #    can instead build the tunnel on a pure-Python userspace network stack,
    #    which needs NO privileges — so on macOS the common case (iOS 17.4+ over
    #    USB) now runs with no password prompt at all, and the app stays a normal
    #    user process. That matters beyond convenience: a root-owned GUI app is
    #    discouraged on macOS and would push the WebView profile and logs into
    #    root's home instead of the user's.
    #
    #    iOS 17.0-17.3 has no CoreDeviceProxy service and still needs the kernel
    #    tunnel; IosBackend fails fast in that case (see _userspace_rsd) and the
    #    UI's error tells the user to relaunch elevated.
    #
    #    Windows keeps the original elevated path: it is the verified, shipped
    #    behavior there, and UAC is a single click rather than a typed password.
    use_userspace = (not IS_WINDOWS) and userspace_tunnel_supported() and not is_admin()
    if use_userspace:
        log.info("Using the in-process userspace tunnel — no root required.")
        return run_ios_userspace(log)

    if not is_admin():
        if IS_WINDOWS:
            log.info("Requesting administrator rights (needed for the tunnel) ...")
        else:
            log.info("Requesting root via sudo (needed for the tunnel) ...")
        relaunch_as_admin()
        return

    # 1.5) single-instance lock — AFTER elevation, never before. The
    #      non-elevated parent is still alive while UAC spawns the elevated
    #      child, so taking the lock earlier would make us block our own child.
    if not acquire_single_instance():
        # Prefer DOING the obvious thing over telling the user to do it.
        if focus_existing_window():
            return
        # No window to switch to: the lock is held by a process that is not
        # showing a UI (crashed mid-teardown, or still exiting). Tell the user
        # exactly how to get unstuck instead of leaving them at a dead end.
        _already_running_error()
        return

    _priv = "administrator" if IS_WINDOWS else "root"
    # These used to be print()s describing the console window you had to keep
    # open. There is no console now — this is a real app window — and print()
    # with sys.stdout=None is silently discarded anyway, so it goes to the log.
    log.info("=" * 62)
    log.info("  AnyLoc  —  running as %s", _priv)
    log.info("  关掉这个窗口就会停止模拟并恢复真实定位。")
    log.info("  最小化没关系，但模拟期间请不要关闭窗口。")
    log.info("=" * 62)

    # 2) tunnel import sanity
    from pymobiledevice3.remote.module_imports import verify_tunnel_imports
    if not verify_tunnel_imports():
        _native_error(
            "The tunnel components failed to load. Try reinstalling:\n"
            "   pip install -U pymobiledevice3",
            "AnyLoc")
        return

    # 3) start web UI + device worker (background)
    worker = _ios_worker()
    worker.start()
    worker.connect()  # will keep retrying via the UI's Connect button too
    try:
        httpd = start_web_server(worker)
    except OSError as e:
        # NOT "is AnyLoc already running?" — the mutex above answers that, and
        # this port was never a lock (allow_reuse_address lets a second process
        # bind it). A failure here is a genuinely occupied/blocked port.
        _native_error(
            f"无法在 {WEB_HOST}:{WEB_PORT} 启动界面服务。\n{e}\n\n"
            f"Could not start the web UI on {WEB_HOST}:{WEB_PORT}.\n"
            "另一个程序可能占用了这个端口。", "AnyLoc")
        return

    url = f"http://{WEB_HOST}:{WEB_PORT}/"
    log.info("Web UI:  %s", url)

    # 3.5) Continuously watch for a device and set up Developer Mode in the
    #      background. NOT one-shot: a new phone may be plugged in / unlocked /
    #      trusted long after launch, and the Trust dialog only appears once we
    #      try to pair — so we keep retrying instead of checking a single time.
    dm = DevModeWatcher(worker)
    dm.start()

    # 4) tunnel on a DAEMON THREAD (it used to block the main thread). The main
    #    thread now belongs to the window — pywebview refuses to start anywhere
    #    else.
    log.info("Starting tunnel (this is what talks to the iPhone) ...")
    shutdown_all = _Shutdown()
    shutdown_all.worker = worker
    shutdown_all.httpd = httpd
    try:
        t_host, t_port = __tunneld_addr()
        shutdown_all.tunneld_server, shutdown_all.tunneld_thread = \
            start_tunneld(t_host, t_port)
    except Exception as e:
        # Don't abort: without a tunnel the UI still loads and can explain
        # itself, which beats a window that never appears.
        _tunneld_failed(f"隧道启动失败：{type(e).__name__}: {e}")

    # Belt and braces. The window's close handler calls shutdown_all directly;
    # atexit covers the paths that don't go through it (a fatal error below, or
    # anything that unwinds main). It is idempotent, so a double call is free.
    import atexit
    atexit.register(shutdown_all)

    # atexit does NOT run when a signal kills us, and on macOS closing the
    # Terminal window (SIGHUP) is a normal way to quit — without this the phone
    # would be left on the fake location. See install_signal_handlers().
    install_signal_handlers(shutdown_all)

    # 5) MAIN THREAD -> the UI. This blocks until the window closes.
    run_ui_blocking(url, shutdown_all, log)


# =============================================================================
# Android path (adb) — no elevation, no tunnel.
# =============================================================================
def detect_android() -> bool:
    """True if an authorized Android device is reachable via the bundled adb.

    Cheap, non-elevated probe run before the iOS elevation step. Any failure
    (adb missing, no device, only-unauthorized) returns False so we fall back to
    the iOS path.
    """
    try:
        import subprocess
        from android_backend import find_adb
        adb = find_adb()
        # start-server is idempotent and quick; ignore its output.
        subprocess.run([adb, "start-server"], capture_output=True, timeout=15)
        out = subprocess.run([adb, "devices"], capture_output=True, timeout=15,
                             text=True).stdout
        for line in out.splitlines()[1:]:
            line = line.strip()
            if "\t" in line and line.split("\t", 1)[1].strip() == "device":
                return True
    except Exception:
        pass
    return False


def run_ios_userspace(log) -> None:
    """
    Run AnyLoc against an iPhone with NO elevation, using the in-process
    userspace tunnel (pymobiledevice3 10+).

    Shaped like run_android(): no elevation prompt and no separate `tunneld`
    server, because the tunnel lives inside our own process. Everything from the
    web server down is identical to the elevated path — only how we reach the
    phone differs.

    If the device turns out to need the kernel tunnel (iOS 17.0-17.3), the
    backend raises and the UI shows the error; the user can relaunch elevated to
    get the old path. We deliberately do NOT auto-elevate here: silently
    demanding a password after starting without one is worse than saying why.
    """
    if not acquire_single_instance():
        if focus_existing_window():
            return
        _already_running_error()
        return

    log.info("=" * 62)
    log.info("  AnyLoc  —  iOS 模式（免 root / no elevation）")
    log.info("  关闭窗口就会停止模拟并恢复真实定位。")
    log.info("=" * 62)

    worker = _ios_worker(userspace=True)
    worker.start()
    worker.connect()
    try:
        httpd = start_web_server(worker)
    except OSError as e:
        _native_error(
            f"无法在 {WEB_HOST}:{WEB_PORT} 启动界面服务。\n{e}\n\n"
            f"Could not start the web UI on {WEB_HOST}:{WEB_PORT}.\n"
            "另一个程序可能占用了这个端口。", "AnyLoc")
        return

    url = f"http://{WEB_HOST}:{WEB_PORT}/"
    log.info("Web UI:  %s", url)

    # Same device-setup guidance as the elevated path (plug in / Trust /
    # Developer Mode); none of it needs privileges.
    DevModeWatcher(worker).start()

    shutdown_all = _Shutdown()
    shutdown_all.worker = worker
    shutdown_all.httpd = httpd
    # (no tunneld process — the tunnel is inside the worker's own session)
    import atexit
    atexit.register(shutdown_all)
    install_signal_handlers(shutdown_all)

    run_ui_blocking(url, shutdown_all, log)


def run_android(log) -> None:
    """Run AnyLoc against an Android device over adb. No elevation, no tunnel.

    Mirrors main()'s iOS path minus the parts that only iOS needs: no UAC/sudo,
    no TunneldRunner. Like the iOS path, the MAIN thread goes to the app window,
    so the AdbWatcher — which used to block main and keep the process alive — now
    runs on its own daemon thread.
    """
    from android_backend import AndroidBackend

    # Safe to take the lock immediately here: the Android path never elevates,
    # so there is no parent process waiting on a child that would need it.
    if not acquire_single_instance():
        # Prefer DOING the obvious thing over telling the user to do it.
        if focus_existing_window():
            return
        # No window to switch to: the lock is held by a process that is not
        # showing a UI (crashed mid-teardown, or still exiting). Tell the user
        # exactly how to get unstuck instead of leaving them at a dead end.
        _already_running_error()
        return

    # Console-era copy, retired: this is a real app window, and print() with
    # sys.stdout=None is silently discarded anyway.
    log.info("=" * 62)
    log.info("  AnyLoc  —  Android 模式 (adb)")
    log.info("  不需要管理员权限。关掉这个窗口就会停止模拟并恢复真实定位。")
    log.info("  最小化没关系，但模拟期间请不要关闭窗口。")
    log.info("=" * 62)

    worker = backend.DeviceWorker(AndroidBackend())
    worker.start()
    worker.connect()  # the UI's Connect button retries too
    try:
        httpd = start_web_server(worker)
    except OSError as e:
        # The mutex above is the single-instance check; a failure here means the
        # port is genuinely taken by something else.
        _native_error(
            f"无法在 {WEB_HOST}:{WEB_PORT} 启动界面服务。\n{e}\n\n"
            f"Could not start the web UI on {WEB_HOST}:{WEB_PORT}.\n"
            "另一个程序可能占用了这个端口。", "AnyLoc")
        return

    url = f"http://{WEB_HOST}:{WEB_PORT}/"
    log.info("Web UI:  %s", url)

    shutdown_all = _Shutdown()
    shutdown_all.worker = worker
    shutdown_all.httpd = httpd
    # (no tunneld on Android — those slots stay None and are skipped)
    import atexit
    atexit.register(shutdown_all)

    # Same reason as the iOS path: a signal (Terminal closed / Force Quit) skips
    # atexit, and the phone's mock providers must still be removed.
    install_signal_handlers(shutdown_all)

    # Connection watcher, now on its own thread. Reports human-readable status
    # into worker.devmode so the UI can guide the user (plug in / authorize).
    watcher = AdbWatcher(worker)
    threading.Thread(target=watcher.run_blocking, daemon=True,
                     name="adb-watch").start()

    run_ui_blocking(url, shutdown_all, log)


class AdbWatcher:
    """
    Main-thread loop for Android mode: polls `adb devices` and writes a friendly
    setup status into worker.devmode (reusing the same status channel the iOS
    DevModeWatcher uses, so the UI banner just works). Also nudges the worker to
    (re)connect when a device appears. Blocks the main thread → keeps the process
    alive (the web server runs on a daemon thread).

    devmode states (Android): adb_waiting | adb_unauthorized | adb_ready | adb_error
    """

    def __init__(self, worker: "backend.DeviceWorker"):
        self.worker = worker
        self._stop = threading.Event()

    def _set(self, state: str, msg: str = ""):
        self.worker.devmode = {"state": state, "msg": msg}

    def run_blocking(self):
        import subprocess
        from android_backend import find_adb
        adb = find_adb()
        last_ready = None
        while not self._stop.is_set():
            state, serial = "adb_waiting", None
            try:
                out = subprocess.run([adb, "devices"], capture_output=True,
                                     timeout=15, text=True).stdout
                rows = []
                for line in out.splitlines()[1:]:
                    line = line.strip()
                    if "\t" in line:
                        s, st = line.split("\t", 1)
                        rows.append((s.strip(), st.strip()))
                ready = [s for s, st in rows if st == "device"]
                unauth = [s for s, st in rows if st == "unauthorized"]
                if ready:
                    state, serial = "adb_ready", ready[0]
                elif unauth:
                    state = "adb_unauthorized"
                else:
                    state = "adb_waiting"
            except Exception as e:
                state = "adb_error"
                self._set(state, str(e))
                time.sleep(2.0)
                continue

            self._set(state)
            # When a device becomes ready and the worker isn't connected, connect.
            if state == "adb_ready" and last_ready != serial:
                self.worker.connect()
            if state != "adb_ready":
                # device unplugged / de-authorized → drop back so it reconnects.
                last_ready = None
            else:
                last_ready = serial
            time.sleep(2.0)

    def stop(self):
        self._stop.set()


class DevModeWatcher:
    """
    Background watcher that keeps trying to make Developer Mode *available* over
    plain USB (no tunnel). Runs on its own thread + event loop, polling every few
    seconds.

    Why a loop and not a single check: on a NEW phone the user must (a) plug in,
    (b) unlock, and (c) tap "Trust" — and the Trust dialog only pops when we try
    to pair. That dance usually happens *after* launch, so a one-shot check almost
    always misses it. We retry until the device is paired, then REVEAL the
    Developer Mode option in Settings.

    Design decision: we only REVEAL the option (make it appear). We deliberately
    do NOT auto-enable it — enabling forces a device reboot, which is alarming and
    confusing when a PC app triggers it silently. Turning it on is left to the
    user via the Settings toggle: safe, visible, in their control. Once the user
    enables it (and reboots), we detect that and report "enabled".

    Reports human-readable progress into worker.devmode so the web UI can show it.
    """

    def __init__(self, worker):
        self.worker = worker
        self.log = logging.getLogger("any-loc.devmode")
        self._loop = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="devmode-watch")
        self._stop = False           # set True to end the watch loop
        self._revealed = False       # have we already revealed the option?
        self._set("waiting", "等待设备… 请插上 iPhone 并解锁")

    def _set(self, state, msg):
        # state: waiting | trust | revealing | reveal_done | enabled | error
        try:
            self.worker.devmode = {"state": state, "msg": msg}
        except Exception:
            pass
        self.log.info("[devmode] %s: %s", state, msg)

    def start(self):
        self._thread.start()

    def _run(self):
        import asyncio
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._watch())
        except Exception as e:
            self._set("error", f"检查开发者模式出错: {e}")

    async def _watch(self):
        import asyncio
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.amfi import AmfiService
        from pymobiledevice3.usbmux import list_devices

        announced_trust = False
        # Poll forever so the status reflects REALITY in real time: unplug the
        # phone -> back to "waiting"; enable dev mode -> "enabled". Never latch.
        while not self._stop:
            try:
                devs = await list_devices()
            except Exception:
                devs = []

            if not devs:
                self._set("waiting", "等待设备… 请用数据线插上 iPhone 并解锁")
                announced_trust = False
                self._revealed = False
                await asyncio.sleep(2)
                continue

            # A device is present — try to open a paired lockdown session.
            try:
                lockdown = await create_using_usbmux(autopair=True, pair_timeout=8)
            except Exception:
                if not announced_trust:
                    self._set("trust", "请在 iPhone 上点「信任这台电脑」并输入密码")
                    announced_trust = True
                await asyncio.sleep(2)
                continue

            # Paired. Check developer-mode status.
            try:
                already = await lockdown.get_developer_mode_status()
            except Exception:
                already = False

            if already:
                self._set("enabled", "开发者模式已开启 ✓ 可以连接了")
                self._revealed = False
                await asyncio.sleep(3)   # keep re-checking; do NOT latch/stop
                continue

            # OFF -> reveal the Settings row once per connection.
            if not self._revealed:
                self._set("revealing", "正在让「开发者模式」选项出现在设置里…")
                try:
                    amfi = AmfiService(lockdown)
                    await amfi.reveal_developer_mode_option_in_ui()
                    self._revealed = True
                except Exception as e:
                    self._set("error", f"无法自动显示开发者模式选项：{e}")
                    await asyncio.sleep(3)
                    continue

            # Revealed — now it's the user's move. Clear, non-scary guidance.
            self._set("reveal_done",
                      "已让「开发者模式」出现在设置里。请在 iPhone 上手动打开："
                      "设置 > 隐私与安全性 > 开发者模式 > 打开开关"
                      "（手机会重启，重启后在锁屏点「打开」确认）。开启后这里会自动继续。")

            # Keep polling: once the user enables + reboots, the check above flips
            # to "enabled". Also detects unplug -> "waiting".
            await asyncio.sleep(3)


def __tunneld_addr():
    from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS
    return TUNNELD_DEFAULT_ADDRESS


def _ios_worker(userspace: bool = False):
    """Build a DeviceWorker wired to the iOS (pymobiledevice3) backend."""
    from ios_backend import IosBackend
    return backend.DeviceWorker(IosBackend(*__tunneld_addr(), userspace=userspace))


def userspace_tunnel_supported() -> bool:
    """
    True if this pymobiledevice3 can build the tunnel with NO root.

    pymobiledevice3 10.x added a pure-Python (PyTCP) userspace network stack as
    an alternative to the kernel `utun` device. That kernel device was the ONLY
    reason AnyLoc ever needed Administrator/root, so when this is available we
    can skip elevation entirely — no UAC prompt, no sudo password.

    Older pymobiledevice3 (9.x, which the Windows build was verified against)
    has no such module, so this returns False there and everything falls back to
    the original elevated-tunneld path unchanged.
    """
    try:
        import importlib.util
        return importlib.util.find_spec(
            "pymobiledevice3.remote.userspace_tunnel") is not None
    except Exception:
        return False


if __name__ == "__main__":
    main()
