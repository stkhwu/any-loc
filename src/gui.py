#!/usr/bin/env python3
"""
gui.py — the native-window layer for AnyLoc (Windows: Edge WebView2 via pywebview).

This module owns exactly one thing: putting the web UI inside a real application
window instead of a browser tab. It deliberately imports NOTHING from launcher.py
or backend.py, so it can be probed standalone (`python src/gui.py`) and so the
"can we even do this?" question is answerable without booting the whole app.

Three hard-won constraints are encoded here; please do not undo them:

  * The GUI MUST own the MAIN THREAD. webview.start() raises
    WebViewException("pywebview must be run on a main thread") otherwise. The
    server/worker/tunnel all run fine on background threads — the window does not.

  * WebView2's presence MUST be checked explicitly, in the registry, BEFORE we
    create the window. If the runtime is missing pywebview raises nothing at all:
    it silently degrades to MSHTML/IE11, which renders enough chrome to look like
    it worked while quietly breaking the Leaflet map. There is no exception to
    catch, so the registry probe is the only defense we get.

  * The profile directory MUST be pinned via start(storage_path=...). pywebview
    IGNORES the WEBVIEW2_USER_DATA_FOLDER environment variable, and its default
    private_mode=True drops the profile in a fresh %TEMP% folder every launch —
    so theme, language, favorites and search history silently stop persisting.

In a windowed (console=False) build sys.stdout/stderr are None and print() output
is discarded without error, so everything diagnostic here goes through `log`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser

log = logging.getLogger("any-loc.gui")

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# CREATE_NO_WINDOW. Any subprocess we spawn from a windowed build must carry this
# or Windows flashes a console for it — the exact black box this whole conversion
# exists to get rid of.
CREATE_NO_WINDOW = 0x08000000

# The WebView2 Runtime's EdgeUpdate client GUID. A stable, documented Microsoft
# constant, not a per-machine id.
_WEBVIEW2_CLIENT_KEY = (
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)

# Recommended window geometry. pywebview sizes are PHYSICAL pixels, not CSS
# pixels, so on a 150% display these read smaller than the numbers suggest. They
# were picked on such a display: enough for the fixed 360px side panel plus a
# usable map, without overflowing a 1080p screen.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 860
DEFAULT_MIN_SIZE = (940, 640)


# =============================================================================
# Paths
#   Everything user-scoped lives under %LOCALAPPDATA%/AnyLoc. That folder is
#   writable and — importantly — resolves to the SAME path elevated and
#   non-elevated (UAC does not redirect LOCALAPPDATA), so the profile written by
#   an admin run is the one an ordinary run reads back.
# =============================================================================
def _app_data_root() -> str:
    """
    The per-user app-data folder.

      Windows -> %LOCALAPPDATA%/AnyLoc
      macOS   -> ~/Library/Application Support/AnyLoc
      other   -> ~/.anyloc

    macOS uses "Application Support" because that is where a Mac app is expected
    to keep this, and it sits alongside the log dir applog.py already writes to
    (~/Library/Logs/AnyLoc).

    A NOTE ON ROOT: when AnyLoc runs elevated, os.path.expanduser("~") would
    resolve to /var/root and the WebView profile would be written somewhere the
    normal user can never read back. We therefore honour SUDO_HOME/SUDO_USER and
    keep using the REAL user's home, so the profile written by an elevated run is
    the same one an ordinary run reads. (This mirrors why Windows uses
    LOCALAPPDATA: UAC does not redirect it, so both runs agree on the path.)
    """
    base = os.environ.get("LOCALAPPDATA")
    if IS_WINDOWS and base:
        return os.path.join(base, "AnyLoc")

    home = _real_user_home()
    if IS_MACOS:
        return os.path.join(home, "Library", "Application Support", "AnyLoc")
    # Non-Windows, or a stripped environment. ~/.anyloc is the closest thing
    # to "per-user, writable, survives reboots" we can rely on.
    return os.path.join(home, ".anyloc")


def _real_user_home() -> str:
    """
    The logged-in user's home, even when we are running as root under sudo.

    Delegates to applog._real_user_home(). This module used to carry a verbatim
    copy of that logic; two independent implementations of a security-adjacent
    path rule only ever drift, and fixing one would not reach the other. applog
    is a safe thing to depend on here — it imports only the standard library, so
    there is no cycle, and launcher.py imports it before anything else anyway.

    The fallback keeps this module's "probe me standalone" property intact: if
    applog cannot be imported at all, resolve the home the plain way rather than
    breaking `python src/gui.py`.
    """
    try:
        import applog
        return applog._real_user_home()
    except Exception:
        return os.path.expanduser("~")


def user_data_dir() -> str:
    """
    The WebView2 profile directory, created if missing.

    Passed to webview.start(storage_path=...) — see the module docstring for why
    that is the only mechanism that actually works.

    This MUST NOT live under sys._MEIPASS: a PyInstaller onefile bundle unpacks to
    a fresh temp dir per launch and deletes it on exit, so a profile stored there
    would be thrown away every single run — the same silent non-persistence bug we
    avoid by not using private mode.
    """
    path = os.path.join(_app_data_root(), "WebView2")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        # Don't die here. start() surfaces an unwritable storage_path as a
        # catchable WebViewException, and run_window() degrades gracefully.
        log.warning("Could not create WebView2 profile dir %s: %s", path, e)
    return path


def browser_profile_dir() -> str:
    """Dedicated profile for the --app= browser fallback. See run_browser_fallback."""
    path = os.path.join(_app_data_root(), "BrowserProfile")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        log.warning("Could not create browser profile dir %s: %s", path, e)
    return path


# =============================================================================
# WebView2 runtime detection
# =============================================================================
def webview2_version() -> str | None:
    """
    The installed Edge WebView2 Runtime version, or None if it is not installed.

    Probes all THREE places the runtime can register itself:
        HKLM, 32-bit view (WOW6432Node)  — the usual one
        HKLM, native view
        HKCU                             — per-user install
    On a real machine we checked, ONLY the WOW6432Node key existed — a detector
    that probes just one location reports "not installed" on a machine where it
    plainly is.

    winreg, not `reg.exe`: the key name contains a braced GUID that reg.exe
    mangles when invoked through a POSIX-y shell, so shelling out gives false
    negatives. winreg reads it correctly and needs no subprocess (which would also
    flash a console window in a windowed build).
    """
    if not IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover — Windows always has it
        return None

    probes = (
        ("HKLM/WOW6432Node", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        ("HKCU", winreg.HKEY_CURRENT_USER, 0),
    )
    for label, hive, view in probes:
        try:
            with winreg.OpenKey(hive, _WEBVIEW2_CLIENT_KEY, 0,
                                winreg.KEY_READ | view) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        except Exception as e:  # unexpected registry weirdness — keep probing
            log.debug("WebView2 probe %s failed: %s", label, e)
            continue

        version = str(value).strip() if value is not None else ""
        # EdgeUpdate writes "0.0.0.0" as a tombstone for a client it knows about
        # but that is not actually installed. That is an ABSENT runtime.
        if not version or version == "0.0.0.0":
            log.debug("WebView2 probe %s: placeholder version %r", label, version)
            continue
        log.debug("WebView2 %s found under %s", version, label)
        return version
    return None


def can_use_native_window() -> bool:
    """
    True if we can put the UI in a real native window.

    Every False path logs WHY at INFO. A silent False here is precisely the bug
    this module defends against: it would drop the user into a browser tab (or,
    worse, into IE11-rendered chrome) with no clue what went wrong, and in a
    windowed build there is no console to notice it on.

    The two OSes need DIFFERENT checks, because their failure modes differ:

      * Windows — the Edge WebView2 Runtime is a separate component that can be
        absent, and pywebview does not raise when it is: it silently degrades to
        MSHTML/IE11, which breaks the Leaflet map while still looking alive. So
        the registry probe is mandatory (see webview2_version()).

      * macOS  — the renderer is WKWebView from the system WebKit framework.
        It is part of the OS; there is no separate runtime to install, miss, or
        probe. The only real question is whether pywebview and its pyobjc
        bindings imported, so we import the Cocoa backend and let that answer it.
    """
    if IS_MACOS:
        try:
            import webview  # noqa: F401
            # Import the actual Cocoa backend rather than trusting that
            # `import webview` implies working pyobjc bindings — a partially
            # installed pyobjc fails HERE, not on the package import.
            import webview.platforms.cocoa  # noqa: F401
        except Exception as e:
            log.info(
                "Native window unavailable on macOS: %s: %s. Falling back to a "
                "browser window. 无法创建原生窗口，将退回到浏览器。",
                type(e).__name__, e,
            )
            return False
        log.info("Native window available (macOS WKWebView).")
        return True

    if not IS_WINDOWS:
        log.info("Native window unavailable: unsupported platform (%s).", sys.platform)
        return False

    version = webview2_version()
    if version is None:
        log.info(
            "Native window unavailable: Edge WebView2 Runtime is NOT installed "
            "(no 'pv' under HKLM/WOW6432Node, HKLM or HKCU). "
            "未检测到 Edge WebView2 运行时，将退回到浏览器窗口。"
        )
        return False

    try:
        import webview  # noqa: F401
    except Exception as e:
        log.info("Native window unavailable: cannot import pywebview: %s: %s",
                 type(e).__name__, e)
        return False

    log.info("Native window available (WebView2 runtime %s).", version)
    return True


# =============================================================================
# The window
# =============================================================================
def run_window(url: str,
               on_close=None,
               title: str = "AnyLoc",
               width: int = DEFAULT_WIDTH,
               height: int = DEFAULT_HEIGHT,
               min_size: tuple[int, int] = DEFAULT_MIN_SIZE) -> bool:
    """
    Open `url` in a native WebView2 window and BLOCK until the user closes it.

    Returns True once the window has been shown and closed; returns False if a
    native window could not be used at all, so the caller can fall back to
    run_browser_fallback() instead of exiting with no UI.

    `on_close` is invoked from pywebview's cancellable `closing` event, which
    fires on the UI thread and BLOCKS the close until the handler returns. That
    blocking is the point: it is the only hook that reliably gives a device
    teardown (~1.4-1.7s) time to actually run before the interpreter exits.

    MUST be called on the main thread — see the module docstring.
    """
    if not can_use_native_window():
        return False

    # Explicit check, not `assert`: assertions are stripped under python -O, and
    # this constraint must hold in every build. Failing here with a clear message
    # beats pywebview's generic WebViewException from three frames deeper.
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "gui.run_window() must be called on the MAIN thread — pywebview "
            "refuses to start anywhere else. Move the server/worker/tunnel onto "
            "background threads and keep main for the window. "
            f"(current thread: {threading.current_thread().name})"
        )

    import webview
    from webview.errors import WebViewException

    # --- the close hook ------------------------------------------------------
    # pywebview 6.2.1 inspects the handler's signature and calls it with ZERO
    # arguments when it declares none. A handler that takes a `window` parameter
    # raises TypeError instead, so keep this signature empty.
    #
    # Returning False from a `closing` handler CANCELS the close. We must never
    # do that by accident, so this wrapper always falls through to `return None`.
    _fired_lock = threading.Lock()
    _fired = [False]

    def _on_closing():
        # Reentrancy guard: Alt+F4 during a slow teardown, or a second close
        # attempt, must not start the shutdown twice.
        with _fired_lock:
            if _fired[0]:
                log.debug("closing event re-entered; ignoring.")
                return None
            _fired[0] = True
        if on_close is None:
            return None
        try:
            log.info("Window closing — running shutdown ... 窗口关闭，正在清理…")
            on_close()
            log.info("Shutdown complete. 清理完成。")
        except Exception as e:
            # A raising handler would be swallowed by pywebview's own logger and
            # could leave the window half-closed. Log it ourselves and let the
            # close proceed regardless — never trap the user in a window they
            # cannot shut.
            log.exception("on_close handler raised: %s: %s", type(e).__name__, e)
        return None

    window = webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=min_size,
    )
    window.events.closing += _on_closing

    storage = user_data_dir()
    log.info("Opening native window: %s (profile: %s)", url, storage)

    # private_mode=False + storage_path is the ONLY combination that persists
    # localStorage across launches. debug=True is never passed: it turns on the
    # WebView2 devtools context menu, which has no place in a shipped build.
    try:
        webview.start(private_mode=False, storage_path=storage)
    except WebViewException as e:
        msg = str(e)
        if "storage path" not in msg.lower() and "not writable" not in msg.lower():
            log.error("webview.start() failed: %s", msg)
            return False
        # An unwritable storage_path is the one start() failure that IS catchable,
        # and it is fatal to nothing but persistence. Retry in private mode: the
        # user loses saved theme/favorites for this run, which beats losing the
        # whole UI.
        log.warning(
            "WebView2 profile dir is not usable (%s). Retrying without "
            "persistence — 本次运行不会保存主题/收藏等设置。", msg
        )
        try:
            webview.start(private_mode=True)
        except Exception as e2:
            log.error("webview.start() retry failed: %s: %s", type(e2).__name__, e2)
            return False

    # start() returns only after the window is gone.
    log.info("Native window closed.")
    return True


# =============================================================================
# Fallback: a chromium browser in app mode
# =============================================================================
def _find_chromium() -> str | None:
    """Locate msedge.exe or chrome.exe. App Paths registry first, then disk."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None

    app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    # Edge first: it ships with Windows, so it is the one that is actually there.
    for exe in ("msedge.exe", "chrome.exe"):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(hive, app_paths + "\\" + exe, 0,
                                        winreg.KEY_READ | view) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                except Exception:
                    continue
                path = str(path).strip('"')
                if path and os.path.isfile(path):
                    return path

    # The registry entry can be missing on a repaired/imaged install; check the
    # standard install roots directly before giving up.
    for root_var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(root_var)
        if not root:
            continue
        for rel in (r"Microsoft\Edge\Application\msedge.exe",
                    r"Google\Chrome\Application\chrome.exe"):
            candidate = os.path.join(root, rel)
            if os.path.isfile(candidate):
                return candidate
    return None


def run_browser_fallback(url: str):
    """
    Show the UI without a native window, and return something the caller can wait on.

    Returns the browser Popen when we managed to start a chromium in app mode
    (Popen.wait() then reliably signals "the user closed the window", which is the
    lifetime signal the caller needs to know when to tear down), or None when all
    we could do was hand the URL to the default browser.

    The --user-data-dir is NOT optional. Without a dedicated profile, `msedge
    --app=<url>` hands the request off to the already-running Edge and the process
    we spawned exits IMMEDIATELY — so Popen.wait() returns at once and the caller
    concludes the user closed a window that is still sitting on screen. A separate
    profile keeps our process the one that owns the window.
    """
    if not IS_WINDOWS:
        # macOS/Linux: reaching here means pywebview (or its bindings) is
        # missing, so all we can do is hand the URL to a browser.
        #
        # As ROOT this is not as simple as webbrowser.open(): that would try to
        # open the browser as root, which typically launches a root-owned browser
        # outside the user's session — or silently nothing at all. `sudo -u <user>
        # open` puts the page in the LOGGED-IN user's browser, where they can
        # actually see it. (The launcher does the same thing for its own browser
        # opening; this is the fallback path that used to miss it.)
        if IS_MACOS:
            sudo_user = os.environ.get("SUDO_USER")
            try:
                euid_root = os.geteuid() == 0
            except AttributeError:
                euid_root = False
            if euid_root and sudo_user and sudo_user != "root":
                try:
                    subprocess.Popen(["sudo", "-u", sudo_user, "open", url])
                    log.info("Opened %s in %s's browser.", url, sudo_user)
                    return None
                except Exception as e:
                    log.warning("Could not open the browser as %s (%s); "
                                "falling back to the default browser.", sudo_user, e)
        try:
            webbrowser.open(url)
        except Exception as e:
            log.error("Could not open a browser for %s: %s", url, e)
        return None

    exe = _find_chromium()
    if exe:
        profile = browser_profile_dir()
        cmd = [
            exe,
            f"--app={url}",
            f"--user-data-dir={profile}",   # mandatory — see docstring
            "--no-first-run",
            "--no-default-browser-check",
        ]
        try:
            # CREATE_NO_WINDOW: a windowed build must never flash a console.
            proc = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
            log.info("Opened app-mode browser: %s (pid %d, profile %s)",
                     os.path.basename(exe), proc.pid, profile)
            return proc
        except Exception as e:
            log.warning("App-mode browser failed (%s: %s); falling back to the "
                        "default browser.", type(e).__name__, e)

    log.info("No chromium browser found; opening the default browser. "
             "未找到 Edge/Chrome，使用默认浏览器打开。")
    try:
        webbrowser.open(url)
    except Exception as e:
        log.error("Could not open a browser for %s: %s", url, e)
    return None


# =============================================================================
# Standalone probe — `python src/gui.py` prints what this layer sees.
# Deliberately runnable without launcher.py or backend.py.
# =============================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    print("webview2_version()       :", webview2_version())
    print("user_data_dir()          :", user_data_dir())
    print("browser_profile_dir()    :", browser_profile_dir())
    print("can_use_native_window()  :", can_use_native_window())
    print("chromium fallback exe    :", _find_chromium())
