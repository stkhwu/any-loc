# platforms/mac

macOS build workspace. Everything here is **generated** and git-ignored — this
folder only holds the structure.

- `.venv/`  — the Python venv used to build the Mac app
              (create with: `python3 -m venv platforms/mac/.venv &&
               platforms/mac/.venv/bin/pip install -U -r ../../requirements.txt pyinstaller`)
- `dist/`   — build output: `AnyLoc.app` + `AnyLoc-<version>.pkg`
- `build/`  — PyInstaller temporary work files

Build with:  `./scripts/build-mac.sh`  (or `./scripts/build-mac.sh test`)
