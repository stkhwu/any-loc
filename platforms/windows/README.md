# platforms/windows

Windows build workspace. Everything here is **generated** and git-ignored — this
folder only holds the structure.

- `.venv/`  — the Python venv used to build the Windows exe
              (create with: `py -m venv platforms\windows\.venv` then
               `platforms\windows\.venv\Scripts\pip install -U -r ..\..\requirements.txt pyinstaller`)
- `dist/`   — build output: `AnyLoc.exe`
- `build/`  — PyInstaller temporary work files

Build on a Windows machine with:
```powershell
$env:ANYLOC_VARIANT="final"
pyinstaller --clean --noconfirm --distpath platforms\windows\dist --workpath platforms\windows\build AnyLoc.spec
```
(PyInstaller cannot cross-build a Windows .exe from macOS — build this on Windows.)
