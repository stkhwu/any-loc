# any-loc

A tiny, self-hosted **iOS & Android GPS spoofer for Windows and macOS** with a
Google-Maps-style web UI. Click the map to teleport your iPhone/iPad or Android
phone, or drive around with a joystick / WASD keys.

This is a DIY, local-only take on tools like AnyGo / iToolab. Everything runs on your
own machine; nothing is uploaded anywhere.

AnyLoc **auto-detects** what you plug in: an Android phone (over adb) takes the
Android path — no admin needed; otherwise it drives an iPhone/iPad over Apple's
tunnel (which needs elevation). Same web UI either way.

```
   Browser (map + joystick)  ──HTTP──►  backend.py ──┬─ iOS:     DVT LocationSimulation  ──► iPhone
                                                      │          (pymobiledevice3 tunneld)
                                                      └─ Android: adb `cmd location`     ──► Android phone
                                                                 (bundled adb, no root)
```

The exact same Python code runs on both OSes, and both get the same **native app
window**. Only these differ, and all of it is handled automatically:

| | Windows | macOS |
|---|---|---|
| **Elevation** (for the tunnel) | Administrator via a **UAC** prompt | **usually none at all** — iOS 17.4+ uses an in-process tunnel that needs no root. Only iOS 17.0–17.3 asks for your password |
| **USB driver** for the iPhone | needs **iTunes / Apple Mobile Device Support** | built into macOS (**nothing to install**) |
| **Ships as** | a single `AnyLoc.exe` | `AnyLoc.app` + `AnyLoc.pkg` |
| **What you see** | one **app window**, no console | one **app window** — no Terminal, no browser tab |
| **Renderer** | Edge **WebView2** | system **WebKit** (nothing to install) |

---

## Requirements

**Both platforms**
- **Python 3.10+** (only needed if you run from source or build; end users of the
  packaged app don't need Python).
- The Python dependencies: `pip install -U -r requirements.txt`
  (that is `pymobiledevice3` — **v10+** is what enables the no-root tunnel on
  macOS — plus `pywebview` for the native window).
- Building the app on **either** OS uses the same `requirements.txt`, and
  `scripts/build-win.ps1` / `scripts/build-mac.sh` install it into the build venv
  for you. They refuse to build without `pywebview`, because PyInstaller only
  bundles what it can see. On macOS it pulls in `pyobjc` automatically.

**Windows — the app window**
- AnyLoc draws its UI in a native window using the **Edge WebView2 Runtime**. It ships
  with Windows 11 and with current Windows 10, so on essentially every machine it is
  already there and there is nothing to do.
- If it is missing (a trimmed LTSC/Server image, or it was removed), AnyLoc notices,
  writes it to the log, and opens the UI in an **Edge/Chrome app window** instead —
  same app, same features, just not a native window. Installing the free
  [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
  restores the real window.

### To spoof an iPhone/iPad
- An **iPhone/iPad** with a Lightning/USB-C cable.
- On the device: **Developer Mode ON**
  (`Settings > Privacy & Security > Developer Mode` → toggle on → reboot).
- **Windows:** **iTunes / Apple Mobile Device Support** installed (provides the USB
  driver Windows needs to see the iPhone). Installing Apple's iTunes from apple.com
  is the easy way.
- **macOS:** nothing extra — macOS already talks to iPhones over USB (`usbmuxd` is
  built in).

### To spoof an Android phone
- **Android 11 or newer** (AnyLoc uses the system `cmd location` command, added in
  Android 11). Pure-HarmonyOS-NEXT Huawei devices (no adb) are not supported;
  HarmonyOS 4 / EMUI and below still work.
- **Developer Options** on, and **USB debugging** on
  (`Settings > About phone` → tap *Build number* 7×, then `Settings > System >
  Developer options > USB debugging`). On some phones (Xiaomi, Huawei) also enable
  **USB debugging (Security settings)**.
- Plug in with a USB cable and tap **Allow USB debugging** when the phone asks.
- **No app to install, no root, and no admin/UAC** — `adb` is bundled with AnyLoc,
  and it grants itself mock-location over adb. macOS talks to Android over USB with
  nothing extra.

**macOS — architecture note:** the packaged `AnyLoc.app` matches the Mac it was
**built** on (Apple Silicon build won't run on Intel and vice-versa). Running from
source works on any Mac with Python.


---

## Usage

### Windows

1. Download **`AnyLoc.exe`** and double-click it. It's a single self-contained
   file — no install, nothing to unzip.
2. Allow the **UAC** prompt (the tunnel needs Administrator).
3. The **AnyLoc window** opens — a normal app window with the map in it. No
   console, no browser tab, nothing else to keep open.
   (First launch takes a few extra seconds — the single exe unpacks itself once.)
4. Click **Connect**. When the dot turns green, you're live.
5. **Click anywhere on the map** to teleport, or use the **joystick / WASD / arrows**
   to move. Pick a speed (Walk / Run / Bike / Drive); hold **Shift** for 3×.
6. Press **Reset GPS** to stop spoofing and restore the real location.
   (**Closing the AnyLoc window** also restores real GPS — it waits a second or two
   to put the phone back before it exits. So does rebooting the phone.)

Minimizing the window is fine. Just don't close it while you want the fake
location to stick.

**The map is drawn by the Edge WebView2 Runtime**, which ships with Windows 11 and
with current Windows 10 — on almost every machine it is simply already there, and
you never think about it. If it is missing (a stripped LTSC/Server image, or it was
removed), AnyLoc says so in the log and falls back to opening the UI in an
**Edge/Chrome app window** instead. Everything works the same; it just isn't a
native window. To get the real window back, install the free
[Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
from Microsoft.

**Where the log is.** There is no console window any more, so if something goes
wrong the log file is the place to look:

```
%LOCALAPPDATA%\AnyLoc\logs\anyloc.log
```

(that expands to `C:\Users\<you>\AppData\Local\AnyLoc\logs\anyloc.log` — paste the
`%LOCALAPPDATA%` form into Explorer's address bar and it will take you there). It
rotates at 1 MB and keeps 3 old copies. The same lines are visible live inside the
app: expand the **Logs** panel at the bottom of the side panel. If AnyLoc hits a
fatal error it shows a message box with the exception and this path.

### macOS

1. Install **`AnyLoc.pkg`** (double-click → installs `AnyLoc.app` into `/Applications`),
   or just drag `AnyLoc.app` into **Applications**.
   - The `.pkg` and `.app` are **ad-hoc signed, not notarized** (no paid Apple
     Developer ID). After downloading/AirDropping, macOS Gatekeeper may block the
     first open. If the installer or app is blocked with *"Apple cannot check it
     for malicious software"*: **right-click it → Open → Open** (once), or run
     `xattr -dr com.apple.quarantine AnyLoc.pkg` / `AnyLoc.app` to clear the flag.
2. Launch **AnyLoc** from Applications / Launchpad / Spotlight.
   - First launch of the app may also need the same **right-click → Open** once.
3. The **AnyLoc window** opens — a normal Mac app window with the map in it. No
   Terminal, no browser tab, nothing else to keep open.
   - **No password on iOS 17.4+ or Android.** AnyLoc builds the tunnel inside its
     own process, which needs no root.
   - **iOS 17.0–17.3 only:** those versions require the privileged kernel tunnel,
     so macOS shows its standard password sheet. Enter your login password.
     (You may also see a one-time *"AnyLoc wants to control Terminal"* prompt —
     click **OK**.)
4. Click **Connect**. When the dot turns green, you're live. From here it's
   identical to Windows: click the map / joystick / WASD → **Reset GPS** to restore.
5. To stop: **close the AnyLoc window** — it restores your real GPS before it
   exits (it waits a second or two to put the phone back). Quitting with ⌘Q, or
   rebooting the phone, works too.

Minimizing the window is fine. Just don't close it while you want the fake
location to stick.

**Where the log is** (macOS):

```
~/Library/Logs/AnyLoc/anyloc.log
```

The same lines are visible live inside the app: expand the **Logs** panel at the
bottom of the side panel.

---

## Notes, limits, gotchas

- **iOS 17 / 18+** use the RemoteXPC "tunnel" path (what this project targets). The tunnel
  must stay running the whole time — it lives inside the app, so that means keeping the
  **AnyLoc window** open.
- The tunnel requires a **USB cable**. Going fully wireless is possible but needs an initial
  wired pairing plus extra setup; not included here to keep it simple and reliable.
- On the very first connect, `backend.py` **auto-mounts the Developer Disk Image** for your
  iOS version. This needs internet (it fetches the DDI) and can take a minute once.
- If **Connect** errors:
  - **Windows:** make sure you allowed the **UAC** prompt.
    **macOS:** no password is needed for iOS 17.4+; on iOS 17.0-17.3 make sure you
    entered it when asked.
  - Unlock the phone; re-accept **Trust**; confirm **Developer Mode** is on.
  - Unplug/replug the cable; try a different USB port (a direct port is often more
    reliable than a hub).
  - Open the **Logs** panel in the app, or read the log file — with no console
    window, that is where the real error message is:
    Windows `%LOCALAPPDATA%\AnyLoc\logs\anyloc.log`,
    macOS `~/Library/Logs/AnyLoc/anyloc.log`.
- **Only one AnyLoc at a time.** A second launch notices the first (a named mutex on
  Windows, a file lock on macOS) and tells you to switch to the window that's already
  open, rather than quietly fighting it for the phone.
- Movement is integrated client-side and streamed at ~10 Hz; the backend keeps Apple's DVT
  `LocationSimulation` channel **open** and applies the latest coordinate (latest-wins), which
  is what makes the joystick feel smooth instead of teleporting in jumps.

### Command-line options (optional)
```bash
python src/backend.py --port 8765 --no-browser -v
```
- `--port` web UI port (default 8765)
- `--no-connect` don't auto-connect on startup (click Connect yourself)
- `--tunneld-port` if you ran tunneld on a non-default port
- `-v` verbose logging (shows pymobiledevice3 detail)

---

## Project layout

```
any-loc/
├── src/               all Python source
│   ├── launcher.py       all-in-one entry point (elevates only if needed, starts server + tunnel + window)
│   ├── gui.py            the native app window (WebView2 on Windows, WebKit on macOS) + browser fallback
│   ├── applog.py         file/ring logging + the null-stream shim the windowed build needs
│   ├── backend.py        device worker + static server + JSON API (stdlib only)
│   ├── backend_base.py   LocationBackend abstraction (shared by iOS + Android)
│   ├── ios_backend.py    iOS backend (pymobiledevice3 tunnel + DVT)
│   └── android_backend.py Android backend (bundled adb + `cmd location`)
├── AnyLoc.spec        PyInstaller build spec (Windows .exe / macOS .app)
├── VERSION            the one place the version number lives (spec + build-mac.sh read it)
├── requirements.txt   Python dependencies
├── web/               the UI (index.html, app.js, i18n.js, config.js, icons)
│   └── vendor/           Leaflet, vendored so the map works with no internet
├── vendor/            bundled adb (vendor/win/adb.exe, vendor/mac/adb)
├── scripts/           launch & build helpers
│   ├── dev.bat / dev.command    dev mode (UI hot-reload, optional real tunnel)
│   ├── AnyLoc.command           run-from-source launcher (macOS, no packaging)
│   ├── build-win.ps1            build AnyLoc.exe (creates the venv, checks the PE header)
│   ├── build-mac.sh             build AnyLoc.app + AnyLoc.pkg
│   └── make_icon.py             regenerate web/icon.ico + icon.icns + icon-256.png
└── platforms/         per-OS build workspace (generated, git-ignored)
    ├── mac/           .venv/ + dist/ (AnyLoc.app, AnyLoc.pkg) + build/
    └── windows/       .venv/ + dist/ (AnyLoc.exe) + build/
```

The Python source lives in `src/`; `web/`, `vendor/`, and `AnyLoc.spec` stay at the
repo root. `import backend` works because all `src/*.py` sit together (and the spec
puts `src/` on `pathex`). The `scripts/` helpers `cd` back to the root and run
`src/launcher.py`.

Each OS gets its own build workspace under `platforms/<os>/` — its build venv
(`.venv/`), PyInstaller work dir (`build/`), and output (`dist/`). All of it is
generated and git-ignored; only the two `platforms/*/README.md` placeholders are
tracked so the structure survives a clone.

---

## Building from source

The same `AnyLoc.spec` builds on both OSes; `ANYLOC_VARIANT` picks test vs. shippable.
Each OS builds into its own `platforms/<os>/` workspace.

### Windows → a single `AnyLoc.exe`
```powershell
.\scripts\build-win.ps1 test     # test build → platforms\windows\dist\AnyLocTest.exe
.\scripts\build-win.ps1          # shippable  → platforms\windows\dist\AnyLoc.exe

# validate the test exe without UAC:
platforms\windows\dist\AnyLocTest.exe --selftest        # expect RESULT: PASS
```
`scripts/build-win.ps1` is the Windows counterpart of `build-mac.sh`: it creates (or
reuses) `platforms\windows\.venv`, installs `requirements.txt` + `pyinstaller` +
**`pywebview`**, runs PyInstaller into `platforms\windows\dist`, and then checks the
PE header of the result to prove the exe really is a **GUI** binary and not a console
one. Useful flags: `-OneDir` (folder build instead of one file), `-SkipDeps` (skip pip
when nothing changed).

> `pywebview` must be installed **in that venv** — PyInstaller only bundles what it
> can see. Build without it and you get an exe that silently falls back to a browser
> window instead of the native one. The script checks and refuses to continue.

Prefer the script, but the raw commands still work if you want them:
```powershell
$env:ANYLOC_VARIANT="test"   # or "final"
pyinstaller --clean --noconfirm --distpath platforms\windows\dist --workpath platforms\windows\build AnyLoc.spec
```

**Two things the Windows build does deliberately:**

- **`console=False`** — the exe is a GUI binary (PE subsystem = GUI), so Windows never
  gives it a console window. The consequence is that `sys.stdout`/`stderr` are `None`
  at runtime and `print()` goes nowhere: everything diagnostic must go through
  `applog`, which writes `%LOCALAPPDATA%\AnyLoc\logs\anyloc.log`. macOS keeps
  `console=True` so the `sudo` fallback (iOS 17.0-17.3) still has a TTY to prompt on;
  the common no-root path does not need it.
- **`runtime_tmpdir`** — the onefile exe unpacks to
  `%LOCALAPPDATA%\AnyLoc\runtime-<version>\` instead of `%TEMP%`. Machines with WDAC /
  Application Control commonly forbid loading DLLs out of `%TEMP%`, which kills a
  default onefile build during bootstrap — before any of our code runs, so there is no
  error message to go on. Unpacking under `LOCALAPPDATA` avoids that policy. Each run
  uses a fresh subfolder and cleans it up on exit; the version in the path keeps one
  version's stranded leftovers (from a crash) distinguishable from another's.

Onefile is the default because it is the thing you hand to a user: one file, no folder
to keep together. `-OneDir` remains available if you want the unpacked form (faster
startup, easier to inspect).

### macOS → `AnyLoc.app` + `AnyLoc.pkg`
```bash
# one-time: make the build venv
python3 -m venv platforms/mac/.venv
platforms/mac/.venv/bin/pip install -U -r requirements.txt pyinstaller pywebview

./scripts/build-mac.sh          # shippable: platforms/mac/dist/AnyLoc.app + AnyLoc-1.1.0.pkg
./scripts/build-mac.sh test     # test: platforms/mac/dist/AnyLocTest.app

# validate the test app without root:
platforms/mac/dist/AnyLocTest.app/Contents/MacOS/AnyLocTest --selftest   # expect RESULT: PASS
```
`scripts/build-mac.sh` auto-uses `platforms/mac/.venv`, checks that `pywebview` is
present (without it the app would silently degrade to a browser window), runs
PyInstaller (output → `platforms/mac/dist`), ad-hoc code-signs the `.app` and verifies
the signature, then wraps it into a `.pkg` that installs into `/Applications`.

The `.app` icon comes from `web/icon.icns`; regenerate it (and the Windows `.ico`) with
`python scripts/make_icon.py`, which uses Apple's `iconutil`.

### Dev mode (fast UI iteration, no packaging)
Edit anything under `web/` and the browser auto-refreshes in ~1s.
- **Windows:** double-click `scripts/dev.bat` (UI only), or right-click → **Run as administrator**
  (adds the real tunnel so you can test on a device).
- **macOS:** `./scripts/dev.command` (UI only), or `sudo ./scripts/dev.command` (adds the real tunnel).

### App icon
The icon is generated from code — no binary editing. Regenerate `web/icon.ico`
(Windows), `web/icon.icns` (macOS) + `web/icon-256.png` with:
```bash
python scripts/make_icon.py     # needs Pillow: pip install pillow
```
The `.icns` step runs only on macOS (it shells out to Apple's `iconutil`).

---

## How it works (for the curious)

`backend.py`:
- Talks to `pymobiledevice3 remote tunneld` on `127.0.0.1:49151` via `get_tunneld_devices()`
  to obtain a connected `RemoteServiceDiscoveryService` for the phone.
- `auto_mount(rsd)` mounts the Developer Disk Image if needed.
- Opens `DvtProvider(rsd)` → `LocationSimulation(dvt)` as a long-lived async context manager
  on a dedicated event-loop thread, and calls `loc.set(lat, lon)` / `loc.clear()`.
- Serves the static UI and a small JSON API (`/api/connect`, `/api/set`, `/api/clear`,
  `/api/status`) using only the Python standard library — no web framework.

`launcher.py` is the all-in-one entry point (what becomes `AnyLoc.exe` / `AnyLoc.app`).
It starts the web server + device worker on background threads and gives the **main
thread to the app window** — pywebview refuses to start anywhere else, so everything
that used to block main is now a daemon thread. Closing the window fires a `closing`
handler that tears down in order (restore real GPS → stop the web server → stop the
tunnel) and only then lets the process exit. POSIX signal handlers (`SIGHUP`/`SIGTERM`/
`SIGINT`) run that same teardown, because `atexit` does **not** run when a process is
killed by a signal — without them, quitting could leave the phone on the fake location.

**Elevation is now the exception, not the rule.** The tunnel only ever needed
Administrator/root because it built a *kernel* `utun` interface. pymobiledevice3 10 can
build the same tunnel on a pure-Python userspace network stack instead, so on macOS
AnyLoc runs the tunnel **inside its own unprivileged process** (`run_ios_userspace()`)
and never asks for a password — which also keeps the GUI out of root, where it would
otherwise write its profile and logs into root's home. Windows keeps the original
elevated path (UAC is one click). iOS 17.0–17.3 has no CoreDeviceProxy service and still
needs the kernel tunnel; there macOS shows its **native password sheet**, falling back to
Terminal + `sudo` only if that fails.

`gui.py` owns the window, and the check differs per OS because the failure modes do.
On **Windows** it probes the registry for the **Edge WebView2 Runtime**, because a
missing runtime raises no exception — pywebview silently falls back to the ancient
MSHTML engine, which would break the map while looking like it worked. If the runtime
really is absent, AnyLoc opens the UI in a dedicated Edge/Chrome app window instead and
watches that process for the close signal. On **macOS** the renderer is WKWebView from
the system WebKit framework: there is no separate runtime to install or miss, so it
simply imports the Cocoa backend and lets that answer the question.

`applog.py` exists because `console=False` means `sys.stdout` is `None` and `print()`
output is discarded without error. It installs harmless stand-in streams (several
libraries, uvicorn included, call `sys.stdout.isatty()` during setup and would crash on
`None`), writes the rotating log under `%LOCALAPPDATA%\AnyLoc\logs\` (Windows) /
`~/Library/Logs/AnyLoc/` (macOS), and keeps the last few hundred lines in memory for the
in-app **Logs** panel to read via `/api/logs`. Both path helpers resolve the *real*
user's home even when running under `sudo`, so an elevated run and an ordinary one write
to the same file instead of stranding it in root's home.

All pymobiledevice3 calls were verified against **v9.3x**; the no-root userspace tunnel
additionally requires **v10+** and is skipped automatically on older versions.

---

## Legal / ethical

Location spoofing has legitimate uses (development, testing, privacy). Using it to defeat
another service's rules (games, check-in/attendance, dating, etc.) may violate their terms
and, in some cases (e.g. faking work attendance), the law. You are responsible for how you
use this. Provided as-is, for learning and legitimate testing.
