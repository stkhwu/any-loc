#!/usr/bin/env python3
"""
make_icon.py — 用代码生成 AnyLoc 应用图标。
设计：白色圆角方块底 + 蓝色定位大头针 + 底部 "AnyLoc"（Any 深黑 / Loc 蓝）。
产物：
  web/icon-256.png   预览图（大图，给人看）
  web/icon.ico       多尺寸 Windows 图标（16/32/48/64/128/256）
运行： python scripts/make_icon.py
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

# this script lives in scripts/, so web/ is one level up (project root)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "web")
os.makedirs(OUT_DIR, exist_ok=True)

# 高分辨率画布，最后缩放到各尺寸（抗锯齿更好）
S = 1024
BLUE = (26, 115, 232, 255)       # #1a73e8  和 UI 一致
BLUE_DK = (21, 101, 213, 255)
WHITE = (255, 255, 255, 255)
INK = (32, 33, 36, 255)          # 近黑（Any 的颜色）
PANEL = (255, 255, 255, 255)     # 白色底板
PANEL_EDGE = (232, 234, 237, 255)  # 底板极淡描边（浅灰，任务栏上留个边界）

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# ---- 0) macOS icon grid ------------------------------------------------------
# macOS does NOT draw app icons edge-to-edge: every system icon leaves a ring of
# transparent padding, and the Dock/Finder scale the full canvas. An icon that
# fills its canvas therefore renders visibly LARGER than every neighbour, even
# though the files are the same pixel size.
#
# Measured from the shipping system icons on this Mac (largest .icns variant):
#     Notes / Maps / Safari  ->  opaque bbox (18, 21, 238, 241) of 256px
#                            ->  body 85.9% of canvas, ~7% padding per side
# AnyLoc was (0, 0, 1024, 1024) = 100%, which is exactly the "too big in the
# Dock" bug. BODY_RATIO reproduces Apple's proportion instead of guessing.
#
# The 1px downward bias (top pad 21 vs side 18) is Apple's too — it optically
# centres the shape once the Dock's reflection/label sit underneath.
BODY_RATIO = 220.0 / 256.0        # 0.859 — the measured system ratio
body = int(S * BODY_RATIO)
pad_x = (S - body) // 2
pad_y = pad_x + int(S * (3.0 / 256.0))   # nudge down ~3/256, as Apple does

# Everything below is drawn into `img` in FULL-canvas coordinates; the whole
# composition is scaled into the padded box at export time (see "icon grid"
# below). Keeping the drawing code canvas-sized means the geometry constants
# (pin radius, text size) stay readable and independent of the padding.

# ---- 1) 白色圆角方块底 ----
# 22% of the *body* is the macOS squircle radius; drawn at canvas scale here and
# scaled down with everything else.
radius = int(S * 0.22)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=PANEL,
                    outline=PANEL_EDGE, width=max(2, int(S * 0.006)))

# ---- 2) 蓝色定位大头针（pin）在上半部居中 ----
import math
cx = S // 2
pin_top = int(S * 0.15)
pin_r = int(S * 0.185)          # 头部圆半径
pin_cy = pin_top + pin_r         # 头部圆心 y
tip_y = int(S * 0.64)            # 针尖 y

# 竖直渐变的蓝（上亮下深），做个渐变蓝 pin
def blue_grad_shape(draw_fn):
    """在一个临时图上用蓝渐变填充由 draw_fn 描出的形状，返回该图。"""
    shape_mask = Image.new("L", (S, S), 0)
    draw_fn(ImageDraw.Draw(shape_mask))
    grad = Image.new("RGBA", (S, S), BLUE)
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        r = int(BLUE[0] + (BLUE_DK[0] - BLUE[0]) * t)
        g = int(BLUE[1] + (BLUE_DK[1] - BLUE[1]) * t)
        b = int(BLUE[2] + (BLUE_DK[2] - BLUE[2]) * t)
        gd.line([(0, y), (S, y)], fill=(r, g, b, 255))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(grad, (0, 0), shape_mask)
    return out

theta = math.radians(52)
lx = cx - pin_r * math.sin(theta)
ly = pin_cy + pin_r * math.cos(theta)
rx = cx + pin_r * math.sin(theta)
ry = pin_cy + pin_r * math.cos(theta)

def draw_pin(dr):
    dr.polygon([(lx, ly), (cx, tip_y), (rx, ry)], fill=255)
    dr.ellipse([cx - pin_r, pin_cy - pin_r, cx + pin_r, pin_cy + pin_r], fill=255)

pin_img = blue_grad_shape(draw_pin)
img.alpha_composite(pin_img)

d = ImageDraw.Draw(img)
# 中间白色圆孔（露出白底）
hole_r = int(pin_r * 0.42)
d.ellipse([cx - hole_r, pin_cy - hole_r, cx + hole_r, pin_cy + hole_r], fill=WHITE)

# ---- 3) 底部 "AnyLoc" 文字（Any 深黑 / Loc 蓝）----
def load_font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()

fsize = int(S * 0.145)
font = load_font(fsize)
txt_any, txt_loc = "Any", "Loc"
def tw(s, f):
    b = d.textbbox((0, 0), s, font=f); return b[2] - b[0]
w_any, w_loc = tw(txt_any, font), tw(txt_loc, font)
total = w_any + w_loc
ty = int(S * 0.70)
sx = cx - total // 2
d.text((sx, ty), txt_any, font=font, fill=INK)
d.text((sx + w_any, ty), txt_loc, font=font, fill=BLUE)

# ---- 导出 ----
# Compose the finished artwork into the macOS icon grid: scale the whole drawing
# down to `body` px and paste it into a transparent S x S canvas at (pad_x,
# pad_y). This is the step that makes the Dock render AnyLoc at the same visual
# size as its neighbours — see the BODY_RATIO note at the top.
art = img.resize((body, body), Image.LANCZOS)
canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
canvas.alpha_composite(art, (pad_x, pad_y))
img = canvas

preview = img.resize((256, 256), Image.LANCZOS)
preview_path = os.path.join(OUT_DIR, "icon-256.png")
preview.save(preview_path)

# Windows: the .ico keeps the same padded artwork. Windows scales taskbar icons
# to fit rather than assuming a fixed inset, so the padding is harmless there,
# and one shared composition keeps the two platforms visually identical.
ico_path = os.path.join(OUT_DIR, "icon.ico")
img.save(ico_path, format="ICO",
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

# ---- macOS .icns ----
# Built with Apple's own `iconutil` from a .iconset directory, which is the
# format it insists on: each file must be named icon_<size>[@2x].png exactly.
# Without an .icns the .app shows the generic blank-page icon in Finder and the
# Dock. iconutil only exists on macOS, so this step is skipped elsewhere and the
# Windows .ico above is unaffected.
icns_path = os.path.join(OUT_DIR, "icon.icns")
if sys.platform == "darwin":
    import shutil
    import subprocess
    import tempfile

    iconset = os.path.join(tempfile.mkdtemp(), "icon.iconset")
    os.makedirs(iconset, exist_ok=True)
    # (base size, @2x?) -> the pairs macOS expects for a complete iconset.
    for size in (16, 32, 128, 256, 512):
        img.resize((size, size), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{size}x{size}.png"))
        img.resize((size * 2, size * 2), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
    try:
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns_path],
                       check=True, capture_output=True)
        print("  图标 ICNS:", icns_path)
    except Exception as e:
        # iconutil can fail for reasons that have nothing to do with the
        # artwork — a sandboxed/restricted environment refuses it even for a
        # trivially valid iconset. Falling back to Pillow's own ICNS writer
        # keeps `build-mac.sh` working instead of shipping a .app with the
        # generic blank-page icon, which is a user-visible defect that the
        # build otherwise happily passes (build-mac.sh only *warns* on a
        # missing icon).
        try:
            img.save(icns_path, format="ICNS")
            print("  图标 ICNS:", icns_path, "(Pillow fallback — iconutil 不可用)")
        except Exception as e2:
            print("  (.icns 生成失败 — iconutil:", e, "| Pillow:", e2, ")")
    finally:
        shutil.rmtree(os.path.dirname(iconset), ignore_errors=True)

print("生成：")
print("  预览 PNG:", preview_path)
print("  图标 ICO:", ico_path)
