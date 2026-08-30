#!/bin/bash
# =============================================================================
#  build-mac.sh — 在 macOS 上把 AnyLoc 打包成 .app 和 .pkg
# =============================================================================
#  产物（都输出到 platforms/mac/dist/）：
#    platforms/mac/dist/AnyLoc.app          —— 拖进「应用程序」双击运行
#    platforms/mac/dist/AnyLoc-1.1.0.pkg    —— 安装包，双击装进 /Applications
#
#  用法：
#    ./build-mac.sh              # 正式版（带 root 提权流程）
#    ./build-mac.sh test         # 测试版（AnyLocTest.app，可 --selftest 验证）
#
#  依赖：Python 3.10+，并已安装 pymobiledevice3 与 pyinstaller。
#  脚本会优先使用 platforms/mac/.venv 虚拟环境（再回退到 .venv-mac / .venv）。
# =============================================================================
set -euo pipefail
# this script lives in scripts/, so cd up to the project root first
cd "$(dirname "$0")/.."

VARIANT="${1:-final}"
# Version comes from the VERSION file at the repo root — the same file
# AnyLoc.spec reads for CFBundleVersion. Keeping one source means the .pkg
# filename and the version inside the .app can never disagree.
VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || true)"
if [ -z "$VERSION" ]; then
  echo "!! 读不到 VERSION 文件（仓库根目录），无法确定版本号。"
  exit 1
fi

# All Mac build artifacts live under platforms/mac/ (venv, work files, dist).
MAC_DIR="platforms/mac"
DIST_DIR="$MAC_DIR/dist"
WORK_DIR="$MAC_DIR/build"

if [ "$VARIANT" = "test" ]; then
  APP_NAME="AnyLocTest"
  BUNDLE_ID="com.anyloc.app.test"
else
  APP_NAME="AnyLoc"
  BUNDLE_ID="com.anyloc.app"
fi

# ---- pick a Python interpreter ---------------------------------------------
if [ -x "$MAC_DIR/.venv/bin/python" ]; then
  PY="$MAC_DIR/.venv/bin/python"
elif [ -x ".venv-mac/bin/python" ]; then
  PY=".venv-mac/bin/python"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="$(command -v python3)"
fi
echo "==> 使用 Python: $PY  ($("$PY" --version 2>&1))"

if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "!! 没装 PyInstaller。请先： $PY -m pip install -U pyinstaller pymobiledevice3"
  exit 1
fi

# pywebview must be in THIS venv or PyInstaller cannot see it, and the app would
# silently fall back to a browser window instead of the native one — a build that
# looks fine and is quietly the wrong product. Same guard as build-win.ps1.
if ! "$PY" -c "import webview" 2>/dev/null; then
  echo "!! 没装 pywebview —— 打包出来的 App 会退化成浏览器窗口，而不是原生窗口。"
  echo "   请先： $PY -m pip install -U pywebview"
  exit 1
fi

# The .app icon is generated, not committed as a binary blob. Warn (don't fail)
# so a missing Pillow doesn't block a build.
if [ ! -f "web/icon.icns" ]; then
  echo "!! 提示：缺少 web/icon.icns，App 将没有图标。"
  echo "   生成： $PY scripts/make_icon.py   （需要 Pillow）"
fi

# ---- 1) build the .app with PyInstaller ------------------------------------
echo "==> [1/3] PyInstaller 打包 ($VARIANT) → $DIST_DIR ..."
ANYLOC_VARIANT="$VARIANT" "$PY" -m PyInstaller --clean --noconfirm \
  --distpath "$DIST_DIR" --workpath "$WORK_DIR" \
  AnyLoc.spec

APP_PATH="$DIST_DIR/${APP_NAME}.app"
if [ ! -d "$APP_PATH" ]; then
  echo "!! 打包失败：找不到 $APP_PATH"
  exit 1
fi
echo "    生成： $APP_PATH"

# ---- 1.5) make the inner CLI executable ad-hoc code-signed -----------------
# Unsigned apps on Apple Silicon must at least be ad-hoc signed to run.
#
# Deliberately NOT `--deep`: Apple has formally deprecated it for signing, and it
# would re-sign the BUNDLED adb too — replacing Google's valid Developer ID
# signature (TeamIdentifier EQHXZ8M8AV, hardened runtime) with an ad-hoc one.
# Correct practice is inside-out: sign nested code first, then the bundle, and
# leave already-validly-signed third-party binaries alone.
echo "==> 对 .app 做 ad-hoc 代码签名（避免 Gatekeeper 直接拒绝）..."
# Strip extended attributes FIRST. codesign refuses a bundle carrying resource
# forks / Finder info ("resource fork, Finder information, or similar detritus
# not allowed"), and macOS attaches com.apple.provenance (and OneDrive/iCloud
# add their own) to files as they are written — so a build from a synced folder
# fails to sign for reasons that have nothing to do with the code.
xattr -cr "$APP_PATH" 2>/dev/null || true
codesign --force --sign - "$APP_PATH" 2>/dev/null || \
  echo "    (codesign 失败，忽略——首次打开时右键→打开即可)"

# Prove the signature is actually valid rather than assuming it took.
if codesign --verify --strict "$APP_PATH" 2>/dev/null; then
  echo "    签名校验通过 ✓"
else
  echo "    !! 签名校验未通过（首次打开时右键→打开即可）"
fi

# ---- 2) build a component .pkg that installs into /Applications ------------
echo "==> [2/3] 生成安装包 .pkg ..."
PKG_ROOT="$(mktemp -d)"
COMPONENT_DIR="$(mktemp -d)"
COMPONENT_PKG="$COMPONENT_DIR/component.pkg"
COMPONENT_PLIST="$COMPONENT_DIR/component.plist"

# Clean up temp trees no matter how we exit (success or failure).
trap 'rm -rf "$PKG_ROOT" "$COMPONENT_DIR"' EXIT

mkdir -p "$PKG_ROOT/Applications"
cp -R "$APP_PATH" "$PKG_ROOT/Applications/"

# Force a fixed install location: without a component plist, pkgbuild marks the
# app "relocatable by bundle id", so if a same-id app already exists elsewhere
# the installer may drop it THERE instead of /Applications. Pin it down.
PLIST_ARGS=()
pkgbuild --analyze --root "$PKG_ROOT" "$COMPONENT_PLIST" >/dev/null 2>&1 || true
if [ -f "$COMPONENT_PLIST" ]; then
  /usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$COMPONENT_PLIST" 2>/dev/null || true
  PLIST_ARGS=(--component-plist "$COMPONENT_PLIST")
fi

pkgbuild \
  --root "$PKG_ROOT" \
  --install-location "/" \
  "${PLIST_ARGS[@]+"${PLIST_ARGS[@]}"}" \
  --identifier "$BUNDLE_ID" \
  --version "$VERSION" \
  "$COMPONENT_PKG"

OUT_PKG="$DIST_DIR/${APP_NAME}-${VERSION}.pkg"
# productbuild wraps the component into a friendlier distribution installer.
productbuild \
  --package "$COMPONENT_PKG" \
  "$OUT_PKG"

echo
echo "============================================================"
echo "  ✓ 完成！"
echo "    App:  $APP_PATH"
echo "    Pkg:  $OUT_PKG"
echo
echo "  把 $OUT_PKG 发给用户，双击安装 → 应用进 /Applications。"
echo "  首次打开若被拦截：右键 AnyLoc.app → 打开 → 打开。"
echo "============================================================"
